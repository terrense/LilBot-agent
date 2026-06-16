from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lilbot.core.delegation import plan_auto_delegation, should_consult_semantic_delegation


DEFAULT_PROMPTS = [
    "2+2等于几",
    "谁是2025年NBA冠军 那谁是那一年的FMVP呢 哦对还有NBA是哪一个国家的比赛呀",
    "遍历 lilbot/core，分析 agent loop、权限风险和测试缺口",
    "帮我做南美旅游推荐，查景点、交通、预算，并对比安全风险",
    "请创作一篇古风的1000字散文，要求有山水意象、情绪递进和收束余味",
]


def describe_prompt(prompt: str) -> dict[str, object]:
    plan = plan_auto_delegation(prompt, max_agents=4, max_question_agents=6)
    return {
        "prompt": prompt,
        "deterministic_plan": None
        if plan is None
        else {
            "reason": plan.reason,
            "probes": [
                {
                    "name": probe.name,
                    "agent_type": probe.agent_type,
                    "timeout_ms": probe.timeout_ms,
                    "prompt_preview": probe.prompt[:220],
                }
                for probe in plan.probes
            ],
        },
        "semantic_planner_if_no_plan": plan is None and should_consult_semantic_delegation(prompt),
    }


def main() -> int:
    prompts = sys.argv[1:] or DEFAULT_PROMPTS
    print(json.dumps([describe_prompt(prompt) for prompt in prompts], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
