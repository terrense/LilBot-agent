"""
DEPRECATED: Keyword-based auto-delegation superseded by Dynamic Agent Tool Prompt Parity.

The auto-delegation system that pre-launched subagents based on keyword matching
is no longer active (see SPEC_DYNAMIC_AGENT_TOOL_PROMPT_PARITY.md).

Preserved for:
- SEMANTIC_DELEGATION_SYSTEM_PROMPT and semantic_delegation_messages() —
  used by the optional plan_delegation tool for structured planning guidance.
- DelegationPlan / DelegationProbe dataclasses — used by plan_delegation output.
- parse_semantic_delegation_plan() — plan_delegation response parsing.

Keyword tables (CODE_ACTION_TERMS, RESEARCH_TERMS, etc.) and heuristic matchers
(plan_auto_delegation, _code_plan, _research_plan, etc.) are retained for reference
but are no longer called during normal agent operation.

See also: lilbot/subagents/render.py for the replacement dynamic rendering.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DelegationProbe:
    name: str
    agent_type: str
    prompt: str
    timeout_ms: int = 15000


@dataclass(frozen=True)
class DelegationPlan:
    reason: str
    probes: list[DelegationProbe]


# ============================================================
# 【简历·1 Plan-and-Execute 的 Planner】
# 这段 System Prompt + parse_semantic_delegation_plan() 就是“规划器”：
# 让 LLM 判断一个复杂任务是否要拆成多个可并行的子步骤(probes)，并输出
# 结构化 JSON 计划（每个 probe = 一个 agent_type + 一段子任务 brief +
# 超时）。相当于把任务分解成“多步计划 / 近似 DAG”，再由 Executor(主循环)
# 逐个派发给 Specialist 子代理执行。
# 注意：模块顶部 docstring 说明——关键词启发式(plan_auto_delegation 那套
# CODE_ACTION_TERMS 等)已被“动态 Agent 工具描述”取代，现在主要保留的是
# 这条“语义规划”路径（由 plan_delegation 工具按需调用）。
# 面试可讲：从“关键词硬匹配触发拆分”演进到“让模型看实时 agent 描述自主
# 决定拆分”，是一次把规则驱动改成模型驱动、并降低维护成本的架构迭代。
# ============================================================
SEMANTIC_DELEGATION_SYSTEM_PROMPT = """You are LilBot's delegation planner.

Decide whether the parent agent should launch subagents before answering.
Return ONLY a JSON object with this shape:
{
  "delegate": true|false,
  "kind": "question_burst|code|research|writing|planning|review|general",
  "reason": "short reason",
  "probes": [
    {
      "name": "short_lowercase_name",
      "agent_type": "explore|researcher|plan|writer|critic|review|general",
      "prompt": "complete task brief for this subagent",
      "timeout_ms": 15000
    }
  ]
}

Use subagents when work has independent dimensions that benefit from parallelism:
- three or more unrelated questions, even if the user omitted question marks;
- broad codebase exploration, review, architecture mapping, or risk analysis;
- multi-source research, recommendations, comparisons, or current/public facts;
- substantial writing or rewriting, in any genre or language, when outline/draft/critique can be separated;
- planning that has milestones, tradeoffs, risks, or verification.

Do not delegate short direct answers, single known facts, single-file reads, or tasks where one normal tool call is enough.
For question_burst, make one researcher probe per question when possible; group overflow questions only if limited by the max probes.
For writing, do not key off only school essays: prose, poems, speeches, product copy, reports, fiction, and style rewrites can all be writing tasks.
Keep every probe tightly scoped. The parent agent will synthesize; subagents should not ask the user questions.
"""


AGENT_TYPE_ALIASES = {
    "explore": "explore",
    "explorer": "explore",
    "code": "explore",
    "research": "researcher",
    "researcher": "researcher",
    "plan": "plan",
    "planner": "plan",
    "writing": "writer",
    "writer": "writer",
    "critic": "critic",
    "review": "review",
    "reviewer": "review",
    "general": "general",
    "general-purpose": "general",
}

QUESTION_CUE_RE = re.compile(
    r"(谁|哪|什么|多少|几|如何|怎么|为什么|为何|是否|有没有|能不能|可不可以|是不是|吗|呢|呀|who|what|when|where|which|why|how)",
    re.IGNORECASE,
)


NO_DELEGATION_TERMS = (
    "no subagent",
    "no subagents",
    "without subagents",
    "do not use subagents",
    "don't use subagents",
    "不要用subagent",
    "不要用 subagent",
    "不要用子代理",
    "不要用子agent",
    "不用子代理",
    "别用subagent",
    "别用 subagent",
    "别用子代理",
    "不要拆分",
    "不要并行",
)

SMALL_TASK_TERMS = (
    "read file",
    "show file",
    "list files",
    "list dir",
    "grep",
    "打开文件",
    "读取文件",
    "读一下文件",
    "列文件",
    "列出文件",
    "查一个词",
)

CODE_ACTION_TERMS = (
    "analyze",
    "analyse",
    "audit",
    "inspect",
    "investigate",
    "map",
    "review",
    "scan",
    "trace",
    "walk",
    "debug",
    "diagnose",
    "分析",
    "遍历",
    "复查",
    "审查",
    "检查",
    "扫描",
    "梳理",
    "调查",
    "排查",
    "定位",
    "诊断",
)

CODE_OBJECT_TERMS = (
    "architecture",
    "bug",
    "code",
    "codebase",
    "directory",
    "file",
    "folder",
    "path",
    "project",
    "repo",
    "repository",
    "risk",
    "source",
    "subagent",
    "tool",
    "代码",
    "代码库",
    "源码",
    "项目",
    "仓库",
    "架构",
    "风险",
    "路径",
    "目录",
    "文件",
    "工具",
    "智能体",
    "子代理",
)

RESEARCH_TERMS = (
    "compare",
    "current",
    "fact",
    "guide",
    "investigate",
    "latest",
    "recommend",
    "research",
    "source",
    "sources",
    "travel",
    "trip",
    "verify",
    "web",
    "调研",
    "研究",
    "资料",
    "搜集",
    "收集",
    "查资料",
    "查证",
    "核实",
    "对比",
    "比较",
    "推荐",
    "最新",
    "旅游",
    "旅行",
    "攻略",
    "行程",
    "景点",
    "交通",
    "预算",
)

WRITING_TERMS = (
    "article",
    "copy",
    "draft",
    "essay",
    "ielts",
    "outline",
    "polish",
    "rewrite",
    "speech",
    "toefl",
    "write",
    "writing",
    "作文",
    "雅思",
    "托福",
    "论文",
    "文章",
    "文案",
    "演讲稿",
    "草稿",
    "大纲",
    "润色",
    "改写",
    "续写",
    "写作",
)

PLANNING_TERMS = (
    "break down",
    "design",
    "milestone",
    "plan",
    "roadmap",
    "schedule",
    "strategy",
    "方案",
    "计划",
    "规划",
    "路线图",
    "拆解",
    "里程碑",
    "安排",
    "设计",
)

STRONG_PLANNING_TERMS = (
    "break down",
    "decompose",
    "milestone",
    "roadmap",
    "strategy",
    "tradeoff",
    "方案评估",
    "多方案",
    "项目计划",
    "路线图",
    "拆解",
    "里程碑",
    "取舍",
    "实施方案",
)

PLANNING_CONTEXT_TERMS = (
    "project",
    "product",
    "release",
    "system",
    "technical",
    "团队",
    "项目",
    "产品",
    "系统",
    "研发",
    "上线",
    "技术",
)

COMPLEXITY_TERMS = (
    "budget",
    "compare",
    "constraints",
    "multi",
    "outline",
    "pros and cons",
    "risk",
    "tradeoff",
    "交通",
    "预算",
    "阶段",
    "目标",
    "结构",
    "论点",
    "优缺点",
    "取舍",
    "风险",
    "多角度",
    "多个",
    "分别",
    "同时",
    "并且",
    "对比",
)

HEAVY_WRITING_TERMS = ("essay", "ielts", "toefl", "论文", "作文", "雅思", "托福")

REVIEW_TERMS = ("audit", "review", "bug", "risk", "审查", "复查", "风险", "bug")

RESEARCH_CRITIC_TERMS = (
    "risk",
    "safety",
    "unsafe",
    "pitfall",
    "critique",
    "review",
    "评估",
    "审查",
    "风险",
    "安全",
    "避坑",
    "踩坑",
    "注意事项",
)

WRITING_CRITIC_TERMS = (
    "critique",
    "review",
    "feedback",
    "score",
    "revision",
    "suggestion",
    "修改建议",
    "点评",
    "评价",
    "评分",
    "批改",
    "润色建议",
)

PLANNING_CRITIC_TERMS = (
    "risk",
    "tradeoff",
    "pros and cons",
    "critique",
    "review",
    "风险",
    "取舍",
    "优缺点",
    "评估",
    "审查",
    "避坑",
)


def plan_auto_delegation(
    user_text: str,
    max_agents: int = 3,
    max_question_agents: int | None = None,
) -> DelegationPlan | None:
    text = " ".join(str(user_text).split())
    if max_agents <= 0 or not text:
        return None
    lower = text.casefold()
    if lower.startswith(("/", "!")):
        return None
    if _has_any(lower, NO_DELEGATION_TERMS):
        return None
    question_limit = max_agents if max_question_agents is None else max_question_agents
    independent_plan = _independent_question_plan(text, question_limit)
    if independent_plan is not None:
        return independent_plan
    if _looks_small_and_direct(text, lower):
        return None

    if _looks_like_code_task(text, lower):
        return _code_plan(text, lower, max_agents)
    if _looks_like_research_task(text, lower):
        return _research_plan(text, lower, max_agents)
    if _looks_like_writing_task(text, lower):
        return _writing_plan(text, lower, max_agents)
    if _looks_like_planning_task(text, lower):
        return _planning_plan(text, lower, max_agents)
    return None


def should_consult_semantic_delegation(user_text: str) -> bool:
    text = " ".join(str(user_text).split())
    if not text:
        return False
    lower = text.casefold()
    if lower.startswith(("/", "!")) or _has_any(lower, NO_DELEGATION_TERMS):
        return False
    if _looks_small_and_direct(text, lower):
        return False
    if len(text) >= 24:
        return True
    return len(QUESTION_CUE_RE.findall(text)) >= 2


def semantic_delegation_messages(
    user_text: str,
    max_agents: int,
    max_question_agents: int,
) -> list[dict[str, str]]:
    user_prompt = (
        f"User request:\n{user_text}\n\n"
        f"Max probes for normal tasks: {max_agents}\n"
        f"Max probes for question_burst tasks: {max_question_agents}\n\n"
        "Available agent types:\n"
        "- explore: read-only codebase/file investigation\n"
        "- researcher: web/public-fact/current-fact research\n"
        "- plan: planning, decomposition, tradeoffs, milestones\n"
        "- writer: substantial writing, rewriting, style adaptation\n"
        "- critic: independent critique, risk or quality review\n"
        "- review: code review for bugs, regressions, missing tests\n"
        "- general: flexible multi-step worker\n"
    )
    return [
        {"role": "system", "content": SEMANTIC_DELEGATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def parse_semantic_delegation_plan(
    response_text: str,
    max_agents: int,
    max_question_agents: int,
) -> DelegationPlan | None:
    data = _extract_json_object(response_text)
    if not isinstance(data, dict) or not data.get("delegate"):
        return None
    kind = str(data.get("kind") or "").strip().casefold()
    limit = max_question_agents if kind == "question_burst" else max_agents
    if limit <= 0:
        return None
    raw_probes = data.get("probes")
    if not isinstance(raw_probes, list):
        return None
    probes: list[DelegationProbe] = []
    used_names: set[str] = set()
    for index, raw in enumerate(raw_probes[:limit], 1):
        if not isinstance(raw, dict):
            continue
        agent_type = AGENT_TYPE_ALIASES.get(str(raw.get("agent_type") or "").strip().casefold())
        prompt = str(raw.get("prompt") or "").strip()
        if not agent_type or not prompt:
            continue
        name = _unique_probe_name(str(raw.get("name") or f"semantic_{agent_type}_{index}"), index, used_names)
        timeout_ms = _clamp_timeout(raw.get("timeout_ms"))
        probes.append(DelegationProbe(name=name, agent_type=agent_type, prompt=prompt, timeout_ms=timeout_ms))
    if not probes:
        return None
    reason = str(data.get("reason") or "semantic delegation planner recommended subagents").strip()
    return DelegationPlan(reason=reason[:240], probes=probes)


def _extract_json_object(text: str) -> Any:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", stripped)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _unique_probe_name(raw_name: str, index: int, used_names: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_name.strip().lower()).strip("_-")
    if not base:
        base = f"semantic_agent_{index}"
    if base not in used_names:
        used_names.add(base)
        return base
    suffix = 2
    while f"{base}_{suffix}" in used_names:
        suffix += 1
    name = f"{base}_{suffix}"
    used_names.add(name)
    return name


def _clamp_timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = 15000
    return max(5000, min(timeout, 60000))


def _code_plan(text: str, lower: str, max_agents: int) -> DelegationPlan:
    area = _target_area(text)
    summary = _clip(text, 700)
    probes = [
        DelegationProbe(
            name="auto_explore_map",
            agent_type="explore",
            prompt=(
                f"Parent task: {summary}\n"
                f"Target area: {area}.\n"
                "Read-only explorer pass 1: map the project structure, entrypoints, key directories, "
                "and files that deserve closer inspection. Use project_map/list_dir/read_file as needed. "
                "Do not edit files."
            ),
            timeout_ms=20000,
        ),
        DelegationProbe(
            name="auto_explore_search",
            agent_type="explore",
            prompt=(
                f"Parent task: {summary}\n"
                f"Target area: {area}.\n"
                "Read-only explorer pass 2: search/grep for task-relevant symbols, configuration, "
                "tool surfaces, and suspicious coupling. Cite concrete file paths and line evidence. "
                "Do not edit files."
            ),
            timeout_ms=20000,
        ),
        DelegationProbe(
            name="auto_explore_risks",
            agent_type="explore",
            prompt=(
                f"Parent task: {summary}\n"
                f"Target area: {area}.\n"
                "Read-only explorer pass 3: inspect risks, missing tests, permission/tool boundaries, "
                "and likely follow-up work. Prefer read_file/grep_files/git evidence. Do not edit files."
            ),
            timeout_ms=15000,
        ),
    ]
    if not _has_any(lower, REVIEW_TERMS):
        probes = probes[:2]
    return DelegationPlan(
        reason="broad codebase/path analysis benefits from parallel read-only explorers",
        probes=probes[:max_agents],
    )


def _research_plan(text: str, lower: str, max_agents: int) -> DelegationPlan:
    summary = _clip(text, 700)
    probes = _research_topic_probes(text, lower, summary)
    if not probes:
        probes = [
            DelegationProbe(
                name="auto_research_facts",
                agent_type="researcher",
                prompt=(
                    f"Parent task: {summary}\n"
                    "Research only the factual/source-gathering portion of this task. "
                    "Use web_search/fetch_url when public, current, or uncertain facts matter. "
                    "Return compact findings with source URLs, dates, and caveats. Do not write the final parent answer."
                ),
                timeout_ms=20000,
            )
        ]
    return DelegationPlan(
        reason="research task benefits from parallel fact-focused subagents; parent should synthesize from their evidence",
        probes=probes[:max_agents],
    )


def _research_topic_probes(text: str, lower: str, summary: str) -> list[DelegationProbe]:
    probes: list[DelegationProbe] = []
    if _has_text_any(
        lower,
        "travel",
        "trip",
        "itinerary",
        "destination",
        "south america",
        "argentina",
        "tour",
        "旅游",
        "旅行",
        "行程",
        "景点",
        "目的地",
        "南美",
        "国庆",
        "推荐",
    ):
        probes.append(DelegationProbe(
            name="auto_research_travel",
            agent_type="researcher",
            prompt=(
                f"Parent task: {summary}\n"
                "Research only the travel portion. Gather evidence for destination recommendations and itinerary "
                "building blocks: season/weather around the requested dates, practical route options, transport "
                "feasibility, safety/logistics caveats, and useful source URLs. If the user asks for a multi-day "
                "itinerary, collect facts the parent can use to build it; do not answer unrelated sports questions."
            ),
            timeout_ms=22000,
        ))
    if _has_text_any(
        lower,
        "fifa",
        "football ranking",
        "soccer ranking",
        "world ranking",
        "national team ranking",
        "足球",
        "世界排名",
        "排名",
        "国家队",
    ):
        probes.append(DelegationProbe(
            name="auto_research_football_rankings",
            agent_type="researcher",
            prompt=(
                f"Parent task: {summary}\n"
                "Research only the football ranking portion. Find the current/latest FIFA men's national team "
                "world ranking top 25, include the ranking date, and cite the official or strongest available "
                "source URL. Do not answer travel or basketball questions."
            ),
            timeout_ms=18000,
        ))
    if _has_text_any(
        lower,
        "lebron",
        "lebron james",
        "mvp",
        "regular season mvp",
        "nba mvp",
        "勒布朗",
        "詹姆斯",
        "常规赛mvp",
        "常规赛 mvp",
    ):
        probes.append(DelegationProbe(
            name="auto_research_lebron_mvp",
            agent_type="researcher",
            prompt=(
                f"Parent task: {summary}\n"
                "Research only the LeBron James MVP portion. Verify how many NBA regular-season MVP awards "
                "LeBron James has won, include the seasons if available, and cite source URLs. Do not answer "
                "travel or football ranking questions."
            ),
            timeout_ms=15000,
        ))
    if _has_any(lower, RESEARCH_CRITIC_TERMS) and not _has_text_any(lower, "mvp", "ranking", "排名"):
        probes.append(DelegationProbe(
            name="auto_research_risks",
            agent_type="researcher",
            prompt=(
                f"Parent task: {summary}\n"
                "Research only risk/safety/caveat information relevant to the parent request. Use source URLs, "
                "separate facts from advice, and keep the output scoped for parent synthesis."
            ),
            timeout_ms=16000,
        ))
    return _dedupe_probes(probes)


def _dedupe_probes(probes: list[DelegationProbe]) -> list[DelegationProbe]:
    seen: set[str] = set()
    unique: list[DelegationProbe] = []
    for probe in probes:
        if probe.name in seen:
            continue
        seen.add(probe.name)
        unique.append(probe)
    return unique


def _writing_plan(text: str, lower: str, max_agents: int) -> DelegationPlan:
    summary = _clip(text, 700)
    probes = [
        DelegationProbe(
            name="auto_writer_outline",
            agent_type="writer",
            prompt=(
                f"Parent task: {summary}\n"
                "Writing pass 1: analyze audience, purpose, rubric, thesis, structure, tone, and constraints. "
                "Produce a compact outline and success criteria."
            ),
            timeout_ms=10000,
        ),
        DelegationProbe(
            name="auto_writer_draft",
            agent_type="writer",
            prompt=(
                f"Parent task: {summary}\n"
                "Writing pass 2: draft or rewrite the core content. Make style choices explicit and keep "
                "the result usable by the parent answer."
            ),
            timeout_ms=15000,
        ),
    ]
    if _has_any(lower, WRITING_CRITIC_TERMS):
        probes.append(DelegationProbe(
            name="auto_critic_writing",
            agent_type="critic",
            prompt=(
                f"Parent task: {summary}\n"
                "Critic pass: evaluate clarity, structure, persuasiveness, factual risk, and whether the draft "
                "meets the requested rubric or style."
            ),
            timeout_ms=8000,
        ))
    return DelegationPlan(
        reason="substantial writing task benefits from separated outline, draft, and critique passes",
        probes=probes[:max_agents],
    )


def _planning_plan(text: str, lower: str, max_agents: int) -> DelegationPlan:
    summary = _clip(text, 700)
    probes = [
        DelegationProbe(
            name="auto_plan_breakdown",
            agent_type="plan",
            prompt=(
                f"Parent task: {summary}\n"
                "Planner pass: decompose the task into stages, dependencies, milestones, outputs, and verification."
            ),
            timeout_ms=12000,
        ),
    ]
    if _has_any(lower, PLANNING_CRITIC_TERMS):
        probes.append(DelegationProbe(
            name="auto_critic_plan",
            agent_type="critic",
            prompt=(
                f"Parent task: {summary}\n"
                "Critic pass: inspect the plan for hidden constraints, sequencing risks, missing alternatives, "
                "and vague acceptance criteria."
            ),
            timeout_ms=8000,
        ))
    return DelegationPlan(
        reason="multi-step planning task benefits from independent planning and critique",
        probes=probes[:max_agents],
    )


def _independent_question_plan(text: str, max_agents: int) -> DelegationPlan | None:
    if max_agents < 3:
        return None
    questions = _split_independent_questions(text)
    if len(questions) < 3:
        return None
    probes = []
    groups = _group_questions_for_agents(questions, max_agents)
    for index, group in enumerate(groups, 1):
        if len(group) == 1:
            assignment = f"Answer only subquestion {index}: {group[0]}"
        else:
            bullets = "\n".join(f"- {question}" for question in group)
            assignment = f"Answer only subquestion group {index} ({len(group)} questions):\n{bullets}"
        probes.append(DelegationProbe(
            name=f"auto_question_{index:02d}",
            agent_type="researcher",
            prompt=(
                f"Parent request contains multiple independent questions: {_clip(text, 700)}\n"
                f"{assignment}\n"
                "Use web_search/fetch_url when the answer is a public fact, current fact, or could be uncertain. "
                "Return a compact answer with source URLs when web evidence is used."
            ),
            timeout_ms=15000,
        ))
    if len(groups) == len(questions):
        reason = f"{len(questions)} independent questions can be answered in parallel"
    else:
        reason = f"{len(questions)} independent questions can be covered by {len(groups)} parallel researcher subagents"
    return DelegationPlan(
        reason=reason,
        probes=probes,
    )


def _group_questions_for_agents(questions: list[str], max_agents: int) -> list[list[str]]:
    slot_count = min(len(questions), max_agents)
    groups: list[list[str]] = []
    start = 0
    for slot in range(slot_count):
        remaining_questions = len(questions) - start
        remaining_slots = slot_count - slot
        size = (remaining_questions + remaining_slots - 1) // remaining_slots
        groups.append(questions[start:start + size])
        start += size
    return groups


def _split_independent_questions(text: str) -> list[str]:
    normalized = " ".join(str(text).split())
    if not normalized:
        return []
    parts = re.findall(r"[^?？]+[?？]", normalized)
    if len(parts) < 3:
        bullet_parts = re.split(r"(?:^|\s)(?:\d+[.)、]|[-*])\s+", normalized)
        parts = [part for part in bullet_parts if "?" in part or "？" in part]
    if len(parts) < 3:
        parts = _split_question_like_clauses(normalized)
    questions = []
    for index, raw in enumerate(parts):
        question = raw.strip()
        if index == 0 and re.search(r"[:：]", question):
            question = re.split(r"[:：]", question, maxsplit=1)[-1].strip()
        question = re.sub(r"^(?:那|那么|还有|哦对还有|对了还有|另外|顺便|再问|以及|并且|同时)\s*", "", question)
        question = question.strip(" \t\r\n,，;；。")
        if len(question) >= 4 and question not in questions and _looks_question_like(question):
            questions.append(question)
    return questions


def _split_question_like_clauses(text: str) -> list[str]:
    marked = re.sub(
        r"([吗呢呀嘛])\s+(?=(?:那|那么|还有|哦对还有|对了还有|另外|顺便|再问|以及|并且|同时)?\s*"
        r"(?:谁|哪|什么|多少|几|如何|怎么|为什么|为何|是否|有没有|能不能|可不可以|是不是|[A-Za-z]))",
        r"\1|",
        text,
    )
    connector_pattern = (
        r"\s+(?=(?:那|那么|还有|哦对还有|对了还有|另外|顺便|再问|以及|并且|同时)\s*"
        r"(?:谁|哪|什么|多少|几|如何|怎么|为什么|为何|是否|有没有|能不能|可不可以|是不是|[A-Za-z0-9]))"
    )
    marked = re.sub(connector_pattern, "|", marked)
    parts = [part.strip() for part in marked.split("|") if part.strip()]
    if len(parts) >= 3:
        return parts
    return []


def _looks_question_like(text: str) -> bool:
    return bool(QUESTION_CUE_RE.search(text) or re.search(r"[?？]$", text))


def _looks_small_and_direct(text: str, lower: str) -> bool:
    if len(text) > 160:
        return False
    return _has_any(lower, SMALL_TASK_TERMS)


def _looks_like_code_task(text: str, lower: str) -> bool:
    action_score = _count_terms(lower, CODE_ACTION_TERMS)
    object_score = _count_terms(lower, CODE_OBJECT_TERMS)
    has_path = _has_path(text)
    complexity = _complexity_score(text, lower)
    if action_score and object_score and (has_path or complexity >= 1 or len(text) > 70):
        return True
    return action_score + object_score >= 4 and (has_path or len(text) > 45)


def _looks_like_research_task(text: str, lower: str) -> bool:
    score = _count_terms(lower, RESEARCH_TERMS)
    if score >= 3:
        return True
    if score >= 2 and (_complexity_score(text, lower) >= 1 or len(text) > 55):
        return True
    return False


def _looks_like_writing_task(text: str, lower: str) -> bool:
    score = _count_terms(lower, WRITING_TERMS)
    if score == 0:
        return False
    if _has_any(lower, HEAVY_WRITING_TERMS):
        return True
    return score >= 2 and (_complexity_score(text, lower) >= 1 or len(text) > 60)


def _looks_like_planning_task(text: str, lower: str) -> bool:
    score = _count_terms(lower, PLANNING_TERMS)
    if score == 0:
        return False
    strong = _count_terms(lower, STRONG_PLANNING_TERMS)
    if strong and (_complexity_score(text, lower) >= 1 or len(text) > 50):
        return True
    return score >= 2 and _has_any(lower, PLANNING_CONTEXT_TERMS)


def _complexity_score(text: str, lower: str) -> int:
    score = _count_terms(lower, COMPLEXITY_TERMS)
    score += len(re.findall(r"[,;，；、]", text))
    if re.search(r"\b(and|with|plus|versus|vs)\b", lower):
        score += 1
    return score


def _has_any(lower: str, terms: tuple[str, ...]) -> bool:
    return any(term.casefold() in lower for term in terms)


def _has_text_any(lower: str, *terms: str) -> bool:
    return any(term.casefold() in lower for term in terms)


def _count_terms(lower: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if term.casefold() in lower)


def _has_path(text: str) -> bool:
    return bool(re.search(r"([A-Za-z]:\\|/|\\|\.py\b|\.ts\b|\.tsx\b|\.js\b|\.md\b)", text))


def _target_area(text: str) -> str:
    quoted = re.findall(r"`([^`]+)`|'([^']+)'|\"([^\"]+)\"", text)
    for group in quoted:
        value = next((item for item in group if item), "").strip()
        if value:
            return value
    path_match = re.search(r"([A-Za-z]:\\[^\s,，。；;]+|(?:\.{1,2}/|/)[^\s,，。；;]+)", text)
    if path_match:
        return path_match.group(1)
    return "."


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."
