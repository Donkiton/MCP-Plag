"""One socket-activated music executor, released after an idle timeout."""
import asyncio
import json
import os
import socket
import struct
import time

MAX_REQUEST = 65536
MAX_RESPONSE = 16 * 1024 * 1024
IDLE_SECONDS = 120


class MusicExecutor:
    def __init__(self):
        self.handler = None
        self.request_type = None

    async def __call__(self, request):
        # Import once, and only after a real tool request reaches the worker.
        if self.handler is None:
            import runtime
            from mcp.types import CallToolRequest
            self.request_type = CallToolRequest
            self.handler = runtime.upstream.mcp._mcp_server.request_handlers[CallToolRequest]
        req = self.request_type(method="tools/call", params=request["params"])
        result = await self.handler(req)
        return result.root.model_dump(mode="json", by_alias=True, exclude_none=True)


def serve(listener, executor=None, idle_seconds=IDLE_SECONDS):
    executor = executor or MusicExecutor()
    deadline = time.monotonic() + idle_seconds
    print(json.dumps({"worker_event": "started", "idle_seconds": idle_seconds}), flush=True)
    with asyncio.Runner() as runner:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            listener.settimeout(remaining)
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                break
            with connection:
                # The socket is mode 0600, and only this Unix user/root may call.
                _, peer_uid, _ = struct.unpack("3i", connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                if peer_uid not in (0, os.getuid()):
                    continue
                connection.settimeout(5)
                request_id = None
                try:
                    with connection.makefile("rb") as stream:
                        line = stream.readline(MAX_REQUEST + 1)
                    if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                        raise ValueError("Invalid request size")
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("Invalid request")
                    request_id = request.get("id")
                    params = request.get("params")
                    if (request.get("jsonrpc") != "2.0" or request.get("method") != "tools/call"
                            or isinstance(request_id, bool) or not isinstance(request_id, (str, int))
                            or not isinstance(params, dict) or not isinstance(params.get("name"), str)
                            or not isinstance(params.get("arguments", {}), dict)):
                        raise ValueError("Invalid tool request")
                except (OSError, ValueError):
                    reply = {"jsonrpc": "2.0", "id": request_id,
                             "error": {"code": -32600, "message": "Invalid request"}}
                else:
                    started = time.monotonic()
                    try:
                        result = runner.run(executor(request))
                        reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
                    except Exception as exc:
                        reply = {"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32603, "message": "Music execution failed; do not replay writes."}}
                        print(json.dumps({"worker_event": "error", "type": type(exc).__name__}), flush=True)
                    deadline = time.monotonic() + idle_seconds
                    print(json.dumps({"worker_event": "tool_completed", "tool": params["name"],
                                      "elapsed_ms": round((time.monotonic() - started) * 1000)}), flush=True)
                payload = (json.dumps(reply, ensure_ascii=False) + "\n").encode()
                if len(payload) > MAX_RESPONSE:
                    payload = (json.dumps({"jsonrpc": "2.0", "id": request_id,
                                          "error": {"code": -32603, "message": "Response too large; do not replay writes."}}) + "\n").encode()
                try:
                    connection.sendall(payload)
                except OSError:
                    # A completed write stays completed even if its caller left.
                    pass
    print(json.dumps({"worker_event": "idle_exit"}), flush=True)


def main():
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("This worker requires its private systemd socket")
    with socket.socket(fileno=3) as listener:
        serve(listener)


if __name__ == "__main__":
    main()
