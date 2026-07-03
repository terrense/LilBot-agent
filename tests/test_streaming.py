from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lilbot.config import LilBotConfig
from lilbot.core.agent import Agent
from lilbot.core.events import ProviderTurn, StreamEvent, TextDelta, ToolCall, TurnFinished
from lilbot.llm.providers import OpenAICompatibleProvider, _iter_stream_events
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult
from lilbot.tui.classic import LilBotUI
from lilbot.tui.dashboard import DashboardUI


class EmptyMemory:
    def context(self) -> str:
        return "(none)"


class EmptySkills:
    def list(self) -> list:
        return []


def _sse(*data_lines: str) -> list[str]:
    """Wrap raw JSON payloads as OpenAI-style `data:` SSE lines."""
    return [f"data: {line}" for line in data_lines]


class StreamParserTests(unittest.TestCase):
    def test_content_deltas_stream_then_assemble_final_turn(self):
        lines = _sse(
            '{"choices":[{"delta":{"content":"Hel"}}]}',
            '{"choices":[{"delta":{"content":"lo"}}]}',
            '{"choices":[{"delta":{}}],"usage":{"total_tokens":7}}',
            "[DONE]",
        )
        events = list(_iter_stream_events(lines))

        texts = [e.text for e in events if e.text]
        self.assertEqual(texts, ["Hel", "lo"])
        final = events[-1].final
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.content, "Hello")
        self.assertEqual(final.tool_calls, [])
        self.assertEqual(final.usage.get("total_tokens"), 7)

    def test_reasoning_deltas_are_separated_from_content(self):
        lines = _sse(
            '{"choices":[{"delta":{"reasoning_content":"think"}}]}',
            '{"choices":[{"delta":{"content":"answer"}}]}',
            "[DONE]",
        )
        events = list(_iter_stream_events(lines))

        self.assertEqual([e.reasoning for e in events if e.reasoning], ["think"])
        final = events[-1].final
        assert final is not None
        self.assertEqual(final.content, "answer")
        self.assertEqual(final.reasoning_content, "think")

    def test_tool_calls_are_reassembled_across_chunks(self):
        lines = _sse(
            '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a",'
            '"function":{"name":"read_file","arguments":"{\\"path\\":"}}]}}]}',
            '{"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\"README.md\\"}"}}]}}]}',
            "[DONE]",
        )
        events = list(_iter_stream_events(lines))
        final = events[-1].final
        assert final is not None

        self.assertEqual(len(final.tool_calls), 1)
        call = final.tool_calls[0]
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "README.md"})
        self.assertEqual(call.call_id, "call_a")

    def test_malformed_and_empty_lines_are_skipped(self):
        lines = [
            "",
            ": keep-alive comment",
            "data: not-json",
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ]
        events = list(_iter_stream_events(lines))

        self.assertEqual([e.text for e in events if e.text], ["ok"])
        assert events[-1].final is not None
        self.assertEqual(events[-1].final.content, "ok")


class StreamingProvider:
    """Fake provider exposing a real `complete_stream` to drive the agent."""

    def __init__(self, chunks: list[str], final: ProviderTurn):
        self.chunks = chunks
        self.final = final
        self.stream_calls = 0

    def complete(self, messages, tools) -> ProviderTurn:  # fallback path
        return self.final

    def complete_stream(self, messages, tools):
        self.stream_calls += 1
        for chunk in self.chunks:
            yield StreamEvent(text=chunk)
        yield StreamEvent(final=self.final)


class BlockingProvider:
    """Legacy provider without `complete_stream` — must still work via fallback."""

    def __init__(self, final: ProviderTurn):
        self.final = final

    def complete(self, messages, tools) -> ProviderTurn:
        return self.final


def _make_agent(tmp: str, provider, stream_output: bool = True) -> Agent:
    ctx = ToolContext(
        sandbox=None,
        permissions=None,
        memory=EmptyMemory(),
        skills=EmptySkills(),
        subagents=None,
        mcp=None,
        config=None,
    )
    cfg = LilBotConfig(workspace=Path(tmp), max_steps=4, stream_output=stream_output)
    return Agent(cfg, provider, ToolRegistry(), ctx)


class AgentStreamDriveTests(unittest.TestCase):
    def test_streaming_provider_surfaces_live_chunks_and_no_final_block(self):
        final = ProviderTurn(content="Hello world")
        provider = StreamingProvider(["Hello ", "world"], final)
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(tmp, provider)
            events = list(agent.run_turn("hi"))

        deltas = [e for e in events if isinstance(e, TextDelta)]
        self.assertTrue(deltas)
        # Every text delta came from the live stream...
        self.assertTrue(all(d.streaming for d in deltas))
        # ...and reassembles into the full answer without a duplicate block.
        self.assertEqual("".join(d.text for d in deltas), "Hello world")
        self.assertEqual(provider.stream_calls, 1)
        self.assertIsInstance(events[-1], TurnFinished)

    def test_stream_output_disabled_emits_single_block(self):
        final = ProviderTurn(content="Hello world")
        provider = StreamingProvider(["Hello ", "world"], final)
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(tmp, provider, stream_output=False)
            events = list(agent.run_turn("hi"))

        deltas = [e for e in events if isinstance(e, TextDelta)]
        self.assertEqual(len(deltas), 1)
        self.assertFalse(deltas[0].streaming)
        self.assertEqual(deltas[0].text, "Hello world")

    def test_blocking_provider_without_stream_falls_back(self):
        final = ProviderTurn(content="final only")
        provider = BlockingProvider(final)
        with tempfile.TemporaryDirectory() as tmp:
            agent = _make_agent(tmp, provider)
            events = list(agent.run_turn("hi"))

        deltas = [e for e in events if isinstance(e, TextDelta)]
        self.assertEqual(len(deltas), 1)
        self.assertFalse(deltas[0].streaming)
        self.assertEqual(deltas[0].text, "final only")

    def test_streaming_turn_with_tool_calls_still_runs_tools(self):
        registry = ToolRegistry()
        executed: list[str] = []

        def handler(args, ctx):
            executed.append("noop")
            return ToolResult(True, "done")

        registry.register(ToolDef("noop", "noop", {"type": "object"}, handler))

        turns = [
            ProviderTurn(content="thinking", tool_calls=[ToolCall("noop", {})]),
            ProviderTurn(content="all done"),
        ]

        class TwoTurnProvider:
            def __init__(self):
                self.i = 0

            def complete(self, messages, tools):
                turn = turns[min(self.i, len(turns) - 1)]
                self.i += 1
                return turn

            def complete_stream(self, messages, tools):
                turn = turns[min(self.i, len(turns) - 1)]
                self.i += 1
                if turn.content:
                    yield StreamEvent(text=turn.content)
                yield StreamEvent(final=turn)

        ctx = ToolContext(None, None, EmptyMemory(), EmptySkills(), None, None, None)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LilBotConfig(workspace=Path(tmp), max_steps=4)
            agent = Agent(cfg, TwoTurnProvider(), registry, ctx)
            events = list(agent.run_turn("go"))

        self.assertEqual(executed, ["noop"])
        streamed = "".join(e.text for e in events if isinstance(e, TextDelta))
        self.assertIn("thinking", streamed)
        self.assertIn("all done", streamed)


class OpenAIStreamBodyTests(unittest.TestCase):
    def test_complete_stream_requests_streaming_with_usage(self):
        import httpx

        captured: dict[str, object] = {}

        class FakeStreamResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
                yield "data: [DONE]"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def stream(self, method, url, headers, json):
                captured["body"] = json
                return FakeStreamResponse()

        cfg = LilBotConfig(workspace=Path("."), provider="deepseek", model="m", api_key="test")
        provider = OpenAICompatibleProvider(cfg)
        import unittest.mock as mock

        with mock.patch("httpx.Client", FakeClient):
            events = list(provider.complete_stream([{"role": "user", "content": "hi"}], []))

        body = captured["body"]
        assert isinstance(body, dict)
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual([e.text for e in events if e.text], ["hi"])
        _ = httpx  # keep import meaningful for the fake transport


class ClassicStreamRenderTests(unittest.TestCase):
    def _render(self, events) -> str:
        ui = LilBotUI(enabled=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            for event in events:
                ui.event(event)
        return buf.getvalue()

    def test_streamed_lines_flush_and_close_on_next_event(self):
        out = self._render([
            TextDelta("Hello ", streaming=True),
            TextDelta("world\nsecond line", streaming=True),
            TurnFinished(1, {}),
        ])

        self.assertIn("LilBot:", out)
        self.assertIn("Hello world", out)
        self.assertIn("second line", out)

    def test_streamed_secret_is_redacted_per_line(self):
        secret = "sk-ABCDEF0123456789ABCDEF0123"
        out = self._render([
            TextDelta(f"key {secret}\n", streaming=True),
            TurnFinished(1, {}),
        ])

        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)


class DashboardStreamRenderTests(unittest.TestCase):
    def _ui(self) -> DashboardUI:
        ui = object.__new__(DashboardUI)
        ui.lines = []
        ui.work_items = []
        ui.tool_count = 0
        ui._stream_active = False
        ui._stream_line = ""
        ui._refresh = lambda: None
        return ui

    def test_streamed_chunks_grow_the_trace_line(self):
        ui = self._ui()
        ui.event(TextDelta("Hello ", streaming=True))
        ui.event(TextDelta("world", streaming=True))

        text = "\n".join(ui.lines)
        self.assertIn("LILBOT", text)
        self.assertIn("Hello world", text)
        # A single growing line, not one line per chunk.
        self.assertEqual(ui.lines[-1], "Hello world")

    def test_streamed_newlines_split_into_discrete_lines(self):
        ui = self._ui()
        ui.event(TextDelta("line one\nline ", streaming=True))
        ui.event(TextDelta("two", streaming=True))

        self.assertIn("line one", ui.lines)
        self.assertEqual(ui.lines[-1], "line two")

    def test_non_streaming_event_ends_the_live_stream(self):
        ui = self._ui()
        ui.event(TextDelta("partial", streaming=True))
        self.assertTrue(ui._stream_active)
        ui.event(TurnFinished(1, {}))
        self.assertFalse(ui._stream_active)


if __name__ == "__main__":
    unittest.main()
