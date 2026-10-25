"""Small MCP discovery adapter; music SDKs only run in the shared worker."""
import json
import os
from pathlib import Path
import socket
import sys
import time

CATALOG_PATH = Path(__file__).with_name("music_catalog.json")
SOCKET_PATH = os.environ.get("YM_WORKER_SOCKET", "/run/yandex-music-mcp-worker.sock")
MAX_REQUEST = 65536
MAX_RESPONSE = 16 * 1024 * 1024


def error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def clean_output_exit():
    # Python exits with 120 if final flushing hits a pipe closed by its client.
    # Redirect that existing descriptor, so interpreter cleanup stays harmless.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            try:
                discard = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(discard, stream.fileno())
                finally:
                    os.close(discard)
            except (OSError, ValueError):
                pass


def forward(request, socket_path=SOCKET_PATH):
    # Never replay a sent request: writes may have completed before a disconnect.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(90)
        connection.connect(socket_path)
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_RESPONSE + 1)
        if not line or len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
            raise OSError("Incomplete worker response")
        reply = json.loads(line)
        if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                or reply.get("id") != request["id"]
                or (("result" in reply) == ("error" in reply))):
            raise ValueError("Invalid worker response")
        return reply


def handle(request, catalog, forwarder=forward):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return error(None, -32600, "Invalid request")
    request_id = request.get("id")
    if "id" in request and (isinstance(request_id, bool)
                            or not isinstance(request_id, (str, int))):
        return error(None, -32600, "Invalid request ID")
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(method, str) or not isinstance(params, dict):
        return error(request_id, -32600, "Invalid request")
    if "id" not in request:
        # Notifications have no response and never wake the music worker.
        return None
    if method == "initialize":
        result = dict(catalog["initialize"])
        requested = params.get("protocolVersion")
        if requested in catalog["protocols"]:
            result["protocolVersion"] = requested
    elif method in catalog and method.endswith("/list"):
        result = catalog[method]
    elif method in ("ping", "logging/setLevel"):
        result = {}
    elif method == "tools/call":
        names = {tool["name"] for tool in catalog["tools/list"]["tools"]}
        if (not isinstance(params.get("name"), str) or params["name"] not in names
                or not isinstance(params.get("arguments", {}), dict)):
            return error(request_id, -32602, "Unknown tool or invalid arguments")
        try:
            return forwarder(request)
        except (OSError, ValueError):
            return error(request_id, -32603,
                         "Исполнитель Яндекс Музыки недоступен или ответ потерян. "
                         "Команда могла выполниться; не повторяйте изменения автоматически.")
    else:
        return error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    stream = sys.stdin.buffer
    started = time.monotonic()
    discovery = 0
    calls = 0
    while True:
        line = stream.readline(MAX_REQUEST + 1)
        if not line:
            try:
                print(json.dumps({"bridge_event": "client_closed", "discovery_requests": discovery,
                                  "tool_calls": calls, "duration_seconds": round(time.monotonic() - started, 1)}),
                      file=sys.stderr, flush=True)
            except OSError:
                pass
            clean_output_exit()
            return
        if len(line) > MAX_REQUEST:
            while not line.endswith(b"\n"):
                line = stream.readline(MAX_REQUEST + 1)
                if not line:
                    break
            reply = error(None, -32700, "Request too large")
        else:
            try:
                request = json.loads(line)
                if isinstance(request, dict) and "id" in request:
                    if request.get("method") == "tools/call":
                        calls += 1
                    else:
                        discovery += 1
                reply = handle(request, catalog)
            except (ValueError, UnicodeError):
                reply = error(None, -32700, "Parse error")
        if reply is not None:
            try:
                sys.stdout.buffer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
            except OSError:
                clean_output_exit()
                return


if __name__ == "__main__":
    main()
