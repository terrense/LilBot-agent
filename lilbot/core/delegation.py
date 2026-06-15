from __future__ import annotations

import re
from dataclasses import dataclass


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


def plan_auto_delegation(user_text: str, max_agents: int = 3) -> DelegationPlan | None:
    text = " ".join(str(user_text).split())
    if max_agents <= 0 or not text:
        return None
    lower = text.casefold()
    if lower.startswith(("/", "!")):
        return None
    if _has_any(lower, NO_DELEGATION_TERMS):
        return None
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
    probes = [
        DelegationProbe(
            name="auto_research_facts",
            agent_type="researcher",
            prompt=(
                f"Parent task: {summary}\n"
                "Research pass: gather factual constraints, useful sources, alternatives, and caveats. "
                "Use web_search/fetch_url when public or current facts matter, and cite URLs for claims."
            ),
            timeout_ms=20000,
        ),
        DelegationProbe(
            name="auto_plan_synthesis",
            agent_type="plan",
            prompt=(
                f"Parent task: {summary}\n"
                "Planning pass: turn the gathered topic into a practical structure, itinerary, decision "
                "framework, or action plan. Identify assumptions and tradeoffs."
            ),
            timeout_ms=12000,
        ),
    ]
    if _has_any(lower, RESEARCH_CRITIC_TERMS):
        probes.append(DelegationProbe(
            name="auto_critic_gaps",
            agent_type="critic",
            prompt=(
                f"Parent task: {summary}\n"
                "Critic pass: look for missing constraints, weak evidence, unsafe recommendations, budget/time "
                "risks, and places where the parent answer should be cautious."
            ),
            timeout_ms=8000,
        ))
    return DelegationPlan(
        reason="multi-source research or recommendation task benefits from fact gathering plus synthesis",
        probes=probes[:max_agents],
    )


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
