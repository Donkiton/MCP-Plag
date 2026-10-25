"""Local MCP discovery; one SSH process per explicit tools/call, never at startup."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
MAX_REQUEST = 65536
MAX_RESPONSE = 16 * 1024 * 1024
SSH_COMMAND = [
    r"C:\Windows\System32\OpenSSH\ssh.exe", "-T", "-i",
    str(Path.home() / ".ssh" / "id_ed25519_yandex_music_vdsina"),
    "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes", "-o", "ConnectionAttempts=1",
    "root@77.238.249.144", "/usr/local/bin/yandex-music-mcp-once",
]


def log(event, **fields):
    # Never log arguments, responses, credentials, or remote stderr.
    try:
        print(json.dumps({"adapter_event": event, **fields}), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def forward_ssh(request):
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    # communicate sends EOF after this one request and waits for SSH to exit.
    # No idle timer, background connection, or retry (writes cannot be replayed).
    with subprocess.Popen(
        SSH_COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        log("ssh_started", pid=process.pid, tool=request["params"]["name"])
        output, _ = process.communicate(payload)
        log("ssh_finished", pid=process.pid, returncode=process.returncode)
        if process.returncode != 0 or not output or len(output) > MAX_RESPONSE:
            raise OSError("SSH execution failed or response was incomplete")
    reply = json.loads(output)
    if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
            or reply.get("id") != request["id"]
            or (("result" in reply) == ("error" in reply))):
        raise ValueError("Invalid SSH response")
    return reply


def handle(request, catalog, forwarder=forward_ssh):
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
        return None
    if method == "initialize":
        result = dict(catalog["initialize"])
        if params.get("protocolVersion") in catalog["protocols"]:
            result["protocolVersion"] = params["protocolVersion"]
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
            log("ssh_failed", tool=params["name"])
            return error(request_id, -32603,
                         "Исполнитель Яндекс Музыки недоступен или ответ потерян. "
                         "Команда могла выполниться; не повторяйте изменения автоматически.")
    else:
        return error(request_id, -32601, "Method not found")
    log("local_discovery", method=method)
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def silence_closed_output():
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


def main():
    catalog = json.loads((ROOT / "music_catalog.json").read_text(encoding="utf-8"))
    stream = sys.stdin.buffer
    while True:
        line = stream.readline(MAX_REQUEST + 1)
        if not line:
            break
        if len(line) > MAX_REQUEST:
            while line and not line.endswith(b"\n"):
                line = stream.readline(MAX_REQUEST + 1)
            reply = error(None, -32700, "Request too large")
        else:
            try:
                reply = handle(json.loads(line), catalog)
            except (ValueError, UnicodeError):
                reply = error(None, -32700, "Parse error")
        if reply is not None:
            try:
                sys.stdout.buffer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
            except OSError:
                break
    silence_closed_output()


if __name__ == "__main__":
    main()
