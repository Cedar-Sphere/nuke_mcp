"""Cross-check a deployed addon against a deployed MCP server over a real socket.

Stands in a minimal fake ``nuke`` module for the addon, starts its listener,
then drives it with the deployed MCP server's own NukeConnection. This catches
wire-protocol drift between the two installed halves without needing Nuke.

Usage:
    python scripts/verify_deployment.py <addon_dir> <server_dir> [port]
"""

import importlib
import os
import sys
import types


def build_fake_nuke():
    nuke = types.ModuleType("nuke")

    def execute_in_main_thread_with_result(func, kwargs=None):
        return func(**(kwargs or {}))

    class FakeKnob(object):
        def __init__(self, value):
            self._value = value

        def value(self):
            return self._value

        def setValue(self, value):
            self._value = value

    class FakeFormat(object):
        def __str__(self):
            return "1920 1080 0 0 1920 1080 1 HD_1080"

    class FakeRoot(object):
        _knobs = {"first_frame": FakeKnob(1), "last_frame": FakeKnob(10)}

        def name(self):
            return "verify.nk"

        def fps(self):
            return 24.0

        def format(self):
            return FakeFormat()

        def firstFrame(self):
            return 1

        def lastFrame(self):
            return 10

        def frame(self):
            return 1

        def __getitem__(self, key):
            return FakeRoot._knobs[key]

    nuke.executeInMainThreadWithResult = execute_in_main_thread_with_result
    nuke.executeInMainThread = lambda func, kwargs=None: func(**(kwargs or {}))
    nuke.root = FakeRoot
    nuke.allNodes = lambda *a, **k: []
    nuke.toNode = lambda name: None
    nuke.menu = lambda name: types.SimpleNamespace(addCommand=lambda *a, **k: None)
    nuke.toolbar = lambda name: types.SimpleNamespace(addCommand=lambda *a, **k: None)
    nuke.NUKE_VERSION_STRING = "15.0v1 (fake)"
    return nuke


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    addon_dir, server_dir = sys.argv[1], sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 9876

    os.environ["NUKE_MCP_PORT"] = str(port)
    os.environ["NUKE_MCP_HOST"] = "127.0.0.1"

    sys.modules["nuke"] = build_fake_nuke()

    sys.path.insert(0, addon_dir)
    addon_mod = importlib.import_module("nuke_mcp_addon")
    assert os.path.dirname(addon_mod.__file__) == os.path.abspath(addon_dir), (
        "imported the wrong addon: %s" % addon_mod.__file__
    )
    print("addon      : %s" % addon_mod.__file__)

    server = addon_mod.NukeMCPServer(host="127.0.0.1", port=port)
    if not server.start():
        print("FAIL: addon listener did not start")
        return 1

    failures = []
    try:
        sys.path.insert(0, server_dir)
        for name in list(sys.modules):
            if name == "nuke_mcp_server":
                del sys.modules[name]
        server_mod = importlib.import_module("nuke_mcp_server")
        assert os.path.dirname(server_mod.__file__) == os.path.abspath(server_dir), (
            "imported the wrong server: %s" % server_mod.__file__
        )
        print("mcp server : %s" % server_mod.__file__)
        print("endpoint   : %s:%s" % (server_mod.NUKE_HOST, server_mod.NUKE_PORT))

        if server_mod.NUKE_PORT != port:
            failures.append("server resolved port %s, expected %s" % (server_mod.NUKE_PORT, port))

        conn = server_mod.NukeConnection(host="127.0.0.1", port=port)
        if not conn.connect():
            failures.append("MCP server could not connect to addon")
        else:
            # 1. Health check must round-trip.
            if not conn.ping():
                failures.append("ping failed across the deployed pair")
            else:
                print("PASS: ping round-trip")

            # 2. A real command must return a parsed result.
            info = conn.send_command("get_script_info")
            if not isinstance(info, dict) or "name" not in info:
                failures.append("get_script_info returned unexpected payload: %r" % (info,))
            else:
                print("PASS: get_script_info -> %s" % info.get("name"))

            # 3. A failing command must surface as a structured command error,
            #    not a dispatch error or a hang.
            print("(the traceback below is expected: deliberate failing command)")
            try:
                conn.send_command("modify_node", {"name": "does_not_exist_xyz"})
                failures.append("expected a failure for a nonexistent node")
            except server_mod.NukeCommandError as exc:
                print("PASS: error envelope -> %s" % exc)
            except Exception as exc:
                failures.append(
                    "failing command raised %s instead of NukeCommandError: %s"
                    % (type(exc).__name__, exc)
                )

            conn.disconnect()
    finally:
        server.stop()

    if failures:
        print("\n=== FAILURES ===")
        for item in failures:
            print(" - %s" % item)
        return 1

    print("\nAll deployment checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
