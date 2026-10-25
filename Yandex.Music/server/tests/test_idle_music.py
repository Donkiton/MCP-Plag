import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

import idle_worker
import lazy_stdio

CATALOG = json.loads((SERVER / "music_catalog.json").read_text(encoding="utf-8"))


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


class DiscoveryTests(unittest.TestCase):
    def test_discovery_does_not_contact_worker(self):
        def forbidden(_):
            raise AssertionError("Discovery woke the music worker")
        for protocol in CATALOG["protocols"]:
            response = lazy_stdio.handle(request("initialize", {"protocolVersion": protocol}), CATALOG, forbidden)
            self.assertEqual(response["result"]["protocolVersion"], protocol)
        for method in ("tools/list", "resources/list", "resources/templates/list", "prompts/list", "ping"):
            response = lazy_stdio.handle(request(method), CATALOG, forbidden)
            self.assertIn("result", response)
            if method != "ping":
                self.assertEqual(response["result"], CATALOG[method])
        self.assertIsNone(lazy_stdio.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, CATALOG, forbidden))

    def test_invalid_requests_never_wake_worker(self):
        def forbidden(_):
            raise AssertionError("Invalid request woke the worker")
        for name in ([], "not_a_tool", None):
            response = lazy_stdio.handle(request("tools/call", {"name": name}), CATALOG, forbidden)
            self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(lazy_stdio.handle([], CATALOG, forbidden)["error"]["code"], -32600)
        self.assertEqual(lazy_stdio.handle(request("ping", request_id=True), CATALOG, forbidden)["error"]["code"], -32600)

    def test_lost_write_response_is_never_replayed(self):
        calls = []
        def lost(req):
            calls.append(req)
            raise OSError("Reply lost after execution")
        response = lazy_stdio.handle(request("tools/call", {"name": "create_playlist", "arguments": {"title": "test"}}), CATALOG, lost)
        self.assertEqual(len(calls), 1)
        self.assertEqual(response["error"]["code"], -32603)

    def test_stdio_protocol_without_socket(self):
        packets = [request("initialize", {"protocolVersion": "2025-11-25"}),
                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   request("tools/list", request_id=2), request("ping", request_id=3)]
        env = dict(os.environ, YM_WORKER_SOCKET="/no-such-music-worker.sock")
        result = subprocess.run([sys.executable, str(Path(lazy_stdio.__file__))],
                                input="\n".join(map(json.dumps, packets)) + "\n",
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(s) for s in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[1]["result"], CATALOG["tools/list"])
        self.assertEqual(json.loads(result.stderr)["tool_calls"], 0)

    def test_closed_client_output_is_a_clean_exit(self):
        client = subprocess.Popen([sys.executable, str(Path(lazy_stdio.__file__))],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client.stdout.close()
        client.stderr.close()
        client.stdin.close()
        self.assertEqual(client.wait(10), 0, "Closed client pipes must not cause exit 120/retry")


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(asyncio, "Runner"), "Linux worker tests")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "worker.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(16)

    def tearDown(self):
        self.listener.close()
        self.directory.cleanup()

    def start(self, executor, idle=0.15):
        errors = []
        def run():
            try:
                idle_worker.serve(self.listener, executor, idle_seconds=idle)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def call(self, n=1):
        return lazy_stdio.forward(request("tools/call", {"name": "account_info", "arguments": {}}, n), self.path)

    def test_eight_clients_share_serial_executor_then_it_sleeps(self):
        calls, active = [], []
        async def execute(req):
            active.append(req["id"])
            self.assertEqual(len(active), 1)
            await asyncio.sleep(0.025)
            calls.append(req["id"])
            active.clear()
            return {"content": [{"type": "text", "text": "ok"}], "isError": False}
        thread, errors = self.start(execute, idle=0.3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.call, range(8)))
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(r["result"]["isError"] is False for r in results))
        thread.join(3)
        self.assertFalse(thread.is_alive(), "Idle executor did not stop")
        self.assertEqual(errors, [])
        # systemd owns the listening socket even after the executor sleeps.
        self.assertGreaterEqual(self.listener.fileno(), 0)

    def test_idle_timeout_never_interrupts_active_operation(self):
        async def execute(req):
            await asyncio.sleep(0.2)
            return {"content": [], "isError": False}
        thread, errors = self.start(execute, idle=0.05)
        self.assertIn("result", self.call())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_invalid_socket_request_does_not_execute_tools(self):
        async def forbidden(req):
            raise AssertionError("Invalid packet reached music code")
        thread, errors = self.start(forbidden)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(self.path)
            connection.sendall(b'[]\n')
            reply = json.loads(connection.makefile("rb").readline())
        self.assertEqual(reply["error"]["code"], -32600)
        thread.join(2)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
