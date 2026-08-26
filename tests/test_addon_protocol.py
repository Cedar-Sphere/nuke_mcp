import contextlib
import importlib
import io
import json
import socket
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


@contextlib.contextmanager
def captured_addon_output():
    """Capture the addon's print/traceback diagnostics for assertion."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        yield stdout, stderr


class FakeMenu(object):
    def addCommand(self, *args, **kwargs):
        pass


class FakeNukeModule(object):
    dispatched_calls = []

    @classmethod
    def reset(cls):
        cls.dispatched_calls = []

    @staticmethod
    def executeInMainThreadWithResult(func, kwargs=None):
        FakeNukeModule.dispatched_calls.append((func, kwargs or {}))
        return func(**(kwargs or {}))

    @staticmethod
    def menu(name):
        return FakeMenu()

    @staticmethod
    def root():
        raise RuntimeError("nuke.root should not be called in ping tests")

    @staticmethod
    def allNodes():
        return []


class FakeClientSocket(object):
    """Minimal client socket stand-in for response-send tests."""

    def __init__(self, send_error=None):
        self.send_error = send_error
        self.timeouts = []
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    def close(self):
        self.closed = True


class FakePythonPanel(object):
    def __init__(self, *args, **kwargs):
        pass


class FakeNukescriptsModule(object):
    PythonPanel = FakePythonPanel


def load_addon_module():
    fake_nuke = FakeNukeModule()
    fake_nukescripts = FakeNukescriptsModule()
    with mock.patch.dict(
        sys.modules,
        {
            "nuke": fake_nuke,
            "nukescripts": fake_nukescripts,
        },
    ):
        if "nuke_mcp_addon" in sys.modules:
            return importlib.reload(sys.modules["nuke_mcp_addon"])
        return importlib.import_module("nuke_mcp_addon")


class AddonProtocolTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = load_addon_module()
        FakeNukeModule.reset()

    def setUp(self):
        FakeNukeModule.reset()
        self.server = self.addon.NukeMCPServer(host="127.0.0.1", port=0)

    def test_encode_message_is_newline_delimited(self):
        self.assertEqual(
            self.addon.encode_message({"id": "abc", "type": "ping"}),
            b'{"id":"abc","type":"ping"}\n',
        )

    def test_encode_message_uses_compact_separators(self):
        payload = {"id": "1", "type": "ping", "params": {"a": 1}}
        encoded = self.addon.encode_message(payload)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(b" ", encoded)
        self.assertEqual(json.loads(encoded.decode("utf-8").strip()), payload)

    def test_process_line_echoes_request_id(self):
        response = json.loads(
            self.server._process_line(
                b'{"id":"abc","type":"ping","params":{}}'
            ).decode("utf-8")
        )
        self.assertEqual(response["id"], "abc")
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["result"], {"pong": True})

    def test_ping_does_not_dispatch_to_main_thread(self):
        self.server._process_line(b'{"id":"abc","type":"ping","params":{}}')
        self.assertEqual(FakeNukeModule.dispatched_calls, [])

    def test_handler_is_dispatched_to_main_thread(self):
        with mock.patch.object(
            self.server, "get_script_info", return_value={"name": "test.nk"}
        ) as get_script_info:
            response = json.loads(
                self.server._process_line(
                    b'{"id":"abc","type":"get_script_info","params":{}}'
                ).decode("utf-8")
            )
        self.assertEqual(len(FakeNukeModule.dispatched_calls), 1)
        _dispatched_func, dispatched_kwargs = FakeNukeModule.dispatched_calls[0]
        self.assertEqual(dispatched_kwargs, {})
        get_script_info.assert_called_once_with()
        self.assertEqual(response["id"], "abc")
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["result"], {"name": "test.nk"})

    def test_braces_inside_json_strings_do_not_break_parsing(self):
        payload = {
            "id": "brace-test",
            "type": "ping",
            "params": {"note": "value with { and } braces"},
        }
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        responses = self.server._feed_data(line + b"\n")
        self.assertEqual(len(responses), 1)
        response = json.loads(responses[0].decode("utf-8"))
        self.assertEqual(response["id"], "brace-test")
        self.assertEqual(response["status"], "success")

    def test_escaped_newlines_inside_json_strings(self):
        payload = {
            "id": "newline-test",
            "type": "ping",
            "params": {"text": "line1\\nline2"},
        }
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        responses = self.server._feed_data(line + b"\n")
        self.assertEqual(len(responses), 1)
        response = json.loads(responses[0].decode("utf-8"))
        self.assertEqual(response["id"], "newline-test")
        self.assertEqual(response["status"], "success")

    def test_malformed_json_returns_structured_error(self):
        response = json.loads(
            self.server._process_line(b'{"id":"bad","type":').decode("utf-8")
        )
        self.assertEqual(response["id"], "bad")
        self.assertEqual(response["status"], "error")
        self.assertIn("type", response["error"])
        self.assertIn("message", response["error"])

    def test_non_object_json_returns_structured_error(self):
        for payload in (b"[]", b"null", b'"hello"'):
            with self.subTest(payload=payload):
                response = json.loads(
                    self.server._process_line(payload).decode("utf-8")
                )
                self.assertIsNone(response["id"])
                self.assertEqual(response["status"], "error")
                self.assertEqual(response["error"]["type"], "InvalidRequest")
                self.assertIn("JSON object", response["error"]["message"])

    def test_malformed_utf8_returns_structured_error(self):
        response = json.loads(
            self.server._process_line(b"\xff\xfe{id}").decode("utf-8")
        )
        self.assertIsNone(response["id"])
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["type"], "UnicodeDecodeError")

    def test_oversized_line_returns_structured_error(self):
        oversized = b"x" * (self.addon.MAX_MESSAGE_BYTES + 1)
        response = json.loads(self.server._process_line(oversized).decode("utf-8"))
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["type"], "MessageTooLarge")

    def test_unterminated_oversized_buffer_returns_structured_error(self):
        chunk = b"x" * (self.addon.MAX_MESSAGE_BYTES + 1)
        responses = self.server._feed_data(chunk)
        self.assertEqual(len(responses), 1)
        response = json.loads(responses[0].decode("utf-8"))
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["type"], "MessageTooLarge")
        self.assertEqual(self.server.buffer, b"")

    def test_fragmented_frames_with_socketpair(self):
        server_sock, client_sock = socket.socketpair()
        try:
            server_sock.setblocking(False)
            client_sock.setblocking(True)
            client_sock.settimeout(2.0)

            request = b'{"id":"frag","type":"ping","params":{}}\n'
            half = len(request) // 2
            client_sock.sendall(request[:half])
            time.sleep(0.05)
            client_sock.sendall(request[half:])

            responses = []
            deadline = time.time() + 2.0
            while not responses and time.time() < deadline:
                try:
                    data = server_sock.recv(8192)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if data:
                    responses.extend(self.server._feed_data(data))

            self.assertEqual(len(responses), 1)
            response = json.loads(responses[0].decode("utf-8"))
            self.assertEqual(response["id"], "frag")
            self.assertEqual(response["status"], "success")
        finally:
            server_sock.close()
            client_sock.close()

    def test_coalesced_frames_with_socketpair(self):
        server_sock, client_sock = socket.socketpair()
        try:
            server_sock.setblocking(False)
            client_sock.setblocking(True)

            request_a = b'{"id":"one","type":"ping","params":{}}\n'
            request_b = b'{"id":"two","type":"ping","params":{}}\n'
            client_sock.sendall(request_a + request_b)

            responses = []
            deadline = time.time() + 2.0
            while len(responses) < 2 and time.time() < deadline:
                try:
                    data = server_sock.recv(8192)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                if data:
                    responses.extend(self.server._feed_data(data))

            self.assertEqual(len(responses), 2)
            ids = [json.loads(item.decode("utf-8"))["id"] for item in responses]
            self.assertEqual(ids, ["one", "two"])
        finally:
            server_sock.close()
            client_sock.close()

    def test_unknown_command_returns_structured_error(self):
        response = json.loads(
            self.server._process_line(
                b'{"id":"missing","type":"not_a_command","params":{}}'
            ).decode("utf-8")
        )
        self.assertEqual(response["id"], "missing")
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["type"], "UnknownCommand")

    def test_handler_exception_returns_structured_error(self):
        def broken_handler():
            raise ValueError("boom")

        with captured_addon_output() as (out, err):
            with mock.patch.object(self.server, "get_script_info", broken_handler):
                response = json.loads(
                    self.server._process_line(
                        b'{"id":"err","type":"get_script_info","params":{}}'
                    ).decode("utf-8")
                )
        self.assertEqual(response["id"], "err")
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["type"], "ValueError")
        self.assertEqual(response["error"]["message"], "boom")
        self.assertIn("Error in handler: ValueError: boom", out.getvalue())
        self.assertIn("ValueError: boom", err.getvalue())

    def test_start_is_idempotent(self):
        port = self._get_free_port()
        server = self.addon.NukeMCPServer(host="127.0.0.1", port=port)
        try:
            self.assertTrue(server.start())
            self.assertTrue(server.start())
            self.assertTrue(server.running)
        finally:
            server.stop()

    def test_stop_clears_client_buffer(self):
        port = self._get_free_port()
        server = self.addon.NukeMCPServer(host="127.0.0.1", port=port)
        try:
            server.start()
            client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            client.sendall(b'{"id":"buf","type":"ping","params":{')
            time.sleep(0.2)
            server.stop()
            self.assertEqual(server.buffer, b"")
            client.close()
        finally:
            if server.running:
                server.stop()

    def test_dispatcher_swallowing_handler_exception_reports_dispatch_error(self):
        def swallowing_dispatcher(func, kwargs=None):
            try:
                func(**(kwargs or {}))
            except Exception:
                pass
            return None

        def broken_handler():
            raise ValueError("boom")

        with captured_addon_output() as (out, _err), mock.patch.object(
            self.addon.nuke,
            "executeInMainThreadWithResult",
            swallowing_dispatcher,
        ), mock.patch.object(self.server, "get_script_info", broken_handler):
            response = json.loads(
                self.server._process_line(
                    b'{"id":"swallow","type":"get_script_info","params":{}}'
                ).decode("utf-8")
            )

        self.assertIn("no usable handler outcome", out.getvalue())
        self.assertEqual(response["id"], "swallow")
        self.assertEqual(response["status"], "error")
        self.assertEqual(
            response["error"]["type"], "MainThreadDispatchError"
        )
        self.assertNotIn("result", response)

    def test_dispatcher_returning_tagged_failure_reports_handler_error(self):
        captured = {}

        def relaying_dispatcher(func, kwargs=None):
            outcome = func(**(kwargs or {}))
            captured["outcome"] = outcome
            return outcome

        def broken_handler():
            raise KeyError("missing-knob")

        with captured_addon_output() as (out, err), mock.patch.object(
            self.addon.nuke,
            "executeInMainThreadWithResult",
            relaying_dispatcher,
        ), mock.patch.object(self.server, "get_script_info", broken_handler):
            response = json.loads(
                self.server._process_line(
                    b'{"id":"tagged","type":"get_script_info","params":{}}'
                ).decode("utf-8")
            )

        self.assertIn("Error in handler: KeyError", out.getvalue())
        self.assertIn("KeyError", err.getvalue())
        self.assertIsInstance(captured["outcome"], dict)
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["type"], "KeyError")
        self.assertIn("missing-knob", response["error"]["message"])

    def test_dispatcher_tagged_success_with_none_value_is_success(self):
        def relaying_dispatcher(func, kwargs=None):
            return func(**(kwargs or {}))

        with mock.patch.object(
            self.addon.nuke,
            "executeInMainThreadWithResult",
            relaying_dispatcher,
        ), mock.patch.object(self.server, "get_script_info", lambda: None):
            response = json.loads(
                self.server._process_line(
                    b'{"id":"none","type":"get_script_info","params":{}}'
                ).decode("utf-8")
            )

        self.assertEqual(response["status"], "success")
        self.assertIsNone(response["result"])

    def test_dispatcher_returning_invalid_outcome_reports_dispatch_error(self):
        def bogus_dispatcher(func, kwargs=None):
            func(**(kwargs or {}))
            return {"unexpected": "shape"}

        with captured_addon_output() as (out, _err), mock.patch.object(
            self.addon.nuke,
            "executeInMainThreadWithResult",
            bogus_dispatcher,
        ), mock.patch.object(
            self.server, "get_script_info", lambda: {"name": "test.nk"}
        ):
            response = json.loads(
                self.server._process_line(
                    b'{"id":"bogus","type":"get_script_info","params":{}}'
                ).decode("utf-8")
            )

        self.assertIn("no usable handler outcome", out.getvalue())
        self.assertEqual(response["status"], "error")
        self.assertEqual(
            response["error"]["type"], "MainThreadDispatchError"
        )

    def test_response_send_timeout_is_distinct_from_poll_timeout(self):
        self.assertGreater(
            self.addon.RESPONSE_SEND_TIMEOUT, self.addon.SOCKET_POLL_TIMEOUT
        )

    def test_successful_send_restores_poll_timeout(self):
        client = FakeClientSocket()
        self.server.client = client
        self.assertTrue(self.server._send_responses(client, [b"a\n", b"b\n"]))
        self.assertEqual(
            client.timeouts,
            [self.addon.RESPONSE_SEND_TIMEOUT, self.addon.SOCKET_POLL_TIMEOUT],
        )
        self.assertEqual(client.sent, [b"a\n", b"b\n"])
        self.assertIs(self.server.client, client)
        self.assertFalse(client.closed)

    def test_send_timeout_resets_client_session(self):
        client = FakeClientSocket(send_error=socket.timeout("timed out"))
        self.server.client = client
        self.server.buffer = b'{"partial":'
        with captured_addon_output() as (out, _err):
            self.assertFalse(self.server._send_responses(client, [b"a\n"]))
        self.assertIn("Error sending response", out.getvalue())
        self.assertIn("timed out", out.getvalue())
        self.assertTrue(client.closed)
        self.assertIsNone(self.server.client)
        self.assertEqual(self.server.buffer, b"")

    def test_send_failure_resets_client_session(self):
        client = FakeClientSocket(send_error=OSError("broken pipe"))
        self.server.client = client
        self.server.buffer = b'{"partial":'
        with captured_addon_output() as (out, _err):
            self.assertFalse(self.server._send_responses(client, [b"a\n"]))
        self.assertIn("Error sending response", out.getvalue())
        self.assertIn("broken pipe", out.getvalue())
        self.assertTrue(client.closed)
        self.assertIsNone(self.server.client)
        self.assertEqual(self.server.buffer, b"")

    def test_reset_client_tolerates_concurrently_cleared_client(self):
        client = FakeClientSocket()
        self.server.client = None
        self.server.buffer = b"leftover"
        self.server._reset_client(client)
        self.assertTrue(client.closed)
        self.assertIsNone(self.server.client)
        self.assertEqual(self.server.buffer, b"")

    def test_get_script_info_raises_on_failure(self):
        with captured_addon_output() as (out, err):
            with self.assertRaises(Exception) as ctx:
                self.server.get_script_info()
        self.assertIn("script info", str(ctx.exception).lower())
        self.assertIn("Error in get_script_info", out.getvalue())
        self.assertIn("RuntimeError", err.getvalue())

    def test_get_script_info_failure_yields_error_envelope(self):
        with captured_addon_output() as (out, _err):
            response = json.loads(
                self.server._process_line(
                    b'{"id":"info","type":"get_script_info","params":{}}'
                ).decode("utf-8")
            )
        self.assertIn("Error in get_script_info", out.getvalue())
        self.assertIn("Error in handler", out.getvalue())
        self.assertEqual(response["id"], "info")
        self.assertEqual(response["status"], "error")
        self.assertNotIn("result", response)

    def test_loop_error_is_reported_while_running(self):
        self.server.running = True
        with captured_addon_output() as (out, _err):
            self.assertTrue(
                self.server._report_loop_error(
                    "receiving data", OSError("real failure")
                )
            )
        self.assertIn("real failure", out.getvalue())

    def test_loop_error_is_silent_after_stop_requested(self):
        self.server.running = False
        with captured_addon_output() as (out, err):
            self.assertFalse(
                self.server._report_loop_error(
                    "accepting connection", OSError("closed by stop")
                )
            )
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_shutdown_does_not_report_socket_errors(self):
        port = self._get_free_port()
        server = self.addon.NukeMCPServer(host="127.0.0.1", port=port)
        client = None
        with captured_addon_output() as (out, err):
            try:
                server.start()
                client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
                time.sleep(0.2)
            finally:
                server.stop()
                if client is not None:
                    client.close()
        output = out.getvalue() + err.getvalue()
        self.assertNotIn("Error accepting", output)
        self.assertNotIn("Error receiving", output)
        self.assertNotIn("WinError 10038", output)

    def test_server_loop_round_trips_over_real_socket(self):
        port = self._get_free_port()
        server = self.addon.NukeMCPServer(host="127.0.0.1", port=port)
        client = None
        try:
            self.assertTrue(server.start())
            client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            client.settimeout(2.0)
            client.sendall(b'{"id":"live","type":"ping","params":{}}\n')

            buffer = b""
            deadline = time.time() + 2.0
            while b"\n" not in buffer and time.time() < deadline:
                buffer += client.recv(8192)

            self.assertIn(b"\n", buffer)
            response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
            self.assertEqual(response["id"], "live")
            self.assertEqual(response["status"], "success")
            self.assertEqual(response["result"], {"pong": True})
        finally:
            if client is not None:
                client.close()
            server.stop()

    def _get_free_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port


if __name__ == "__main__":
    unittest.main()
