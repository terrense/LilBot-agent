"""Runtime micro-benchmarks for the Hermes-gap optimization plan.

Offline and free: requests go to a local fake SSE server, never to a model.

    .venv\\Scripts\\python benchmarks\\bench_runtime.py          # table
    .venv\\Scripts\\python benchmarks\\bench_runtime.py --json   # machine-readable

Measures:
  1. httpx.Client creation cost (old per-request pattern vs the shared factory)
  2. 10 sequential streaming requests: new client each time vs provider reuse,
     plus how many TCP connections the server saw
  3. grep over the repo for a string that does not exist (Day 4)
  4. token estimate for Chinese text (Day 3)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import httpx  # noqa: E402

from lilbot.config import LilBotConfig  # noqa: E402
from lilbot.core.compaction import estimate_tokens_text  # noqa: E402
from lilbot.llm import providers  # noqa: E402
from lilbot.sandbox import Sandbox  # noqa: E402
from lilbot.tools.builtin import _grep  # noqa: E402

_SSE = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
    b"data: [DONE]\n\n"
)


class _FakeSSE(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like a real API endpoint
    connections: set[tuple[str, int]] = set()

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).connections.add(self.client_address)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(_SSE)))
        self.end_headers()
        self.wfile.write(_SSE)

    def log_message(self, *args: object) -> None:
        pass


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def bench_client_create(n: int = 10) -> dict[str, float]:
    def avg(factory) -> float:
        samples = []
        for _ in range(n):
            t = time.perf_counter()
            factory().close()
            samples.append(time.perf_counter() - t)
        return _ms(statistics.mean(samples))

    providers._ssl_context()  # one-time cost, paid once per process
    return {
        "old_per_request_client_ms": avg(lambda: httpx.Client(timeout=providers._http_timeout())),
        "shared_factory_client_ms": avg(providers._new_http_client),
    }


def bench_sequential_requests(n: int = 10) -> dict[str, float]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSSE)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    msgs = [{"role": "user", "content": "hi"}]
    body = {"model": "m", "messages": msgs, "stream": True}
    try:
        _FakeSSE.connections = set()
        t = time.perf_counter()
        for _ in range(n):  # what providers.py did before Day 1
            with httpx.Client(timeout=providers._http_timeout()) as client:
                with client.stream("POST", f"{base_url}/chat/completions", json=body) as resp:
                    for _line in resp.iter_lines():
                        pass
        old_total, old_conns = time.perf_counter() - t, len(_FakeSSE.connections)

        _FakeSSE.connections = set()
        cfg = LilBotConfig(workspace=ROOT, provider="openai", model="m", api_key="x", base_url=base_url)
        provider = providers.OpenAICompatibleProvider(cfg)
        t = time.perf_counter()
        for _ in range(n):
            list(provider.complete_stream(msgs, []))
        new_total, new_conns = time.perf_counter() - t, len(_FakeSSE.connections)
        provider.close()
    finally:
        server.shutdown()
        server.server_close()
    return {
        "requests": n,
        "old_total_ms": _ms(old_total),
        "old_tcp_connections": old_conns,
        "reuse_total_ms": _ms(new_total),
        "reuse_tcp_connections": new_conns,
    }


def bench_grep() -> dict[str, float]:
    ctx = SimpleNamespace(sandbox=Sandbox(ROOT))
    t = time.perf_counter()
    _grep({"pattern": "zzz_not_exist_zzz"}, ctx)
    return {"grep_repo_no_match_ms": _ms(time.perf_counter() - t)}


def bench_tokens() -> dict[str, float]:
    zh = "这是一个用于测试中文分词的句子，包含常见的汉字和标点符号。" * 50
    return {"zh_chars": len(zh), "zh_estimated_tokens": estimate_tokens_text(zh)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    t = time.perf_counter()
    providers._ssl_context()
    ssl_once_ms = _ms(time.perf_counter() - t)
    results: dict[str, object] = {"ssl_context_once_ms": ssl_once_ms}
    results.update(bench_client_create())
    results.update(bench_sequential_requests())
    results.update(bench_grep())
    results.update(bench_tokens())

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        width = max(len(k) for k in results)
        for key, value in results.items():
            print(f"{key:<{width}}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
