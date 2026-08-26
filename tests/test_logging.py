import importlib
import io
import logging
import os
import contextlib
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def reload_server_module():
  if "nuke_mcp_server" in sys.modules:
    del sys.modules["nuke_mcp_server"]
  return importlib.import_module("nuke_mcp_server")


def reload_main_module():
  if "main" in sys.modules:
    del sys.modules["main"]
  return importlib.import_module("main")


@contextlib.contextmanager
def captured_server_logs(server, level="INFO"):
  """Capture what the server logger's handler actually writes.

  Two things have to be true for a record to reach the buffer, and both are
  established here rather than assumed:

  * ``configure_logging()`` binds the StreamHandler to whatever ``sys.stderr``
    is at call time, so wrapping an already-configured module in
    ``redirect_stderr`` captures nothing. Re-running the production
    configuration *inside* the redirect rebinds the existing handler to the
    buffer.
  * The configured level must admit the records under test. The caller's
    ``NUKE_MCP_LOG_LEVEL`` is overridden for the duration of the context only,
    so an operator running the suite with ``WARNING`` does not silently turn
    INFO-level positive controls into vacuous passes.

  Both the environment override and the redirect unwind on exit, and the
  ``finally`` then re-runs the production configuration so the real handler
  stream and the caller's level are restored even if the body raises.
  Production code is untouched; this only exercises the idempotent
  reconfiguration path the module already supports.
  """
  buffer = io.StringIO()
  try:
    with redirect_stderr(buffer), mock.patch.dict(
      os.environ, {"NUKE_MCP_LOG_LEVEL": level}
    ):
      server.configure_logging()
      yield buffer
  finally:
    server.configure_logging()


class LoggingTestCase(unittest.TestCase):
  def setUp(self):
    self._saved_modules = {}
    for name in ("nuke_mcp_server", "main"):
      if name in sys.modules:
        self._saved_modules[name] = sys.modules.pop(name)

    self._responders = []
    self._tracked_sockets = []
    self._thread_exceptions = []
    self._saved_excepthook = threading.excepthook
    threading.excepthook = self._thread_exceptions.append

  def _start_responder(self, peer_sock, handler):
    """Start a responder that is stopped and joined during teardown."""
    from tests.test_connection import ResponderThread
    responder = ResponderThread(peer_sock, handler)
    self._responders.append(responder)
    return responder

  def _track_sockets(self, *socks):
    self._tracked_sockets.extend(socks)

  def tearDown(self):
    # Stop responders before closing their sockets, so an orderly teardown
    # never looks like a socket fault to a still-polling thread.
    unjoined = [r for r in self._responders if not r.stop()]
    for sock in self._tracked_sockets:
      try:
        sock.close()
      except OSError:
        pass

    for name, module in self._saved_modules.items():
      sys.modules[name] = module

    threading.excepthook = self._saved_excepthook

    self.assertEqual(unjoined, [], "responder thread failed to join")
    self.assertEqual(
      [str(args.exc_value) for args in self._thread_exceptions],
      [],
      "unhandled exception escaped a helper thread",
    )

  def test_import_server_module_produces_no_stdout(self):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
      server = reload_server_module()
    self.assertEqual(stdout.getvalue(), "")
    self.assertIsNotNone(server)

  def test_server_logger_record_goes_to_stderr_not_stdout(self):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
      server = reload_server_module()
      with captured_server_logs(server) as stderr:
        server.logger.info("probe-message-12345")
    self.assertEqual(stdout.getvalue(), "")
    self.assertIn("probe-message-12345", stderr.getvalue())

  def test_nuke_mcp_log_level_controls_configured_level(self):
    with mock.patch.dict(os.environ, {"NUKE_MCP_LOG_LEVEL": "WARNING"}, clear=False):
      server = reload_server_module()
    self.assertEqual(server.logger.level, logging.WARNING)
    handlers = server.logger.handlers
    self.assertTrue(handlers, "expected at least one configured log handler")
    for handler in handlers:
      self.assertEqual(handler.level, logging.WARNING)

  def test_import_main_module_produces_no_stdout(self):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
      main_mod = reload_main_module()
    self.assertEqual(stdout.getvalue(), "")
    self.assertIsNotNone(main_mod)

  def test_main_script_import_failure_exits_nonzero_without_input(self):
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    helper = (
      "import sys\n"
      "sys.path.insert(0, r'%s')\n"
      "import builtins\n"
      "_real_import = builtins.__import__\n"
      "def _broken(name, globals=None, locals=None, fromlist=(), level=0):\n"
      "    if name == 'nuke_mcp_server':\n"
      "        raise ImportError('simulated import failure')\n"
      "    return _real_import(name, globals, locals, fromlist, level)\n"
      "builtins.__import__ = _broken\n"
      "import runpy\n"
      "try:\n"
      "    runpy.run_module('main', run_name='__main__', alter_sys=True)\n"
      "except SystemExit as exc:\n"
      "    sys.exit(exc.code if exc.code is not None else 1)\n"
      "except Exception:\n"
      "    sys.exit(1)\n"
    ) % REPO_ROOT
    result = subprocess.run(
      [sys.executable, "-c", helper],
      cwd=REPO_ROOT,
      env=env,
      capture_output=True,
      text=True,
      timeout=10,
      stdin=subprocess.DEVNULL,
    )
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(result.stdout, "")
    self.assertNotIn("Press Enter", result.stdout + result.stderr)

  def test_main_script_fatal_startup_exits_nonzero_without_input(self):
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    result = subprocess.run(
      [
        sys.executable,
        os.path.join(REPO_ROOT, "main.py"),
      ],
      cwd=REPO_ROOT,
      env=env,
      capture_output=True,
      text=True,
      timeout=10,
      stdin=subprocess.DEVNULL,
    )
    # main.py calls mcp.run() which may block or fail without MCP stdio;
    # we only require no stdout startup prints and no input() prompt.
    self.assertEqual(result.stdout, "")
    self.assertNotIn("Press Enter", result.stderr + result.stdout)

  def test_send_command_logs_do_not_include_full_params(self):
    server = reload_server_module()
    client_sock, peer_sock = __import__("socket").socketpair()
    self._track_sockets(client_sock, peer_sock)
    conn = server.NukeConnection(host="localhost", port=9876)
    conn.sock = client_sock
    conn._receive_buffer = b""

    secret_params = {
      "parameters": {"file": "/secret/path/plate.exr", "label": "SECRET_LABEL"},
      "name": "Read1",
    }

    def handler(request):
      import json
      response = {
        "id": request["id"],
        "status": "success",
        "result": {"echo": True},
      }
      payload = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
      peer_sock.sendall(payload)
      return None

    self._start_responder(peer_sock, handler)

    with captured_server_logs(server) as stderr:
      conn.send_command("create_node", secret_params)

    log_text = stderr.getvalue()
    # Positive control: prove the handler stream was actually captured, so the
    # redaction assertions below cannot pass vacuously on an empty buffer.
    self.assertIn("Sending command: create_node", log_text)
    self.assertNotIn("/secret/path/plate.exr", log_text)
    self.assertNotIn("SECRET_LABEL", log_text)
    self.assertNotIn('"parameters"', log_text)

  def test_execute_nuke_code_logs_do_not_include_source(self):
    server = reload_server_module()
    client_sock, peer_sock = __import__("socket").socketpair()
    self._track_sockets(client_sock, peer_sock)
    conn = server.NukeConnection(host="localhost", port=9876)
    conn.sock = client_sock
    conn._receive_buffer = b""

    secret_code = "import os\nSECRET_TOKEN = 'do-not-log-this'\noutput = {'ok': True}"

    def handler(request):
      import json
      response = {
        "id": request["id"],
        "status": "success",
        "result": {"executed": True, "output": {}},
      }
      payload = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
      peer_sock.sendall(payload)
      return None

    self._start_responder(peer_sock, handler)

    with captured_server_logs(server) as stderr:
      conn.send_command("execute_code", {"code": secret_code})

    log_text = stderr.getvalue()
    # Positive control: prove the handler stream was actually captured, so the
    # redaction assertions below cannot pass vacuously on an empty buffer.
    self.assertIn("Sending command: execute_code", log_text)
    self.assertNotIn("do-not-log-this", log_text)
    self.assertNotIn("SECRET_TOKEN", log_text)
    self.assertNotIn(secret_code, log_text)

  def test_nuke_command_error_details_available_for_tool_formatting(self):
    server = reload_server_module()
    err = server.NukeCommandError("ValueError", "node missing")
    self.assertEqual(err.error_type, "ValueError")
    self.assertEqual(err.message, "node missing")
    self.assertEqual(str(err), "ValueError: node missing")

    client_sock, peer_sock = __import__("socket").socketpair()
    self._track_sockets(client_sock, peer_sock)
    conn = server.NukeConnection(host="localhost", port=9876)
    conn.sock = client_sock
    conn._receive_buffer = b""

    def handler(request):
      import json
      response = {
        "id": request["id"],
        "status": "error",
        "error": {"type": "ValueError", "message": "node missing"},
      }
      payload = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
      peer_sock.sendall(payload)
      return None

    self._start_responder(peer_sock, handler)

    # This test deliberately provokes a command failure; capture the expected
    # diagnostics and assert on them instead of letting them escape to stderr.
    with captured_server_logs(server) as stderr:
      with self.assertRaises(server.NukeCommandError) as ctx:
        conn.send_command("get_node_info", {"name": "Foo"})
      self.assertEqual(ctx.exception.error_type, "ValueError")
      self.assertEqual(ctx.exception.message, "node missing")

      with mock.patch.object(server, "get_nuke_connection", return_value=conn):
        result = server.get_node_info(mock.Mock(), "Foo")

    self.assertIn("ValueError", result)
    self.assertIn("node missing", result)
    self.assertNotIn("Foo", result)

    log_text = stderr.getvalue()
    self.assertIn("Command failed: get_node_info", log_text)
    self.assertIn("Error in get_node_info: NukeCommandError", log_text)
    # The failure log names the exception class, never the addon's message.
    self.assertNotIn("node missing", log_text)

  def test_captured_server_logs_forces_info_then_restores_caller_config(self):
    server = reload_server_module()
    self.addCleanup(server.configure_logging)

    with mock.patch.dict(os.environ, {"NUKE_MCP_LOG_LEVEL": "WARNING"}):
      server.configure_logging()
      self.assertEqual(server.logger.level, logging.WARNING)

      with captured_server_logs(server) as buffer:
        # Forced to INFO for the duration of the context only, so INFO-level
        # positive controls stay meaningful under a WARNING environment.
        self.assertEqual(os.environ["NUKE_MCP_LOG_LEVEL"], "INFO")
        self.assertEqual(server.logger.level, logging.INFO)
        self.assertIs(server.logger.handlers[0].stream, buffer)
        server.logger.info("inside-probe-98765")

      self.assertIn("inside-probe-98765", buffer.getvalue())

      # Caller's environment, level and handler stream are all restored.
      self.assertEqual(os.environ["NUKE_MCP_LOG_LEVEL"], "WARNING")
      self.assertEqual(server.logger.level, logging.WARNING)
      self.assertIs(server.logger.handlers[0].stream, sys.stderr)

  def test_captured_server_logs_restores_configuration_after_exception(self):
    server = reload_server_module()
    self.addCleanup(server.configure_logging)

    with mock.patch.dict(os.environ, {"NUKE_MCP_LOG_LEVEL": "WARNING"}):
      server.configure_logging()

      with self.assertRaises(RuntimeError):
        with captured_server_logs(server):
          raise RuntimeError("boom inside capture")

      self.assertEqual(os.environ["NUKE_MCP_LOG_LEVEL"], "WARNING")
      self.assertEqual(server.logger.level, logging.WARNING)
      self.assertIs(server.logger.handlers[0].stream, sys.stderr)

  def test_captured_server_logs_honors_explicit_level(self):
    server = reload_server_module()
    self.addCleanup(server.configure_logging)

    with captured_server_logs(server, level="ERROR") as buffer:
      self.assertEqual(server.logger.level, logging.ERROR)
      server.logger.info("filtered-info-probe")
      server.logger.error("kept-error-probe")

    self.assertNotIn("filtered-info-probe", buffer.getvalue())
    self.assertIn("kept-error-probe", buffer.getvalue())

  def test_reload_does_not_duplicate_log_handlers(self):
    server = reload_server_module()
    first_count = len(server.logger.handlers)
    self.assertGreater(first_count, 0)
    reloaded = reload_server_module()
    self.assertEqual(len(reloaded.logger.handlers), first_count)


if __name__ == "__main__":
  unittest.main()
