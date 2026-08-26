import importlib
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock


def load_server_module():
    if "nuke_mcp_server" in sys.modules:
        return importlib.reload(sys.modules["nuke_mcp_server"])
    return importlib.import_module("nuke_mcp_server")


class LocalResponderServer(object):
    def __init__(self, handler):
        self.handler = handler
        self.accept_count = 0
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("127.0.0.1", 0))
        self._server_sock.listen(5)
        self.port = self._server_sock.getsockname()[1]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._responders = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                self._server_sock.settimeout(0.2)
                client, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.accept_count += 1
                responder = ResponderThread(client, self.handler)
                self._responders.append(responder)

    def close_all_clients(self):
        with self._lock:
            for responder in self._responders:
                responder.stop()
            self._responders.clear()

    def stop(self):
        self._stop.set()
        self.close_all_clients()
        try:
            self._server_sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def is_peer_gone_error(exc):
    """True when a socket error just means the other end ended the session."""
    if isinstance(
        exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
    ):
        return True
    # WSAECONNABORTED / WSAECONNRESET / WSAESHUTDOWN
    return getattr(exc, "winerror", None) in (10053, 10054, 10058)


class ResponderThread(object):
    def __init__(self, peer_sock, handler):
        self.peer_sock = peer_sock
        self.handler = handler
        self.received = []
        self.error = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _handle_socket_error(self, exc):
        """Return True to end the session quietly, False to let it surface."""
        if self._stop.is_set() or is_peer_gone_error(exc):
            return True
        self.error = exc
        return False

    def _run(self):
        buffer = b""
        try:
            while not self._stop.is_set():
                try:
                    self.peer_sock.settimeout(0.1)
                    chunk = self.peer_sock.recv(8192)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if self._handle_socket_error(exc):
                        break
                    raise
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    request = json.loads(line.decode("utf-8"))
                    self.received.append(request)
                    response = self.handler(request)
                    if response is not None:
                        payload = json.dumps(
                            response, separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8") + b"\n"
                        try:
                            self.peer_sock.sendall(payload)
                        except OSError as exc:
                            if self._handle_socket_error(exc):
                                return
                            raise
        finally:
            try:
                self.peer_sock.close()
            except OSError:
                pass

    def stop(self):
        """Signal the responder and join it. Returns True when it exited."""
        self._stop.set()
        self.thread.join(timeout=2.0)
        return not self.thread.is_alive()


class ResponderThreadHygieneTestCase(unittest.TestCase):
    """Pins the suppression boundary of the test responder helper."""

    def setUp(self):
        self.thread_exceptions = []
        self._saved_excepthook = threading.excepthook
        threading.excepthook = self.thread_exceptions.append

    def tearDown(self):
        threading.excepthook = self._saved_excepthook

    def test_intentional_stop_before_close_is_silent_and_joins(self):
        client_sock, peer_sock = socket.socketpair()
        try:
            responder = ResponderThread(peer_sock, lambda request: None)
            time.sleep(0.15)
            self.assertTrue(responder.stop())
        finally:
            client_sock.close()
            peer_sock.close()

        time.sleep(0.15)
        self.assertEqual(self.thread_exceptions, [])
        self.assertIsNone(responder.error)

    def test_peer_disconnect_ends_session_quietly(self):
        client_sock, peer_sock = socket.socketpair()
        responder = ResponderThread(peer_sock, lambda request: None)
        try:
            client_sock.close()
            time.sleep(0.15)
        finally:
            responder.stop()
            peer_sock.close()

        self.assertEqual(self.thread_exceptions, [])
        self.assertIsNone(responder.error)

    def test_unexpected_socket_error_is_not_suppressed(self):
        client_sock, peer_sock = socket.socketpair()
        responder = ResponderThread(peer_sock, lambda request: None)
        try:
            # Close the socket the thread is polling without stopping it
            # first: that is a harness bug, not an orderly shutdown, so it
            # must stay visible.
            peer_sock.close()
            responder.thread.join(timeout=2.0)
        finally:
            client_sock.close()

        self.assertFalse(responder.thread.is_alive())
        self.assertEqual(len(self.thread_exceptions), 1)
        self.assertIsInstance(responder.error, OSError)
        self.assertFalse(is_peer_gone_error(responder.error))

    def test_peer_gone_classification(self):
        self.assertTrue(is_peer_gone_error(ConnectionResetError()))
        self.assertTrue(is_peer_gone_error(ConnectionAbortedError()))
        self.assertTrue(is_peer_gone_error(BrokenPipeError()))
        self.assertFalse(is_peer_gone_error(OSError("not a socket")))


class ConnectionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server_module()

    def setUp(self):
        self.server._nuke_connection = None

        # Routine INFO chatter from send_command drowns out real failures in
        # this file. Quieted locally only; production defaults are untouched
        # and tests/test_logging.py still exercises the real configuration.
        self._saved_log_level = self.server.logger.level
        self.server.logger.setLevel(logging.CRITICAL)

        # Surface any exception that escapes a helper thread as a test failure
        # instead of an interleaved traceback on stderr.
        self._thread_exceptions = []
        self._saved_excepthook = threading.excepthook
        threading.excepthook = self._record_thread_exception

        self._responders = []
        client_sock, peer_sock = socket.socketpair()
        client_sock.setblocking(True)
        peer_sock.setblocking(True)
        self.client_sock = client_sock
        self.peer_sock = peer_sock
        self.conn = self.server.NukeConnection(host="localhost", port=9876)
        self.conn.sock = client_sock
        self.conn._receive_buffer = b""

    def _record_thread_exception(self, args):
        self._thread_exceptions.append(args)

    def start_responder(self, handler):
        """Start a responder that is stopped and joined during teardown."""
        responder = ResponderThread(self.peer_sock, handler)
        self._responders.append(responder)
        return responder

    def tearDown(self):
        # Stop responders *before* closing sockets, so teardown never yanks a
        # socket out from under a thread that is still polling it.
        unjoined = [
            responder for responder in self._responders if not responder.stop()
        ]
        try:
            self.client_sock.close()
        except OSError:
            pass
        try:
            self.peer_sock.close()
        except OSError:
            pass
        self.server._nuke_connection = None

        threading.excepthook = self._saved_excepthook
        self.server.logger.setLevel(self._saved_log_level)

        self.assertEqual(unjoined, [], "responder thread failed to join")
        self.assertEqual(
            [str(args.exc_value) for args in self._thread_exceptions],
            [],
            "unhandled exception escaped a helper thread",
        )

    def _default_handler(self, request):
        return {
            "id": request["id"],
            "status": "success",
            "result": {"echo": request["type"]},
        }

    def _responder_handler(self, request):
        if request["type"] == "ping":
            result = {"pong": True}
        else:
            result = {"echo": request["type"]}
        return {
            "id": request["id"],
            "status": "success",
            "result": result,
        }

    def test_outgoing_request_has_nonempty_id_and_trailing_newline(self):
        captured = []

        def handler(request):
            captured.append(request)
            return self._default_handler(request)

        self.start_responder(handler)
        self.conn.send_command("get_script_info")
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertTrue(request["id"])
        self.assertEqual(request["type"], "get_script_info")
        self.assertEqual(request["params"], {})

    def test_partial_response_reads(self):
        response_event = threading.Event()
        sent = {"done": False}

        def handler(request):
            response = self._default_handler(request)
            payload = json.dumps(
                response, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8") + b"\n"
            half = max(1, len(payload) // 2)
            self.peer_sock.sendall(payload[:half])
            time.sleep(0.05)
            self.peer_sock.sendall(payload[half:])
            sent["done"] = True
            response_event.set()
            return None

        self.start_responder(handler)
        result = self.conn.send_command("ping")
        self.assertTrue(response_event.wait(timeout=2.0))
        self.assertEqual(result, {"echo": "ping"})

    def test_response_id_mismatch_raises_protocol_error_and_invalidates_socket(self):
        def handler(request):
            return {
                "id": "wrong-id",
                "status": "success",
                "result": {},
            }

        self.start_responder(handler)
        with self.assertRaises(self.server.ProtocolError):
            self.conn.send_command("ping")
        self.assertIsNone(self.conn.sock)
        self.assertEqual(self.conn._receive_buffer, b"")

    def test_malformed_response_invalidates_socket(self):
        def handler(request):
            self.peer_sock.sendall(b"not-json\n")
            return None

        self.start_responder(handler)
        with self.assertRaises(self.server.ProtocolError):
            self.conn.send_command("ping")
        self.assertIsNone(self.conn.sock)
        self.assertEqual(self.conn._receive_buffer, b"")

    def test_oversized_response_invalidates_socket(self):
        def handler(request):
            oversized = b"x" * (self.server.MAX_MESSAGE_BYTES + 1) + b"\n"
            self.peer_sock.sendall(oversized)
            return None

        self.start_responder(handler)
        with self.assertRaises(self.server.ProtocolError):
            self.conn.send_command("ping")
        self.assertIsNone(self.conn.sock)
        self.assertEqual(self.conn._receive_buffer, b"")

    def test_addon_error_envelope_raises_nuke_command_error(self):
        def handler(request):
            return {
                "id": request["id"],
                "status": "error",
                "error": {"type": "ValueError", "message": "boom"},
            }

        self.start_responder(handler)
        with self.assertRaises(self.server.NukeCommandError) as ctx:
            self.conn.send_command("get_script_info")
        self.assertEqual(ctx.exception.error_type, "ValueError")
        self.assertEqual(ctx.exception.message, "boom")

    def test_ping_sends_ping_type_not_get_script_info(self):
        captured = []

        def handler(request):
            captured.append(request["type"])
            return {
                "id": request["id"],
                "status": "success",
                "result": {"pong": True},
            }

        self.start_responder(handler)
        self.assertTrue(self.conn.ping())
        self.assertEqual(captured, ["ping"])

    def test_concurrent_calls_do_not_interleave(self):
        active = {"count": 0}
        max_active = {"value": 0}
        gate = threading.Event()
        results = {}
        errors = {}

        def handler(request):
            active["count"] += 1
            max_active["value"] = max(max_active["value"], active["count"])
            time.sleep(0.05)
            active["count"] -= 1
            return {
                "id": request["id"],
                "status": "success",
                "result": {"tag": request["params"]["tag"]},
            }

        self.start_responder(handler)

        def worker(tag):
            gate.wait(timeout=2.0)
            try:
                results[tag] = self.conn.send_command(
                    "get_script_info", {"tag": tag}
                )
            except Exception as exc:
                errors[tag] = exc

        threads = [
            threading.Thread(target=worker, args=(tag,), daemon=True)
            for tag in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(timeout=5.0)

        self.assertEqual(errors, {})
        self.assertEqual(results["a"]["tag"], "a")
        self.assertEqual(results["b"]["tag"], "b")
        self.assertEqual(max_active["value"], 1)

    def test_explicit_timeout_is_applied(self):
        def handler(request):
            time.sleep(0.3)
            return self._default_handler(request)

        self.start_responder(handler)
        with self.assertRaises(TimeoutError):
            self.conn.send_command("ping", timeout=0.05)
        self.assertIsNone(self.conn.sock)

    def test_default_timeout_uses_command_timeout(self):
        class TrackingSocket(object):
            def __init__(self, inner):
                self._inner = inner
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)
                return self._inner.settimeout(value)

            def sendall(self, data):
                return self._inner.sendall(data)

            def recv(self, size):
                return self._inner.recv(size)

            def close(self):
                return self._inner.close()

        tracking_sock = TrackingSocket(self.client_sock)
        self.conn.sock = tracking_sock
        self.start_responder(self._default_handler)
        self.conn.send_command("get_script_info")
        self.assertIn(self.server.COMMAND_TIMEOUT_SECONDS, tracking_sock.timeouts)

    def test_timeout_constants_match_configured_values(self):
        expected = {
            "RENDER_TIMEOUT_SECONDS": ("NUKE_MCP_RENDER_TIMEOUT", "3600"),
            "COMMAND_TIMEOUT_SECONDS": ("NUKE_MCP_COMMAND_TIMEOUT", "30"),
            "CONNECT_TIMEOUT_SECONDS": ("NUKE_MCP_CONNECT_TIMEOUT", "5"),
        }
        for constant, (env_var, default) in expected.items():
            with self.subTest(constant=constant):
                self.assertEqual(
                    getattr(self.server, constant),
                    float(os.environ.get(env_var, default)),
                )

    def test_timeout_constant_defaults_in_isolated_environment(self):
        env_vars = (
            "NUKE_MCP_RENDER_TIMEOUT",
            "NUKE_MCP_COMMAND_TIMEOUT",
            "NUKE_MCP_CONNECT_TIMEOUT",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            for env_var in env_vars:
                os.environ.pop(env_var, None)
            isolated = importlib.reload(self.server)
            try:
                self.assertEqual(isolated.RENDER_TIMEOUT_SECONDS, 3600.0)
                self.assertEqual(isolated.COMMAND_TIMEOUT_SECONDS, 30.0)
                self.assertEqual(isolated.CONNECT_TIMEOUT_SECONDS, 5.0)
            finally:
                importlib.reload(self.server)

    def test_render_tool_passes_render_timeout(self):
        conn = mock.Mock()
        conn.send_command.return_value = {"status": "done"}
        with mock.patch.object(self.server, "get_nuke_connection", return_value=conn):
            self.server.render(mock.Mock(), frame_range="1-1")
        conn.send_command.assert_called_once_with(
            "render",
            {
                "frame_range": "1-1",
                "write_node": None,
                "proxy_mode": False,
            },
            timeout=self.server.RENDER_TIMEOUT_SECONDS,
        )

    def test_closed_peer_invalidates_socket_and_get_nuke_connection_reconnects(self):
        addon_server = LocalResponderServer(self._responder_handler)
        dead_conn = None
        try:
            dead_conn = self.server.NukeConnection(
                host="127.0.0.1", port=addon_server.port
            )
            self.assertTrue(dead_conn.connect())
            self.assertTrue(dead_conn.ping())
            self.assertEqual(addon_server.accept_count, 1)

            self.server._nuke_connection = dead_conn
            first_sock = dead_conn.sock

            addon_server.close_all_clients()
            with self.assertRaises(ConnectionError):
                dead_conn.send_command("ping")
            self.assertIsNone(dead_conn.sock)

            reconnected = self.server.get_nuke_connection()
            self.assertIs(reconnected, dead_conn)
            self.assertIsNotNone(reconnected.sock)
            self.assertIsNot(reconnected.sock, first_sock)
            self.assertGreaterEqual(addon_server.accept_count, 2)
            self.assertTrue(reconnected.ping())
            self.assertEqual(
                reconnected.send_command("get_script_info"),
                {"echo": "get_script_info"},
            )
        finally:
            if dead_conn is not None:
                dead_conn.disconnect()
            addon_server.stop()
            self.server._nuke_connection = None

    def test_get_nuke_connection_uses_ping_not_get_script_info(self):
        conn = mock.Mock()
        conn.ping.return_value = True
        self.server._nuke_connection = conn
        result = self.server.get_nuke_connection()
        self.assertIs(result, conn)
        conn.ping.assert_called_once_with()
        conn.send_command.assert_not_called()

    def test_get_nuke_connection_serializes_concurrent_acquisition(self):
        created = []

        class FakeConnection(object):
            def __init__(self, host=None, port=None, ping_result=True,
                         ping_delay=0.0):
                self.host = host
                self.port = port
                self.sock = object()
                self.ping_result = ping_result
                self.ping_delay = ping_delay
                self.disconnected = False

            def connect(self):
                self.sock = object()
                return True

            def ping(self):
                if self.ping_delay:
                    time.sleep(self.ping_delay)
                return self.ping_result

            def disconnect(self):
                self.disconnected = True
                self.sock = None

        def factory(host=None, port=None):
            conn = FakeConnection(host=host, port=port)
            created.append(conn)
            return conn

        stale = FakeConnection(ping_result=False, ping_delay=0.25)
        self.server._nuke_connection = stale

        results = {}
        errors = {}
        barrier = threading.Barrier(2, timeout=5.0)

        def worker(tag):
            try:
                barrier.wait()
                results[tag] = self.server.get_nuke_connection()
            except Exception as exc:  # pragma: no cover - failure diagnostics
                errors[tag] = exc

        with mock.patch.object(self.server, "NukeConnection", factory):
            threads = [
                threading.Thread(target=worker, args=(tag,), daemon=True)
                for tag in ("a", "b")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

        self.assertEqual(errors, {})
        self.assertEqual(len(created), 1)
        self.assertIs(results["a"], results["b"])
        self.assertIs(results["a"], created[0])

    def test_execute_nuke_code_reports_handler_failure(self):
        conn = mock.Mock()
        conn.send_command.return_value = {
            "executed": False,
            "error": "NameError: undefined_symbol",
        }
        with mock.patch.object(
            self.server, "get_nuke_connection", return_value=conn
        ):
            result = self.server.execute_nuke_code(mock.Mock(), "undefined_symbol")
        self.assertIn("failed", result.lower())
        self.assertIn("undefined_symbol", result)
        self.assertNotIn("successfully", result.lower())

    def test_execute_nuke_code_reports_command_error(self):
        conn = mock.Mock()
        conn.send_command.side_effect = self.server.NukeCommandError(
            "RuntimeError", "nuke exploded"
        )
        with mock.patch.object(
            self.server, "get_nuke_connection", return_value=conn
        ):
            result = self.server.execute_nuke_code(mock.Mock(), "print(1)")
        self.assertIn("RuntimeError", result)
        self.assertIn("nuke exploded", result)
        self.assertNotIn("successfully", result.lower())

    def test_get_script_info_tool_reports_command_error(self):
        conn = mock.Mock()
        conn.send_command.side_effect = self.server.NukeCommandError(
            "RuntimeError", "no script open"
        )
        with mock.patch.object(
            self.server, "get_nuke_connection", return_value=conn
        ):
            result = self.server.get_script_info(mock.Mock())
        self.assertIn("RuntimeError", result)
        self.assertIn("no script open", result)
        self.assertNotIn("Total Nodes", result)

    def test_max_message_bytes_constant(self):
        self.assertEqual(self.server.MAX_MESSAGE_BYTES, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
