"""Simulate Nuke's startup sequence against the installed .nuke config.

Executes init.py and menu.py the way Nuke does -- init.py with no UI available
(no ``nukescripts``), menu.py afterwards -- using a fake ``nuke`` module. Proves
the addon imports at init time, the menu commands register, and the socket
server auto-starts on the expected port.

Usage:
    python scripts/verify_nuke_startup.py [nuke_home] [expected_port]
"""

import os
import socket
import sys
import types


def build_fake_nuke(recorder):
    nuke = types.ModuleType("nuke")

    def plugin_add_path(path, *args, **kwargs):
        recorder["plugin_paths"].append(path)

    class FakeMenu(object):
        def __init__(self, kind, name):
            self.kind = kind
            self.name = name

        def addCommand(self, label, command=None, *args, **kwargs):
            recorder["commands"].append((self.kind, self.name, label))

    nuke.pluginAddPath = plugin_add_path
    nuke.menu = lambda name: FakeMenu("menu", name)
    nuke.toolbar = lambda name: FakeMenu("toolbar", name)
    nuke.executeInMainThreadWithResult = lambda func, kwargs=None: func(**(kwargs or {}))
    nuke.executeInMainThread = lambda func, kwargs=None: func(**(kwargs or {}))
    nuke.Int_Knob = lambda *a, **k: None
    nuke.Text_Knob = lambda *a, **k: None
    nuke.PyScript_Knob = lambda *a, **k: None
    nuke.STARTLINE = 0
    return nuke


def run_script(path, fake_nuke, nuke_home):
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    namespace = {"__name__": "__main__", "__file__": path, "nuke": fake_nuke}
    cwd = os.getcwd()
    os.chdir(nuke_home)
    try:
        exec(compile(source, path, "exec"), namespace)
    finally:
        os.chdir(cwd)
    return namespace


def main():
    nuke_home = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.nuke")
    expected_port = int(sys.argv[2]) if len(sys.argv) > 2 else 9877

    # Nuke does not export NUKE_MCP_PORT; menu.py is responsible for it.
    os.environ.pop("NUKE_MCP_PORT", None)
    os.environ.pop("NUKE_MCP_HOST", None)

    recorder = {"plugin_paths": [], "commands": []}
    fake_nuke = build_fake_nuke(recorder)
    sys.modules["nuke"] = fake_nuke
    # init.py runs before Nuke's UI exists, so nukescripts must be absent.
    sys.modules.pop("nukescripts", None)

    failures = []

    init_path = os.path.join(nuke_home, "init.py")
    menu_path = os.path.join(nuke_home, "menu.py")

    run_script(init_path, fake_nuke, nuke_home)
    print("init.py    : plugin paths added -> %s" % recorder["plugin_paths"])

    for rel in recorder["plugin_paths"]:
        resolved = os.path.abspath(os.path.join(nuke_home, rel))
        if resolved not in sys.path:
            sys.path.insert(0, resolved)

    menu_ns = run_script(menu_path, fake_nuke, nuke_home)
    addon = menu_ns.get("nuke_mcp_addon")

    if addon is None:
        failures.append("menu.py did not import nuke_mcp_addon")
        print("\n=== FAILURES ===\n - %s" % failures[0])
        return 1

    print("addon      : %s" % addon.__file__)
    print("menu items : %s" % [item[2] for item in recorder["commands"]])

    if not recorder["commands"]:
        failures.append("no menu/toolbar commands were registered")
    else:
        print("PASS: menu commands registered")

    if addon.DEFAULT_PORT != expected_port:
        failures.append(
            "addon DEFAULT_PORT is %s, expected %s (panel knob would disagree "
            "with the MCP server)" % (addon.DEFAULT_PORT, expected_port)
        )
    else:
        print("PASS: addon DEFAULT_PORT == %s" % expected_port)

    server = getattr(addon, "_global_server", None)
    try:
        if server is None or not server.running:
            failures.append("auto-start did not leave a running server")
        else:
            if server.port != expected_port:
                failures.append(
                    "auto-started on port %s, expected %s" % (server.port, expected_port)
                )
            else:
                print("PASS: auto-started listener on port %s" % server.port)

            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(3)
            try:
                probe.connect(("127.0.0.1", expected_port))
                print("PASS: port %s accepts connections" % expected_port)
            except OSError as exc:
                failures.append("could not connect to port %s: %s" % (expected_port, exc))
            finally:
                probe.close()
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass

    if failures:
        print("\n=== FAILURES ===")
        for item in failures:
            print(" - %s" % item)
        return 1

    print("\nNuke startup simulation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
