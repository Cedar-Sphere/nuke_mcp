"""Endpoint configuration and Nuke-startup integration.

These cover the pieces the local deployment depends on: the socket endpoint is
env-configurable on both sides, the addon imports cleanly during ``init.py``
(before ``nukescripts.PythonPanel`` exists), and the server can be started
without the panel UI.
"""

import contextlib
import importlib
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.test_addon_protocol import (
    FakeNukeModule,
    FakeNukescriptsModule,
    load_addon_module,
)


def load_server_module():
    if "nuke_mcp_server" in sys.modules:
        return importlib.reload(sys.modules["nuke_mcp_server"])
    return importlib.import_module("nuke_mcp_server")


ENDPOINT_ENV_VARS = ("NUKE_MCP_HOST", "NUKE_MCP_PORT")


class QuietOutputMixin(unittest.TestCase):
    """Keep expected start/stop diagnostics out of the test report."""

    def silence_output(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(redirect_stderr(io.StringIO()))


class ServerEndpointConfigTestCase(QuietOutputMixin):
    def setUp(self):
        self.server = load_server_module()
        self.addCleanup(importlib.reload, self.server)

    def _reload_with(self, env):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ENDPOINT_ENV_VARS:
                os.environ.pop(name, None)
            os.environ.update(env)
            return importlib.reload(self.server)

    def test_endpoint_defaults_when_env_is_unset(self):
        isolated = self._reload_with({})
        self.assertEqual(isolated.NUKE_HOST, "localhost")
        self.assertEqual(isolated.NUKE_PORT, 9876)

    def test_port_is_read_from_environment(self):
        isolated = self._reload_with({"NUKE_MCP_PORT": "9877"})
        self.assertEqual(isolated.NUKE_PORT, 9877)

    def test_host_is_read_from_environment(self):
        isolated = self._reload_with({"NUKE_MCP_HOST": "127.0.0.1"})
        self.assertEqual(isolated.NUKE_HOST, "127.0.0.1")

    def test_get_nuke_connection_uses_configured_endpoint(self):
        """The resolved endpoint must actually reach NukeConnection."""
        # Silence first so the reload rebinds the log handler to the buffer.
        self.silence_output()
        isolated = self._reload_with({"NUKE_MCP_PORT": "9999"})
        created = []

        class RecordingConnection(object):
            def __init__(self, host, port):
                created.append((host, port))

            def connect(self):
                return True

            def ping(self):
                return True

            def disconnect(self):
                pass

        with mock.patch.object(isolated, "_nuke_connection", None), mock.patch.object(
            isolated, "NukeConnection", RecordingConnection
        ):
            isolated.get_nuke_connection()

        self.assertEqual(created, [("localhost", 9999)])


class AddonImportTimeTestCase(unittest.TestCase):
    """The addon must import during init.py, before panel classes exist."""

    def test_addon_imports_without_nukescripts_available(self):
        fake_nuke = FakeNukeModule()
        with mock.patch.dict(sys.modules, {"nuke": fake_nuke}):
            sys.modules.pop("nukescripts", None)
            sys.modules.pop("nuke_mcp_addon", None)
            with mock.patch.object(sys, "path", list(sys.path)):
                addon = importlib.import_module("nuke_mcp_addon")
            self.assertTrue(hasattr(addon, "NukeMCPServer"))
        # Restore a normally-loaded module for the rest of the suite.
        load_addon_module()

    def test_addon_does_not_import_nukescripts_at_module_scope(self):
        addon = load_addon_module()
        self.assertFalse(
            hasattr(addon, "nukescripts"),
            "nukescripts must be imported lazily, not bound at module scope",
        )


class AddonEndpointConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.addon = load_addon_module()
        FakeNukeModule.reset()

    def _reload_addon_with(self, env):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ENDPOINT_ENV_VARS:
                os.environ.pop(name, None)
            os.environ.update(env)
            reloaded = load_addon_module()
            self.addCleanup(load_addon_module)
            return reloaded

    def test_default_port_matches_server_default(self):
        isolated = self._reload_addon_with({})
        self.assertEqual(isolated.DEFAULT_PORT, 9876)
        self.assertEqual(isolated.NukeMCPServer().port, 9876)

    def test_default_port_is_read_from_environment(self):
        isolated = self._reload_addon_with({"NUKE_MCP_PORT": "9877"})
        self.assertEqual(isolated.DEFAULT_PORT, 9877)
        self.assertEqual(isolated.NukeMCPServer().port, 9877)

    def test_explicit_port_overrides_environment(self):
        isolated = self._reload_addon_with({"NUKE_MCP_PORT": "9877"})
        self.assertEqual(isolated.NukeMCPServer(port=9123).port, 9123)


class EnsureServerRunningTestCase(QuietOutputMixin):
    """menu.py calls ensure_server_running() to start without the panel."""

    def setUp(self):
        self.addon = load_addon_module()
        FakeNukeModule.reset()
        self.addon._global_server = None
        # Silence before registering shutdown: cleanups run LIFO, so the
        # redirect must be registered first to still be active during teardown.
        self.silence_output()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        server = getattr(self.addon, "_global_server", None)
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass
        self.addon._global_server = None

    def test_starts_server_and_records_it_globally(self):
        server = self.addon.ensure_server_running(port=0)
        self.assertTrue(server.running)
        self.assertIs(self.addon._global_server, server)

    def test_is_idempotent_and_reuses_running_server(self):
        first = self.addon.ensure_server_running(port=0)
        second = self.addon.ensure_server_running(port=0)
        self.assertIs(first, second)

    def test_restarts_when_previous_server_stopped(self):
        first = self.addon.ensure_server_running(port=0)
        first.stop()
        second = self.addon.ensure_server_running(port=0)
        self.assertIsNot(first, second)
        self.assertTrue(second.running)

    def test_raises_when_bind_fails(self):
        with mock.patch.object(
            self.addon.NukeMCPServer, "start", return_value=False
        ):
            with self.assertRaises(RuntimeError):
                self.addon.ensure_server_running(port=0)
        self.assertIsNone(self.addon._global_server)


class LazyPanelTestCase(unittest.TestCase):
    def setUp(self):
        self.addon = load_addon_module()
        FakeNukeModule.reset()
        self.addon._panel = None
        self.addCleanup(setattr, self.addon, "_panel", None)

    def test_panel_base_resolves_from_nukescripts_at_call_time(self):
        with mock.patch.dict(
            sys.modules, {"nukescripts": FakeNukescriptsModule()}
        ):
            base = self.addon._python_panel_base()
        self.assertIs(base, FakeNukescriptsModule.PythonPanel)

    def test_panel_base_falls_back_to_nukescripts_panels(self):
        class PanelsSubmodule(object):
            PythonPanel = FakeNukescriptsModule.PythonPanel

        class NukescriptsWithoutPanel(object):
            panels = PanelsSubmodule()

        with mock.patch.dict(
            sys.modules,
            {
                "nukescripts": NukescriptsWithoutPanel(),
                "nukescripts.panels": PanelsSubmodule(),
            },
        ):
            base = self.addon._python_panel_base()
        self.assertIs(base, FakeNukescriptsModule.PythonPanel)

    def test_panel_base_raises_actionable_error_when_unavailable(self):
        class EmptyNukescripts(object):
            pass

        with mock.patch.dict(sys.modules, {"nukescripts": EmptyNukescripts()}):
            with self.assertRaises(AttributeError) as ctx:
                self.addon._python_panel_base()
        self.assertIn("menu.py", str(ctx.exception))

    def test_make_panel_class_subclasses_resolved_base(self):
        with mock.patch.dict(
            sys.modules, {"nukescripts": FakeNukescriptsModule()}
        ):
            panel_class = self.addon._make_panel_class()
        self.assertTrue(issubclass(panel_class, FakeNukescriptsModule.PythonPanel))

    def test_show_panel_builds_class_once_and_reuses_panel(self):
        builds = []

        class StubPanel(object):
            def __init__(self):
                builds.append(self)

            def show(self):
                pass

        def fake_make_panel_class():
            return StubPanel

        with mock.patch.object(
            self.addon, "_make_panel_class", fake_make_panel_class
        ):
            self.addon.show_panel()
            first = self.addon._panel
            self.addon.show_panel()

        self.assertEqual(len(builds), 1)
        self.assertIs(self.addon._panel, first)


if __name__ == "__main__":
    unittest.main()
