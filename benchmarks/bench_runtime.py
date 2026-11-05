"""Runtime micro-benchmarks for the Hermes-gap optimization plan.

Offline and free: requests go to a local fake SSE server, never to a model.

    .venv\\Scripts\\python benchmarks\\bench_runtime.py          # table
    .venv\\Scripts\\python benchmarks\\bench_runtime.py --json   # machine-readable

Measures:
  1. httpx.Client creation cost (old per-request pattern vs the shared factory)
  2. 10 sequential streaming requests: new client each time vs provider reuse,
     plus how many TCP connections the server saw
  3. grep (no match) and glob **/*.py over the repo, median of 3 (Day 4)
  4. token estimate vs real MiniMax-M3 counts on benchmarks/samples (Day 3)
  5. durable SQLite append latency for 100 messages (Day 5)
  6. plan approval state reads before and after the mtime cache (Day 6)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
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
from lilbot.core.session_db import SessionDB  # noqa: E402
from lilbot.llm import providers  # noqa: E402
from lilbot.sandbox import Sandbox  # noqa: E402
from lilbot.tools.builtin import _glob, _grep  # noqa: E402
from lilbot.tools.registry import _read_cached_plan_approval  # noqa: E402

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


def bench_grep(runs: int = 3) -> dict[str, object]:
    """Median of a few runs — single runs swing 2x with the OS file cache."""
    ctx = SimpleNamespace(sandbox=Sandbox(ROOT))

    def median_ms(fn) -> float:
        samples = []
        for _ in range(runs):
            t = time.perf_counter()
            fn()
            samples.append(time.perf_counter() - t)
        return _ms(statistics.median(samples))

    glob_lines = _glob({"pattern": "**/*.py"}, ctx).metadata["count"]
    return {
        "grep_repo_no_match_ms": median_ms(lambda: _grep({"pattern": "zzz_not_exist_zzz"}, ctx)),
        "glob_py_ms": median_ms(lambda: _glob({"pattern": "**/*.py"}, ctx)),
        "glob_py_lines": glob_lines,
    }


# Real prompt_tokens for benchmarks/samples/*, measured on MiniMax-M3
# (2026-10-28, minus the fixed per-request overhead). Other tokenizers differ;
# the agent calibrates against live usage at runtime.
_REAL_TOKENS_MINIMAX_M3 = {"zh_doc.txt": 2610, "zh_code.txt": 1511, "en_code.txt": 1413}


def bench_tokens() -> dict[str, object]:
    out: dict[str, object] = {}
    samples = ROOT / "benchmarks" / "samples"
    for name, real in _REAL_TOKENS_MINIMAX_M3.items():
        text = (samples / name).read_text(encoding="utf-8")
        est = estimate_tokens_text(text)
        old = len(text) // 4
        stem = name.removesuffix(".txt")
        out[f"tokens_{stem}"] = f"real {real} | est {est} ({(est - real) / real:+.0%}) | old len/4 {old} ({(old - real) / real:+.0%})"
    return out


def bench_session_append(messages: int = 100) -> dict[str, object]:
    """Measure the durable per-message SQLite append path used during turns."""
    with tempfile.TemporaryDirectory(prefix="lilbot-session-bench-") as tmp:
        db = SessionDB(Path(tmp) / "sessions.sqlite3")
        samples: list[float] = []
        started = time.perf_counter()
        db.begin_turn()
        open_ms = (time.perf_counter() - started) * 1000
        try:
            for index in range(messages):
                message = {"role": "tool", "tool_call_id": f"bench-{index}", "content": "ok"}
                started = time.perf_counter()
                db.append_message("benchmark", message, {"total_tokens": index}, {"turns": 1})
                samples.append((time.perf_counter() - started) * 1000)
        finally:
            db.end_turn()
            db.close()
    steady = samples[1:] if len(samples) > 1 else samples
    ordered = sorted(steady)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]
    return {
        "session_append_messages": messages,
        "session_db_open_ms": round(open_ms, 3),
        "session_append_first_ms": round(samples[0], 3) if samples else 0.0,
        "session_append_median_ms": round(statistics.median(steady), 3) if steady else 0.0,
        "session_append_mean_ms": round(statistics.mean(steady), 3) if steady else 0.0,
        "session_append_p95_ms": round(p95, 3) if steady else 0.0,
    }


def bench_plan_cache(reads: int = 200) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="lilbot-plan-bench-") as directory:
        path = Path(directory) / "plan_mode.json"
        path.write_text(json.dumps({
            "active": True,
            "approval_state": "pending_approval",
            "requires_approval": True,
            "plan": "Inspect the source and report findings. " * 10,
        }), encoding="utf-8")
        _read_cached_plan_approval(path)

        def median_ms(read_state) -> float:
            samples = []
            for _ in range(reads):
                started = time.perf_counter()
                read_state()
                samples.append((time.perf_counter() - started) * 1000)
            return round(statistics.median(samples), 3)

        return {
            "plan_state_reads": reads,
            "plan_uncached_read_median_ms": median_ms(lambda: json.loads(path.read_text(encoding="utf-8"))),
            "plan_cached_read_median_ms": median_ms(lambda: _read_cached_plan_approval(path)),
        }


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
    results.update(bench_session_append())
    results.update(bench_plan_cache())

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        width = max(len(k) for k in results)
        for key, value in results.items():
            print(f"{key:<{width}}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
