# Nuke MCP Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the localhost Nuke bridge resilient to thread-unsafe API access, fragmented TCP data, concurrent requests, disconnects, and long renders.

**Architecture:** Keep the existing FastMCP-to-addon TCP bridge. Replace implicit JSON completion with newline-delimited request/response envelopes, synchronously marshal addon handlers onto Nuke's main thread, and serialize client exchanges behind a lock. Add isolated tests using fake Nuke objects and socket pairs.

**Tech Stack:** Python standard library, FastMCP, `unittest`, TCP sockets, Nuke Python API.

## Global Constraints

- Preserve current MCP tool names and arguments.
- Preserve localhost and default port `9876`.
- Update both protocol endpoints together; no legacy framing compatibility.
- Avoid modern-only Python syntax in `nuke_mcp_addon.py`.
- Do not automatically retry mutating commands.
- Send MCP-process logs to stderr.

---

### Task 1: Addon Protocol Framing and Main-Thread Dispatch

**Files:**
- Modify: `nuke_mcp_addon.py`
- Create: `tests/test_addon_protocol.py`

**Interfaces:**
- Produces: `encode_message(payload: dict) -> bytes`
- Produces: `NukeMCPServer._process_line(line: bytes) -> bytes`
- Produces: `NukeMCPServer._run_in_nuke(handler, params) -> dict`
- Produces: request/response envelopes containing matching `id`

- [ ] **Step 1: Write failing protocol and dispatch tests**

Create tests that inject a fake `nuke` and `nukescripts` module before loading
the addon. Assert that:

```python
def test_encode_message_is_newline_delimited(self):
    self.assertEqual(
        addon.encode_message({"id": "abc", "type": "ping"}),
        b'{"id":"abc","type":"ping"}\n',
    )

def test_process_line_echoes_request_id(self):
    response = json.loads(
        server._process_line(b'{"id":"abc","type":"ping","params":{}}')
    )
    self.assertEqual(response["id"], "abc")
    self.assertEqual(response["status"], "success")

def test_handler_is_dispatched_to_main_thread(self):
    server._process_line(b'{"id":"abc","type":"get_script_info","params":{}}')
    self.assertEqual(fake_nuke.dispatched_calls, 1)
```

Also exercise fragmented and coalesced frames with `socket.socketpair()`, braces
inside JSON strings, malformed JSON, and a message exceeding `MAX_MESSAGE_BYTES`.

- [ ] **Step 2: Run tests and verify red**

Run: `python -m unittest tests.test_addon_protocol -v`

Expected: FAIL because `encode_message`, `_process_line`, `ping`, request IDs,
and main-thread dispatch do not exist.

- [ ] **Step 3: Implement framed protocol**

Add:

```python
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

def encode_message(payload):
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
```

Register a `ping` handler that returns `{"pong": True}` without accessing
Nuke. Replace brace counting with a byte buffer split on `b"\n"`. Reject an
unterminated buffer once it exceeds `MAX_MESSAGE_BYTES`; reject individual
oversized lines and malformed UTF-8/JSON with structured error envelopes.

Every response must have:

```python
{
    "id": request_id,
    "status": "success",
    "result": result,
}
```

or:

```python
{
    "id": request_id,
    "status": "error",
    "error": {"type": exception_name, "message": message},
}
```

- [ ] **Step 4: Implement synchronous main-thread execution**

Wrap all command handlers:

```python
def _run_in_nuke(self, handler, params):
    dispatcher = getattr(nuke, "executeInMainThreadWithResult", None)
    if dispatcher is None:
        return handler(**params)
    return dispatcher(handler, kwargs=params)
```

Call this only from the socket worker. Keep socket reads/writes on that worker.
Do not dispatch `ping`, because it does not access Nuke.

- [ ] **Step 5: Harden lifecycle**

Make `start()` return successfully when already running. Give listener and
client sockets bounded polling timeouts, close both in `stop()`, clear the
buffer per client, and join the worker briefly unless `stop()` was called from
that worker.

- [ ] **Step 6: Run addon tests**

Run: `python -m unittest tests.test_addon_protocol -v`

Expected: all addon protocol tests PASS.

### Task 2: MCP Connection Serialization, IDs, Ping, and Timeouts

**Files:**
- Modify: `nuke_mcp_server.py`
- Create: `tests/test_connection.py`

**Interfaces:**
- Consumes: newline-framed envelopes from Task 1
- Produces: `NukeConnection.send_command(command_type, params=None, timeout=None)`
- Produces: `NukeConnection.ping() -> bool`
- Produces: `COMMAND_TIMEOUT_SECONDS` and `RENDER_TIMEOUT_SECONDS`

- [ ] **Step 1: Write failing connection tests**

Use `socket.socketpair()` and a small responder thread. Verify:

```python
def test_command_writes_request_id_and_newline(self):
    connection.send_command("ping")
    self.assertTrue(received.endswith(b"\n"))
    self.assertEqual(request["type"], "ping")
    self.assertTrue(request["id"])

def test_ping_does_not_request_script_info(self):
    self.assertTrue(connection.ping())
    self.assertEqual(received_request["type"], "ping")

def test_mismatched_response_id_invalidates_socket(self):
    with self.assertRaises(ProtocolError):
        connection.send_command("ping")
    self.assertIsNone(connection.sock)
```

Add tests for partial responses, two concurrent calls being serialized,
ordinary timeout selection, render timeout selection, and reconnect after a
closed peer.

- [ ] **Step 2: Run tests and verify red**

Run: `python -m unittest tests.test_connection -v`

Expected: FAIL because request IDs, newline reads, ping, locking, timeout
selection, and protocol exceptions are absent.

- [ ] **Step 3: Implement connection state and newline response reads**

Add a `threading.RLock`, receive buffer, UUID request IDs, and:

```python
def _receive_line(self):
    while b"\n" not in self._receive_buffer:
        chunk = self.sock.recv(8192)
        if not chunk:
            raise ConnectionError("Nuke closed the connection")
        self._receive_buffer += chunk
        if len(self._receive_buffer) > MAX_MESSAGE_BYTES:
            raise ProtocolError("Nuke response exceeds maximum size")
    line, self._receive_buffer = self._receive_buffer.split(b"\n", 1)
    return line
```

Lock the complete send/receive cycle. Validate response JSON, response ID, and
status. Always close and null the socket after transport/protocol failure.

- [ ] **Step 4: Implement health check and timeout categories**

Define environment-configurable defaults:

```python
CONNECT_TIMEOUT_SECONDS = float(os.getenv("NUKE_MCP_CONNECT_TIMEOUT", "5"))
COMMAND_TIMEOUT_SECONDS = float(os.getenv("NUKE_MCP_COMMAND_TIMEOUT", "30"))
RENDER_TIMEOUT_SECONDS = float(os.getenv("NUKE_MCP_RENDER_TIMEOUT", "3600"))
```

`ping()` calls `send_command("ping")`. `get_nuke_connection()` uses only
`ping()` for an existing or newly-created connection. The `render` MCP tool
passes `RENDER_TIMEOUT_SECONDS`; ordinary calls use
`COMMAND_TIMEOUT_SECONDS`. Do not retry a command after bytes were sent.

- [ ] **Step 5: Run connection tests**

Run: `python -m unittest tests.test_connection -v`

Expected: all connection tests PASS.

### Task 3: Stdio-Safe Logging and Structured Tool Errors

**Files:**
- Modify: `nuke_mcp_server.py`
- Modify: `main.py`
- Create: `tests/test_logging.py`

**Interfaces:**
- Consumes: protocol and connection exceptions from Task 2
- Produces: application logs exclusively on stderr

- [ ] **Step 1: Write failing logging tests**

Reload the server module while capturing stdout and stderr:

```python
def test_logging_uses_stderr_not_stdout(self):
    server.logger.info("probe")
    self.assertEqual(stdout.getvalue(), "")
    self.assertIn("probe", stderr.getvalue())
```

Assert `main.py` does not print startup messages to stdout and does not call
`input()` after an import failure.

- [ ] **Step 2: Run tests and verify red**

Run: `python -m unittest tests.test_logging -v`

Expected: FAIL because `main.py` prints to stdout and blocks on `input()`.

- [ ] **Step 3: Configure stderr logging**

Configure a `logging.StreamHandler(sys.stderr)` at INFO by default, with
`NUKE_MCP_LOG_LEVEL` override. Log command name, request ID, elapsed seconds,
timeout category, reconnect, and exception class without logging full
parameters or arbitrary Python source.

Replace `main.py` startup prints with logging calls and remove the blocking
`input()`. Let fatal startup errors produce a nonzero process exit.

- [ ] **Step 4: Preserve structured failure details**

Map addon error envelopes into a dedicated exception that retains error type
and message. MCP tools may continue returning human-readable error strings for
compatibility, but logs must include the exception class and command name.
Mark `execute_nuke_code` documentation as advanced and high risk.

- [ ] **Step 5: Run logging tests**

Run: `python -m unittest tests.test_logging -v`

Expected: all logging tests PASS.

### Task 4: Regression Verification and Documentation

**Files:**
- Modify: `readme.md`
- Modify: `docs/superpowers/plans/2026-08-26-nuke-mcp-stability.md`

**Interfaces:**
- Consumes: completed implementation from Tasks 1-3
- Produces: operator configuration and smoke-test instructions

- [ ] **Step 1: Run the full automated suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 2: Compile all Python files**

Run: `python -m compileall -q main.py nuke_mcp_server.py nuke_mcp_addon.py tests`

Expected: exit code 0 with no output.

- [ ] **Step 3: Document operation and tuning**

Add README sections explaining that both addon and MCP components must be
updated together, logs are on stderr, and these variables tune timeouts:

```text
NUKE_MCP_CONNECT_TIMEOUT=5
NUKE_MCP_COMMAND_TIMEOUT=30
NUKE_MCP_RENDER_TIMEOUT=3600
NUKE_MCP_LOG_LEVEL=INFO
```

Document the manual smoke test: start the panel, query script info, create and
modify one Grade, reconnect the MCP client, then render a short frame range.

- [ ] **Step 4: Inspect the final diff**

Run: `git diff --check`

Expected: exit code 0 and no whitespace errors.

Run: `git status --short`

Expected: only the approved source, tests, README, spec, and plan files are
modified or untracked.

No commit is created unless the user explicitly requests one.
