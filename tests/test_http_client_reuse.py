"""Day 1 (Hermes gap P1): the OpenAI-compatible provider reuses one keep-alive
httpx.Client instead of building a new one (and reloading certifi) per request."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from lilbot.cli import switch_runtime_model
from lilbot.config import LilBotConfig
from lilbot.llm import providers
from lilbot.llm.providers import OpenAICompatibleProvider

_CHAT = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
_SSE = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'


class _Factory:
    """Stands in for ``_new_http_client``: real httpx.Client on a mock transport."""

    def __init__(self) -> None:
        self.clients: list[httpx.Client] = []
        self.hosts: list[str] = []
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.hosts.append(request.url.host)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_CHAT)

    def __call__(self) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        with self.lock:
            self.clients.append(client)
        return client


def _cfg(base_url: str = "https://a.example/v1") -> LilBotConfig:
    return LilBotConfig(workspace=Path("."), provider="openai", model="m", api_key="k", base_url=base_url)


_MSGS = [{"role": "user", "content": "hi"}]


class HttpClientReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = _Factory()
        patcher = mock.patch.object(providers, "_new_http_client", self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_consecutive_calls_share_one_client(self):
        provider = OpenAICompatibleProvider(_cfg())
        provider.complete(_MSGS, [])
        first = provider._client
        provider.complete(_MSGS, [])
        events = list(provider.complete_stream(_MSGS, []))
        self.assertIs(provider._client, first)
        self.assertEqual(len(self.factory.clients), 1)
        self.assertEqual(len(self.factory.hosts), 3)
        self.assertEqual([e.text for e in events if e.text], ["hi"])

    def test_base_url_change_rebuilds_and_closes_old_client(self):
        cfg = _cfg()
        provider = OpenAICompatibleProvider(cfg)
        provider.complete(_MSGS, [])
        old = provider._client
        cfg.base_url = "https://b.example/v1"  # /model mutates config in place
        provider.complete(_MSGS, [])
        self.assertIsNot(provider._client, old)
        self.assertTrue(old.is_closed)
        self.assertEqual(self.factory.hosts, ["a.example", "b.example"])

    def test_close_releases_client_and_next_call_recreates(self):
        provider = OpenAICompatibleProvider(_cfg())
        provider.complete(_MSGS, [])
        client = provider._client
        provider.close()
        self.assertTrue(client.is_closed)
        self.assertIsNone(provider._client)
        provider.close()  # idempotent
        provider.complete(_MSGS, [])
        self.assertEqual(len(self.factory.clients), 2)

    def test_concurrent_calls_build_a_single_client(self):
        # Recall prefetch and the main loop hit the provider from different threads.
        provider = OpenAICompatibleProvider(_cfg())
        start = threading.Barrier(8)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                start.wait()
                provider.complete(_MSGS, [])
                list(provider.complete_stream(_MSGS, []))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.factory.clients), 1)
        self.assertEqual(len(self.factory.hosts), 16)

    def test_stream_is_drained_past_done_so_connection_can_be_pooled(self):
        # httpx only returns a connection to the keep-alive pool once the body
        # has been read to the end; the SSE parser itself stops at [DONE].
        drained = threading.Event()

        class TrackedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield _SSE
                yield b": trailing keep-alive comment\n\n"
                drained.set()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=TrackedStream())

        self.factory.handler = handler
        provider = OpenAICompatibleProvider(_cfg())
        events = list(provider.complete_stream(_MSGS, []))
        self.assertEqual([e.text for e in events if e.text], ["hi"])
        self.assertTrue(drained.is_set())

    def test_switch_runtime_model_closes_previous_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg()
            cfg.workspace = Path(tmp)
            old = OpenAICompatibleProvider(cfg)
            old.complete(_MSGS, [])
            client = old._client
            agent = SimpleNamespace(config=cfg, provider=old)
            ctx = SimpleNamespace(config=cfg, subagents=SimpleNamespace(provider=None))
            switch_runtime_model(agent, ctx, "gpt-4o")
        self.assertIsNot(agent.provider, old)
        self.assertTrue(client.is_closed)


class SslContextCacheTests(unittest.TestCase):
    def test_ssl_context_built_once(self):
        with mock.patch.object(providers, "_SSL_CONTEXT", None):
            with mock.patch("httpx.create_ssl_context", wraps=httpx.create_ssl_context) as spy:
                first = providers._ssl_context()
                second = providers._ssl_context()
        self.assertIs(first, second)
        self.assertEqual(spy.call_count, 1)

    def test_real_client_uses_shared_context_and_keepalive_pool(self):
        client = providers._new_http_client()
        try:
            pool = client._transport._pool
            self.assertIs(pool._ssl_context, providers._ssl_context())
            self.assertEqual(pool._max_keepalive_connections, 8)
            self.assertEqual(pool._keepalive_expiry, 60.0)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
