from __future__ import annotations

import json
import unittest

from lilbot.core.delegation import (
    parse_semantic_delegation_plan,
    plan_auto_delegation,
    should_consult_semantic_delegation,
)


class DelegationMatrixTests(unittest.TestCase):
    def test_simple_or_forbidden_prompts_do_not_auto_delegate(self):
        prompts = [
            "2+2等于几",
            "show file README.md",
            "不要用 subagent，帮我分析这个项目架构",
        ]

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(plan_auto_delegation(prompt, max_agents=4, max_question_agents=6))

    def test_no_question_mark_chinese_burst_splits_into_parallel_researchers(self):
        prompt = "谁是2025年NBA冠军 那谁是那一年的FMVP呢 哦对还有NBA是哪一个国家的比赛呀"

        plan = plan_auto_delegation(prompt, max_agents=4, max_question_agents=6)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.reason, "3 independent questions can be answered in parallel")
        self.assertEqual([probe.agent_type for probe in plan.probes], ["researcher"] * 3)
        self.assertEqual([probe.name for probe in plan.probes], ["auto_question_01", "auto_question_02", "auto_question_03"])
        self.assertIn("2025年NBA冠军", plan.probes[0].prompt)
        self.assertIn("FMVP", plan.probes[1].prompt)
        self.assertIn("NBA是哪一个国家", plan.probes[2].prompt)

    def test_code_research_and_multi_fact_prompts_have_distinct_agent_shapes(self):
        cases = [
            (
                "code",
                "遍历 lilbot/core，分析 agent loop、权限风险和测试缺口",
                ["explore", "explore", "explore"],
            ),
            (
                "research",
                "帮我做南美旅游推荐，查景点、交通、预算，并对比安全风险",
                ["researcher", "researcher"],
            ),
            (
                "mixed_facts",
                (
                    "国庆节去南美旅游推荐几个目的地，然后说一下目前世界足球排名前25名，"
                    "最后问一下勒布朗詹姆斯拿过几次常规赛MVP"
                ),
                ["researcher", "researcher", "researcher"],
            ),
        ]

        for _name, prompt, expected_types in cases:
            with self.subTest(prompt=prompt):
                plan = plan_auto_delegation(prompt, max_agents=4, max_question_agents=6)
                self.assertIsNotNone(plan)
                assert plan is not None
                self.assertEqual([probe.agent_type for probe in plan.probes], expected_types)

    def test_non_hardcoded_writing_prompt_defers_to_semantic_planner(self):
        prompt = "请创作一篇古风的1000字散文，要求有山水意象、情绪递进和收束余味"

        self.assertIsNone(plan_auto_delegation(prompt, max_agents=3, max_question_agents=5))
        self.assertTrue(should_consult_semantic_delegation(prompt))

    def test_semantic_planner_result_can_create_writer_subagents(self):
        response = json.dumps({
            "delegate": True,
            "kind": "writing",
            "reason": "creative prose benefits from separate outline and draft passes",
            "probes": [
                {
                    "name": "style_outline",
                    "agent_type": "writer",
                    "prompt": "Build imagery, structure, tone, and acceptance criteria for the prose task.",
                    "timeout_ms": 9000,
                },
                {
                    "name": "draft_pass",
                    "agent_type": "writer",
                    "prompt": "Draft the prose using the outline and preserve the requested classical tone.",
                    "timeout_ms": 15000,
                },
            ],
        })

        plan = parse_semantic_delegation_plan(response, max_agents=3, max_question_agents=5)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual([probe.name for probe in plan.probes], ["style_outline", "draft_pass"])
        self.assertEqual([probe.agent_type for probe in plan.probes], ["writer", "writer"])


if __name__ == "__main__":
    unittest.main()
