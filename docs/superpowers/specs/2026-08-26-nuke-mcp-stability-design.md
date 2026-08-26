# Nuke MCP Stability-First Design

## Goal

Prevent Nuke and the MCP process from crashing or disconnecting during normal
node operations and renders while preserving the current tool names, default
host, and port.

## Scope

This pass changes the transport and execution boundary between
`nuke_mcp_server.py` and `nuke_mcp_addon.py`. Existing MCP tools remain
available. New feature work and broad workflow-rule changes are out of scope.

## Architecture

The existing two-process architecture remains:

1. The MCP process serves tools over stdio.
2. It connects to the addon over localhost TCP.
3. The addon receives framed requests on a background socket thread.
4. Every operation that accesses Nuke is synchronously dispatched to Nuke's
   main thread.
5. The result is returned as a framed response with the matching request ID.

In GUI Nuke, dispatch uses `nuke.executeInMainThreadWithResult`. The helper is
called only from the socket worker. A direct-call fallback is retained for
test doubles and environments where the helper is unavailable.

## Wire Protocol

Messages use newline-delimited UTF-8 JSON. Each request contains:

- `id`: a unique request identifier
- `type`: the command name
- `params`: command arguments

Each response echoes `id` and contains:

- `status`: `success` or `error`
- `result`: successful result data
- `error`: structured error data when unsuccessful

The receiver buffers partial reads and handles multiple messages from one
read. It rejects oversized messages and malformed JSON without reusing the
bad payload for a later command.

A dedicated `ping` command checks transport health without inspecting the
script or touching the node graph.

## Connection Management

The MCP side serializes socket request/response cycles with a lock so
concurrent tool calls cannot interleave bytes. A failed send, closed socket,
timeout, malformed response, or mismatched request ID invalidates and closes
the connection before the error is returned.

Connection creation retries with bounded backoff. Existing connections are
checked using `ping`, not `get_script_info`.

Timeouts are configurable by category:

- connection establishment: short
- ordinary inspection/edit command: moderate
- render and other long-running commands: long

Timeouts produce explicit errors and leave the next call able to reconnect.
The server does not retry mutations automatically because that could apply
the same edit twice.

## Addon Lifecycle

Starting an already-running addon is idempotent. Stopping closes client and
listener sockets, clears buffered data, and allows the worker to exit.
Disconnects reset only the affected client session; the listener remains
available for reconnection.

Socket and command exceptions are contained within the addon and converted to
structured responses when possible. UI status updates continue to occur
through Nuke-safe mechanisms.

## Logging and Diagnostics

The MCP process sends logs to stderr so stdout remains a valid MCP stdio
transport. Default logging is informative rather than full debug payload
logging.

Logs include command name, request ID, elapsed time, reconnects, timeout
category, and exception class. Arbitrary code and full parameter payloads are
not logged by default.

## Arbitrary Python

`execute_nuke_code` remains for compatibility, but it uses the same
main-thread dispatch and structured error path as every other command.
Its documentation identifies it as an advanced, high-risk operation.
Removing it or adding interactive authorization requires a separate design
because MCP client confirmation behavior varies.

## Compatibility

- Preserve current MCP tool names and arguments.
- Preserve localhost and default port `9876`.
- Accept only the new newline-framed protocol after both repository
  components are updated together.
- Remain compatible with Python versions supported by the existing project;
  avoid syntax requiring modern Python inside the Nuke addon.
- Preserve human-readable MCP tool results where practical.

## Testing

Automated tests use a fake `nuke` module and real localhost sockets where
useful. They cover:

- partial and coalesced newline-delimited messages
- braces and newlines escaped inside JSON strings
- malformed and oversized messages
- request/response ID matching
- main-thread dispatch for successful and failed handlers
- lightweight ping behavior
- concurrent MCP calls being serialized
- disconnect and reconnect behavior
- ordinary versus render timeout selection
- stderr logging configuration

Static verification compiles all Python files. Tests must not require an
installed Nuke application. A final manual smoke test in Nuke should start the
panel, query script info, create and modify one node, reconnect the MCP
client, and render a short range.

## Success Criteria

- No Nuke API handler runs directly on the socket worker thread.
- JSON messages cannot be split or merged incorrectly by brace counting.
- Health checks do not enumerate the script.
- Concurrent requests cannot corrupt the TCP stream.
- A timeout or disconnect does not poison subsequent requests.
- Long renders are not constrained by the ordinary command timeout.
- MCP stdout contains no application logs.
- The automated protocol and connection tests pass.
