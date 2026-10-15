"""Request-scoped MCP Streamable HTTP client; no browser-supplied endpoints."""
import json
import time
from urllib.parse import urlsplit

import requests


class MCPError(RuntimeError):
    pass


class MCPClient:
    MAX_BYTES = 2_000_000

    def __init__(self, url, token="", timeout=30):
        try:
            parsed = urlsplit(url)
            parsed.port  # Validate a configured port before making a request.
        except ValueError as exc:
            raise MCPError("Configure a valid MCP_SERVER_URL.") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise MCPError("Configure a valid MCP_SERVER_URL without embedded credentials.")
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        if token:
            self.session.headers["Authorization"] = "Bearer " + token
        self.counter = 0
        self.deadline = time.monotonic() + 100

    def __enter__(self):
        try:
            result = self.rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "buro-agentic-ai", "version": "1.0"},
            })
            version = result.get("protocolVersion")
            if version not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
                raise MCPError("MCP server negotiated an unsupported protocol version.")
            self.session.headers["MCP-Protocol-Version"] = version
            self.rpc("notifications/initialized", notification=True)
            return self
        except Exception:
            self.session.close()
            raise

    def __exit__(self, *args):
        # Sessions are request-scoped. Closing the HTTP client releases local resources;
        # server session expiry is managed by the MCP server.
        self.session.close()

    def rpc(self, method, params=None, notification=False):
        self.counter += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self.counter
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError("The agent reached its request time limit.")
        try:
            with self.session.post(self.url, json=payload, timeout=(min(5, remaining), min(self.timeout, remaining)), stream=True, allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise MCPError(f"MCP request failed (HTTP {response.status_code}); check server connectivity and credentials.")
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session.headers["Mcp-Session-Id"] = session_id
                if notification:
                    return {}
                total = 0
                chunks = []
                is_sse = "text/event-stream" in response.headers.get("Content-Type", "")
                event = []
                buffer = b""
                for chunk in response.iter_content(chunk_size=4096):
                    if time.monotonic() >= self.deadline:
                        raise MCPError("The agent reached its request time limit.")
                    total += len(chunk)
                    if total > self.MAX_BYTES:
                        raise MCPError("MCP response exceeded the size limit.")
                    if not is_sse:
                        chunks.append(chunk)
                        continue
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.rstrip(b"\r")
                        if line.startswith(b"data:"):
                            event.append(line[5:].lstrip())
                        elif not line and event:
                            message = json.loads(b"\n".join(event))
                            event = []
                            if isinstance(message, dict) and message.get("id") == payload["id"]:
                                return self._result(message)
                if is_sse:
                    if buffer.startswith(b"data:"):
                        event.append(buffer[5:].lstrip())
                    if not event:
                        raise MCPError("MCP stream ended without a matching response.")
                    message = json.loads(b"\n".join(event))
                else:
                    message = json.loads(b"".join(chunks))
                if not isinstance(message, dict) or message.get("id") != payload["id"]:
                    raise MCPError("MCP response did not match the request.")
                return self._result(message)
        except requests.RequestException as exc:
            raise MCPError("MCP connection failed or timed out. Check the configured server from the Django pod.") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise MCPError("MCP server returned an invalid response.") from exc

    @staticmethod
    def _result(message):
        if "error" in message:
            # Do not return server exceptions, which may include credentials or URLs.
            raise MCPError("MCP server rejected the request; check its server logs.")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPError("MCP response is missing its result object.")
        return result

    def list_tools(self):
        tools = []
        cursor = None
        seen = set()
        for _ in range(20):
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            batch = result.get("tools", [])
            if not isinstance(batch, list) or any(not isinstance(t, dict) for t in batch):
                raise MCPError("MCP tool catalog is invalid.")
            tools.extend(batch)
            if len(tools) > 500:
                raise MCPError("MCP catalog exceeds the configured client limit.")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise MCPError("MCP catalog pagination is invalid.")
            seen.add(cursor)
        raise MCPError("MCP catalog pagination limit reached.")

    def call_tool(self, name, arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})
