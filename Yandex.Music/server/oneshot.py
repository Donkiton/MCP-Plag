"""Execute exactly one MCP request and exit; no daemon, socket, or idle timer."""
import asyncio
import json
from pathlib import Path
import sys

from lazy_stdio import MAX_REQUEST, MAX_RESPONSE, error, handle


def execute(request):
    # Reuse the current tool policy/handler, not the idle-worker lifecycle.
    from idle_worker import MusicExecutor
    result = asyncio.run(MusicExecutor()(request))
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def main():
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        reply = error(None, -32700, "Request too large")
    else:
        try:
            request = json.loads(raw)
            catalog = json.loads(Path(__file__).with_name("music_catalog.json").read_text(encoding="utf-8"))
            # Discovery is deliberately local. Only tool calls reach this entry point.
            if not isinstance(request, dict) or request.get("method") != "tools/call":
                reply = error(request.get("id") if isinstance(request, dict) else None,
                              -32600, "Only a single tools/call request is accepted")
            else:
                reply = handle(request, catalog, forwarder=execute)
        except (ValueError, UnicodeError):
            reply = error(None, -32700, "Parse error")
        except Exception as exc:
            print(json.dumps({"oneshot_event": "execution_error", "type": type(exc).__name__}),
                  file=sys.stderr, flush=True)
            reply = error(request.get("id") if isinstance(request, dict) else None,
                          -32603, "Music execution failed; do not replay writes.")
    if reply is not None:
        output = (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
        if len(output) > MAX_RESPONSE:
            output = (json.dumps(error(reply.get("id"), -32603,
                                      "Response too large; do not replay writes.")) + "\n").encode()
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
