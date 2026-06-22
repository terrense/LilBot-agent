from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lilbot.config import LilBotConfig, load_config
from lilbot.core.agent import Agent
from lilbot.core.delegation import parse_semantic_delegation_plan, plan_auto_delegation
from lilbot.core.events import ProviderTurn, TextDelta, ToolCall, TurnFinished
from lilbot.llm.providers import OpenAICompatibleProvider, ProviderError
from lilbot.tools import ToolContext, ToolDef, ToolRegistry, ToolResult


class EmptyMemory:
    def context(self) -> str:
        return "(none)"


class EmptySkills:
    def list(self) -> list:
        return []


class LoopingProvider:
    def __init__(self, calls_per_turn: int = 1):
        self.calls_per_turn = calls_per_turn
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
        self.calls.append((messages, tools))
        if tools:
            calls = [ToolCall(f"noop_{idx}", {}) for idx in range(self.calls_per_turn)]
            return ProviderTurn(tool_calls=calls)
        return ProviderTurn(content="final answer from gathered results")


def make_agent(tmp: str, provider: LoopingProvider, max_steps: int) -> tuple[Agent, list[str]]:
    executed: list[str] = []
    registry = ToolRegistry()

    def handler(args, ctx):
        executed.append(ctx.current_tool)
        return ToolResult(True, f"result from {ctx.current_tool}")

    for name in ["noop_0", "noop_1"]:
        registry.register(ToolDef(name, "noop", {"type": "object"}, handler))
    ctx = ToolContext(
        sandbox=None,
        permissions=None,
        memory=EmptyMemory(),
        skills=EmptySkills(),
        subagents=None,
        mcp=None,
        config=None,
    )
    original_execute = registry.execute

    def execute(name, arguments, context):
        context.current_tool = name
        return original_execute(name, arguments, context)

    registry.execute = execute  # type: ignore[method-assign]
    cfg = LilBotConfig(workspace=Path(tmp), max_steps=max_steps)
    return Agent(cfg, provider, registry, ctx), executed


class AgentLoopTests(unittest.TestCase):
    def test_default_and_legacy_max_steps_are_twenty(self):
        self.assertEqual(LilBotConfig(Path(".")).max_steps, 20)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".lilbot"
            state.mkdir()
            (state / "config.json").write_text(
                json.dumps({"provider": "auto", "model": "lilbot-rule-model", "max_steps": 8}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(root)
        self.assertEqual(cfg.max_steps, 20)

    def test_step_limit_synthesizes_final_answer_without_stopped_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LoopingProvider()
            agent, executed = make_agent(tmp, provider, max_steps=2)
            events = list(agent.run_turn("keep using tools"))

        text = "\n".join(event.text for event in events if isinstance(event, TextDelta))
        finished = [event for event in events if isinstance(event, TurnFinished)][-1]
        self.assertEqual(executed, ["noop_0", "noop_0"])
        self.assertEqual(finished.steps, 2)
        self.assertIn("final answer from gathered results", text)
        self.assertNotIn("Stopped after max_steps", text)
        self.assertEqual(provider.calls[-1][1], [])

    def test_unexecuted_tool_calls_are_not_recorded_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LoopingProvider(calls_per_turn=2)
            agent, executed = make_agent(tmp, provider, max_steps=1)
            list(agent.run_turn("call two tools"))

        self.assertEqual(executed, ["noop_0"])
        assistant_calls = [
            message
            for message in agent.messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        self.assertEqual(len(assistant_calls[-1]["tool_calls"]), 1)

    def test_broad_code_task_gets_auto_delegation_plan(self):
        plan = plan_auto_delegation("遍历 `src/runtime` 这个项目路径，分析架构和风险", max_agents=3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertGreaterEqual(len(plan.probes), 2)
        self.assertTrue(all(probe.agent_type == "explore" for probe in plan.probes))
        self.assertIn("src/runtime", plan.probes[0].prompt)

    def test_auto_delegation_has_chinese_english_parity_for_common_tasks(self):
        cases = [
            (
                "帮我分析这个项目架构和风险",
                "Analyze this project architecture and risks",
                ["explore", "explore", "explore"],
            ),
            (
                "帮我做阿根廷旅游攻略，查景点、交通和预算",
                "Make an Argentina travel guide with attractions, transportation, and budget",
                ["researcher"],
            ),
            (
                "帮我写一篇雅思作文，要求有大纲、论点和修改建议",
                "Write an IELTS essay with outline, arguments, and revision suggestions",
                ["writer", "writer", "critic"],
            ),
        ]

        for zh, en, expected_types in cases:
            with self.subTest(query=zh):
                zh_plan = plan_auto_delegation(zh, max_agents=3)
                en_plan = plan_auto_delegation(en, max_agents=3)

            self.assertIsNotNone(zh_plan)
            self.assertIsNotNone(en_plan)
            assert zh_plan is not None
            assert en_plan is not None
            self.assertEqual([probe.agent_type for probe in zh_plan.probes], expected_types)
            self.assertEqual([probe.agent_type for probe in en_plan.probes], expected_types)

    def test_auto_delegation_skips_short_direct_tasks(self):
        self.assertIsNone(plan_auto_delegation("帮我看一下这个文件", max_agents=3))
        self.assertIsNone(plan_auto_delegation("show file README.md", max_agents=3))
        self.assertIsNone(plan_auto_delegation("不要用 subagent，帮我分析这个项目架构", max_agents=3))
        self.assertIsNone(plan_auto_delegation("给我写一个2个月减脂详细计划，安排饮食和训练", max_agents=3))

    def test_auto_delegation_keeps_project_roadmap_planning(self):
        plan = plan_auto_delegation("帮我拆解一个技术项目路线图，包含里程碑、风险和验收标准", max_agents=3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([probe.agent_type for probe in plan.probes], ["plan", "critic"])

    def test_research_critic_is_only_for_explicit_risk_review(self):
        normal = plan_auto_delegation("帮我做阿根廷旅游攻略，查景点、交通和预算", max_agents=3)
        risky = plan_auto_delegation("帮我做阿根廷旅游攻略，并重点评估安全风险和避坑事项", max_agents=3)

        assert normal is not None
        assert risky is not None
        self.assertEqual([probe.agent_type for probe in normal.probes], ["researcher"])
        self.assertEqual([probe.agent_type for probe in risky.probes], ["researcher", "researcher"])

    def test_independent_question_burst_gets_parallel_researchers(self):
        prompt = (
            "What are Beijing coordinates? "
            "How long do cats usually live? "
            "Who is Feng Gong? "
            "How tall is LeBron James? "
            "Which NBA teams has LeBron James played for?"
        )
        plan = plan_auto_delegation(prompt, max_agents=5)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.reason, "5 independent questions can be answered in parallel")
        self.assertEqual([probe.agent_type for probe in plan.probes], ["researcher"] * 5)
        self.assertEqual([probe.name for probe in plan.probes], [f"auto_question_{idx:02d}" for idx in range(1, 6)])
        self.assertIn("Answer only subquestion 1", plan.probes[0].prompt)

    def test_question_burst_groups_extra_questions_when_slots_are_limited(self):
        prompt = (
            "What are Beijing coordinates? "
            "How long do cats usually live? "
            "What is the Russian word for hamster? "
            "Who is Feng Gong? "
            "How tall is LeBron James? "
            "Which NBA teams has LeBron James played for?"
        )
        plan = plan_auto_delegation(prompt, max_agents=5)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan.probes), 5)
        self.assertIn("6 independent questions can be covered by 5 parallel researcher subagents", plan.reason)
        joined_prompts = "\n".join(probe.prompt for probe in plan.probes)
        self.assertIn("What is the Russian word for hamster?", joined_prompts)
        self.assertIn("Which NBA teams has LeBron James played for?", joined_prompts)

    def test_question_burst_without_question_marks_gets_parallel_researchers(self):
        prompt = "谁是2025年NBA冠军 那谁是那一年的FMVP呢 哦对还有NBA是哪一个国家的比赛呀"
        plan = plan_auto_delegation(prompt, max_agents=5)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan.probes), 3)
        self.assertEqual([probe.agent_type for probe in plan.probes], ["researcher"] * 3)
        joined_prompts = "\n".join(probe.prompt for probe in plan.probes)
        self.assertIn("谁是2025年NBA冠军", joined_prompts)
        self.assertIn("谁是那一年的FMVP呢", joined_prompts)
        self.assertIn("NBA是哪一个国家的比赛呀", joined_prompts)

    def test_semantic_delegation_plan_parser_accepts_writing_tasks(self):
        response = json.dumps({
            "delegate": True,
            "kind": "writing",
            "reason": "substantial prose can be split into outline and draft",
            "probes": [
                {
                    "name": "style-outline",
                    "agent_type": "writer",
                    "prompt": "Plan a 1000-character classical Chinese prose piece with imagery and structure.",
                    "timeout_ms": 12000,
                },
                {
                    "name": "draft",
                    "agent_type": "writer",
                    "prompt": "Draft the prose in a refined classical style.",
                    "timeout_ms": 15000,
                },
            ],
        })
        plan = parse_semantic_delegation_plan(response, max_agents=3, max_question_agents=5)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([probe.agent_type for probe in plan.probes], ["writer", "writer"])
        self.assertEqual(plan.probes[0].name, "style-outline")

    def test_mixed_research_task_splits_fact_scopes_without_fact_free_planner(self):
        prompt = (
            "I want to travel in South America during China's National Day; recommend destinations "
            "and build a 10-day itinerary. Also tell me the current FIFA men's world ranking top 25. "
            "Last, how many NBA regular season MVP awards has LeBron James won?"
        )
        plan = plan_auto_delegation(prompt, max_agents=4)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([probe.agent_type for probe in plan.probes], ["researcher", "researcher", "researcher"])
        self.assertEqual(
            [probe.name for probe in plan.probes],
            ["auto_research_travel", "auto_research_football_rankings", "auto_research_lebron_mvp"],
        )
        self.assertNotIn("auto_plan_synthesis", [probe.name for probe in plan.probes])

    @unittest.skip("SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions")
    def test_agent_auto_opens_and_evals_explorers_for_broad_task(self):
        class FinalProvider:
            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                return ProviderTurn(content="final synthesis")

        with tempfile.TemporaryDirectory() as tmp:
            calls: list[tuple[str, dict]] = []
            registry = ToolRegistry()

            def agent_open(args, ctx):
                calls.append(("agent_open", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "running"}), {"name": args["name"]})

            def agent_eval(args, ctx):
                calls.append(("agent_eval", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "completed", "result": "evidence"}))

            registry.register(ToolDef("agent_open", "Open subagent.", {"type": "object"}, agent_open))
            registry.register(ToolDef("agent_eval", "Eval subagent.", {"type": "object"}, agent_eval))
            ctx = ToolContext(
                sandbox=None,
                permissions=None,
                memory=EmptyMemory(),
                skills=EmptySkills(),
                subagents=None,
                mcp=None,
                config=None,
            )
            cfg = LilBotConfig(workspace=Path(tmp), max_steps=6)
            agent = Agent(cfg, FinalProvider(), registry, ctx)
            events = list(agent.run_turn("请遍历这个项目代码，分析架构和风险"))

        self.assertEqual([name for name, _args in calls], ["agent_open", "agent_open", "agent_eval", "agent_eval"])
        self.assertEqual(calls[0][1]["type"], "explore")
        self.assertTrue(calls[0][1]["background"])
        self.assertEqual(calls[2][1]["name"], "auto_explore_map")
        self.assertTrue(any(isinstance(event, TextDelta) and "Auto-delegating" in event.text for event in events))
        finished = [event for event in events if isinstance(event, TurnFinished)][-1]
        self.assertEqual(finished.steps, 4)

    @unittest.skip("SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions")
    def test_agent_auto_opens_general_subagents_for_research_task(self):
        class FinalProvider:
            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                return ProviderTurn(content="final synthesis")

        with tempfile.TemporaryDirectory() as tmp:
            calls: list[tuple[str, dict]] = []
            registry = ToolRegistry()

            def agent_open(args, ctx):
                calls.append(("agent_open", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "running"}), {"name": args["name"]})

            def agent_eval(args, ctx):
                calls.append(("agent_eval", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "completed", "result": "evidence"}))

            registry.register(ToolDef("agent_open", "Open subagent.", {"type": "object"}, agent_open))
            registry.register(ToolDef("agent_eval", "Eval subagent.", {"type": "object"}, agent_eval))
            ctx = ToolContext(
                sandbox=None,
                permissions=None,
                memory=EmptyMemory(),
                skills=EmptySkills(),
                subagents=None,
                mcp=None,
                config=None,
            )
            cfg = LilBotConfig(workspace=Path(tmp), max_steps=8)
            agent = Agent(cfg, FinalProvider(), registry, ctx)
            list(agent.run_turn("帮我做阿根廷旅游攻略，查景点、交通和预算"))

        open_calls = [args for name, args in calls if name == "agent_open"]
        eval_calls = [args for name, args in calls if name == "agent_eval"]
        self.assertEqual([args["type"] for args in open_calls], ["researcher"])
        self.assertEqual([args["timeout_ms"] for args in eval_calls], [22000])
        self.assertTrue(any("Avoid repeating the same web/search/tool calls" in message.get("content", "") for message in agent.messages))

    @unittest.skip("SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions")
    def test_agent_auto_opens_fact_researchers_for_mixed_research_task(self):
        class FinalProvider:
            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                return ProviderTurn(content="final synthesis")

        with tempfile.TemporaryDirectory() as tmp:
            calls: list[tuple[str, dict]] = []
            registry = ToolRegistry()

            def agent_open(args, ctx):
                calls.append(("agent_open", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "running"}), {"name": args["name"]})

            def agent_eval(args, ctx):
                calls.append(("agent_eval", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "completed", "result": "evidence"}))

            registry.register(ToolDef("agent_open", "Open subagent.", {"type": "object"}, agent_open))
            registry.register(ToolDef("agent_eval", "Eval subagent.", {"type": "object"}, agent_eval))
            ctx = ToolContext(None, None, EmptyMemory(), EmptySkills(), None, None, None)
            cfg = LilBotConfig(workspace=Path(tmp), max_steps=10, subagent_max_concurrent=8)
            agent = Agent(cfg, FinalProvider(), registry, ctx)
            list(agent.run_turn(
                "I want to travel in South America during China's National Day; recommend destinations "
                "and build a 10-day itinerary. Also tell me the current FIFA men's world ranking top 25. "
                "Last, how many NBA regular season MVP awards has LeBron James won?"
            ))

        open_calls = [args for name, args in calls if name == "agent_open"]
        self.assertEqual([args["type"] for args in open_calls], ["researcher", "researcher", "researcher"])
        self.assertEqual(
            [args["name"] for args in open_calls],
            ["auto_research_travel", "auto_research_football_rankings", "auto_research_lebron_mvp"],
        )
        self.assertNotIn("auto_plan_synthesis", [args["name"] for args in open_calls])

    @unittest.skip("SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions")
    def test_agent_auto_opens_five_researchers_for_question_burst(self):
        class FinalProvider:
            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                return ProviderTurn(content="final synthesis")

        with tempfile.TemporaryDirectory() as tmp:
            calls: list[tuple[str, dict]] = []
            registry = ToolRegistry()

            def agent_open(args, ctx):
                calls.append(("agent_open", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "running"}), {"name": args["name"]})

            def agent_eval(args, ctx):
                calls.append(("agent_eval", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "completed", "result": "evidence"}))

            registry.register(ToolDef("agent_open", "Open subagent.", {"type": "object"}, agent_open))
            registry.register(ToolDef("agent_eval", "Eval subagent.", {"type": "object"}, agent_eval))
            ctx = ToolContext(None, None, EmptyMemory(), EmptySkills(), None, None, None)
            cfg = LilBotConfig(workspace=Path(tmp), max_steps=10, subagent_max_concurrent=8)
            agent = Agent(cfg, FinalProvider(), registry, ctx)
            events = list(agent.run_turn(
                "What are Beijing coordinates? How long do cats usually live? Who is Feng Gong? "
                "How tall is LeBron James? Which NBA teams has LeBron James played for?"
            ))

        open_calls = [args for name, args in calls if name == "agent_open"]
        eval_calls = [args for name, args in calls if name == "agent_eval"]
        self.assertEqual(len(open_calls), 5)
        self.assertEqual(len(eval_calls), 5)
        self.assertEqual([args["type"] for args in open_calls], ["researcher"] * 5)
        self.assertTrue(all(args["background"] for args in open_calls))
        self.assertEqual([args["name"] for args in eval_calls], [f"auto_question_{idx:02d}" for idx in range(1, 6)])
        self.assertTrue(any(isinstance(event, TextDelta) and "5 independent questions" in event.text for event in events))

    @unittest.skip("SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions")
    def test_agent_uses_semantic_delegation_fallback_for_unlisted_writing_task(self):
        class SemanticProvider:
            def __init__(self):
                self.calls: list[tuple[list[dict], list[dict]]] = []

            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                self.calls.append((messages, tools))
                if not tools:
                    return ProviderTurn(content=json.dumps({
                        "delegate": True,
                        "kind": "writing",
                        "reason": "substantial prose benefits from outline and draft workers",
                        "probes": [
                            {
                                "name": "gufeng-outline",
                                "agent_type": "writer",
                                "prompt": "Create structure, imagery, and tone guidance for a 1000-character classical-style prose piece.",
                                "timeout_ms": 10000,
                            },
                            {
                                "name": "gufeng-draft",
                                "agent_type": "writer",
                                "prompt": "Draft the requested prose using the outline intent and a refined classical style.",
                                "timeout_ms": 15000,
                            },
                        ],
                    }))
                return ProviderTurn(content="final synthesis")

        with tempfile.TemporaryDirectory() as tmp:
            provider = SemanticProvider()
            calls: list[tuple[str, dict]] = []
            registry = ToolRegistry()

            def agent_open(args, ctx):
                calls.append(("agent_open", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "running"}), {"name": args["name"]})

            def agent_eval(args, ctx):
                calls.append(("agent_eval", args))
                return ToolResult(True, json.dumps({"name": args["name"], "status": "completed", "result": "evidence"}))

            registry.register(ToolDef("agent_open", "Open subagent.", {"type": "object"}, agent_open))
            registry.register(ToolDef("agent_eval", "Eval subagent.", {"type": "object"}, agent_eval))
            ctx = ToolContext(None, None, EmptyMemory(), EmptySkills(), None, None, None)
            cfg = LilBotConfig(workspace=Path(tmp), max_steps=8)
            agent = Agent(cfg, provider, registry, ctx)
            events = list(agent.run_turn("请创作一篇古风的1000字散文，要求有山水意象、情绪递进和收束余味"))

        open_calls = [args for name, args in calls if name == "agent_open"]
        eval_calls = [args for name, args in calls if name == "agent_eval"]
        self.assertEqual([args["type"] for args in open_calls], ["writer", "writer"])
        self.assertEqual([args["name"] for args in eval_calls], ["gufeng-outline", "gufeng-draft"])
        self.assertEqual(provider.calls[0][1], [])
        self.assertTrue(any(isinstance(event, TextDelta) and "substantial prose" in event.text for event in events))

    @unittest.skip("SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY: auto-delegation replaced by dynamic tool descriptions")
    def test_auto_delegation_records_internal_observations_not_tool_history(self):
        class FinalProvider:
            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                self.messages = messages
                return ProviderTurn(content="final synthesis")

        provider = FinalProvider()
        with tempfile.TemporaryDirectory() as tmp:
            registry = ToolRegistry()
            registry.register(ToolDef(
                "agent_open",
                "Open subagent.",
                {"type": "object"},
                lambda args, ctx: ToolResult(True, json.dumps({"name": args["name"], "status": "running"})),
            ))
            registry.register(ToolDef(
                "agent_eval",
                "Eval subagent.",
                {"type": "object"},
                lambda args, ctx: ToolResult(True, json.dumps({"name": args["name"], "status": "completed", "result": "evidence"})),
            ))
            ctx = ToolContext(None, None, EmptyMemory(), EmptySkills(), None, None, None)
            agent = Agent(LilBotConfig(workspace=Path(tmp), max_steps=6), provider, registry, ctx)
            list(agent.run_turn("请遍历这个项目代码，分析架构和风险"))

        self.assertFalse(any(message.get("role") == "tool" for message in agent.messages))
        self.assertFalse(any(message.get("tool_calls") for message in agent.messages))
        internal = [message for message in agent.messages if "Internal LilBot orchestration result" in message.get("content", "")]
        self.assertEqual(len(internal), 2)
        self.assertTrue(all(message["role"] == "user" for message in internal))

    def test_compact_does_not_orphan_tool_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = LoopingProvider()
            agent, _ = make_agent(tmp, provider, max_steps=2)

            # Long prefix so summarization actually triggers; the tool pair sits
            # in the recent tail so we can assert it is never orphaned.
            filler = "x " * 2000
            agent.messages = [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "old 1 " + filler},
                {"role": "assistant", "content": "old 1 reply " + filler},
                {"role": "user", "content": "old 2 " + filler},
                {"role": "assistant", "content": "old 2 reply " + filler},
                {"role": "user", "content": "use tools"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_a", "type": "function", "function": {"name": "noop_0", "arguments": "{}"}},
                        {"id": "call_b", "type": "function", "function": {"name": "noop_1", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_a", "content": "a"},
                {"role": "tool", "tool_call_id": "call_b", "content": "b"},
                {"role": "assistant", "content": "after tools"},
                {"role": "user", "content": "current"},
            ]

            message = agent.compact()
            self.assertIn("Compacted context", message)
            # The summary replaces the prefix; the system prompt is preserved.
            self.assertEqual(agent.messages[0]["content"], "system")
            self.assertEqual(agent.messages[1]["role"], "system")
            # No orphaned tool message: every 'tool' role is preceded by an
            # assistant tool_calls message somewhere before it.
            for idx, msg in enumerate(agent.messages):
                if msg.get("role") == "tool":
                    self.assertTrue(
                        any(agent.messages[j].get("tool_calls") for j in range(1, idx)),
                        f"tool message at {idx} has no preceding tool_calls",
                    )

        self.assertEqual(agent.messages[2]["role"], "assistant")
        self.assertEqual([message["role"] for message in agent.messages[2:5]], ["assistant", "tool", "tool"])
        self.assertEqual(agent.messages[3]["tool_call_id"], "call_a")
        self.assertEqual(agent.messages[4]["tool_call_id"], "call_b")

    def test_openai_message_adapter_drops_invalid_tool_history(self):
        provider = OpenAICompatibleProvider(LilBotConfig(workspace=Path("."), provider="deepseek", api_key="test"))
        clean = provider._messages([
            {"role": "system", "content": "system"},
            {"role": "tool", "tool_call_id": "call_missing", "content": "orphan"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "noop_0", "arguments": "{}"}},
                    {"id": "call_b", "type": "function", "function": {"name": "noop_1", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "partial"},
            {"role": "user", "content": "next"},
        ])

        self.assertEqual([message["role"] for message in clean], ["system", "user", "user"])
        self.assertFalse(any(message.get("tool_calls") for message in clean))

    def test_deepseek_reasoning_content_is_preserved_in_history(self):
        class ReasoningProvider:
            def complete(self, messages: list[dict], tools: list[dict]) -> ProviderTurn:
                return ProviderTurn(content="answer", reasoning_content="hidden chain")

        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = make_agent(tmp, ReasoningProvider(), max_steps=2)
            list(agent.run_turn("hello"))

        self.assertEqual(agent.messages[-1]["reasoning_content"], "hidden chain")

        provider = OpenAICompatibleProvider(LilBotConfig(workspace=Path("."), provider="deepseek", api_key="test"))
        clean = provider._messages([
            {"role": "assistant", "content": "answer", "reasoning_content": "hidden chain"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "tool thinking",
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "noop_0", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        ])

        self.assertEqual(clean[0]["reasoning_content"], "hidden chain")
        self.assertEqual(clean[1]["reasoning_content"], "tool thinking")

    def test_provider_reads_deepseek_reasoning_content_from_response(self):
        import httpx

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, url, headers, json):
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "answer",
                                    "reasoning_content": "hidden chain",
                                }
                            }
                        ],
                        "usage": {"total_tokens": 3},
                    },
                    request=httpx.Request("POST", url),
                )

        cfg = LilBotConfig(workspace=Path("."), provider="deepseek", model="deepseek-v4-flash", api_key="test")
        provider = OpenAICompatibleProvider(cfg)
        with patch("httpx.Client", FakeClient):
            turn = provider.complete([{"role": "user", "content": "hello"}], [])

        self.assertEqual(turn.content, "answer")
        self.assertEqual(turn.reasoning_content, "hidden chain")

    def test_provider_http_error_includes_response_body(self):
        import httpx

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, url, headers, json):
                return httpx.Response(
                    400,
                    json={"error": {"message": "bad tool history", "type": "invalid_request_error"}},
                    request=httpx.Request("POST", url),
                )

        cfg = LilBotConfig(workspace=Path("."), provider="deepseek", model="deepseek-v4-pro", api_key="test")
        provider = OpenAICompatibleProvider(cfg)
        with patch("httpx.Client", FakeClient):
            with self.assertRaises(ProviderError) as raised:
                provider.complete([{"role": "user", "content": "hello"}], [])

        self.assertIn("bad tool history", str(raised.exception))
        self.assertIn("model=deepseek-v4-pro", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
