from __future__ import annotations

import math
import os
import re
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path
from statistics import median

from lilbot import __version__

from .grader import GRADER_VERSION

MODEL_NAME = "scripted-mock"


@lru_cache(maxsize=1)
def _git_commit() -> str | None:
    project = Path(__file__).resolve().parent.parent
    repo_overrides = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() not in repo_overrides}
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel", "HEAD"],
            cwd=project, env=env, stderr=subprocess.DEVNULL, encoding="utf-8", timeout=3,
        ).splitlines()
        if len(output) == 2 and Path(output[0]).resolve() == project:
            commit = output[1]
            if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit):
                return commit
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return None


def make_result(
    *, task_id: str, trial_id: str, dataset_version: str, success: bool | None,
    latency: float | None, tool_calls: int | None, token_usage: dict[str, int] | None,
    error_type: str | None, trace_path: str, checks: list[dict],
    final_output: str | None, detail: str | None = None,
    model_name: str | None = MODEL_NAME, mode: str = "mock",
    category: str | None = None, risk: str | None = None, success_criteria: str | None = None,
) -> dict:
    """Assemble one report row from measured values and version identifiers."""
    return {
        "task_id": task_id,
        "trial_id": trial_id,
        "dataset_version": dataset_version,
        "agent_version": __version__,
        "model_name": model_name,
        "mode": mode,
        "category": category,
        "risk": risk,
        "success_criteria": success_criteria,
        "grader_version": GRADER_VERSION,
        "git_commit": _git_commit(),
        "success": success,
        "latency": latency,
        "tool_calls": tool_calls,
        "token_usage": token_usage,
        "error_type": error_type,
        "trace_path": trace_path,
        "checks": checks,
        "final_output": final_output,
        "detail": detail,
    }


def tool_observations(events: list[dict]) -> dict:
    """Count only tool events that reached the trial trace."""
    results = [event.get("status") for event in events if event["event_type"] == "tool_finished"]
    return {
        "tool_finished": sum(status in {"ok", "failed"} for status in results),
        "tool_failures": results.count("failed"),
    }


def provider_observations(events: list[dict]) -> dict:
    """Aggregate returned per-request counters, including partial trial traces."""
    completed = [event for event in events if event["event_type"] == "provider_finished"]
    usages = [event["usage"] for event in completed if event.get("usage")]
    totals: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    for usage in usages:
        totals.update(usage)
        coverage.update(usage.keys())
    return {
        "provider_requests": sum(event["event_type"] == "provider_started" for event in events),
        "provider_finished": len(completed),
        "provider_failures": sum(event.get("status") == "failed" for event in completed),
        "provider_usage_count": len(usages),
        "provider_usage_totals": dict(totals) if usages else None,
        "provider_usage_field_coverage": dict(coverage),
    }


def classify_failure(error_type: str | None, checks: list[dict], events: list[dict]) -> dict | None:
    """Describe the strongest observable failure signal and its evidence."""
    if error_type is None:
        return None
    if error_type == "GRADER_ERROR":
        return {"category": "grader", "certainty": "observed", "signal": "A declared check could not be evaluated."}
    if error_type in {"INFRA_ERROR", "TIMEOUT"}:
        return {"category": "environment", "certainty": "observed", "signal": "The worker failed or exceeded its deadline."}

    failed_checks = [check for check in checks if check["passed"] is False]
    policy = next((check for check in failed_checks if check["type"] == "forbidden_tool_not_called"), None)
    if policy is not None:
        return {"category": "permission", "certainty": "observed", "signal": "A forbidden tool attempt violated the rubric.", "check": policy["index"]}

    provider_succeeded = False
    recovered_tools = {
        check.get("target") for check in checks if check["type"] in {"tool_failed", "guardrail_observed"} and check["passed"]
    }
    for event in reversed(events):
        kind = event.get("failure_kind")
        event_type = event["event_type"]
        status = event.get("status")
        evidence = {"certainty": "hypothesis", "trace_seq": event.get("seq")}
        if event_type == "tool_finished":
            name = event.get("tool_name")
            if status == "ok":
                recovered_tools.add(name)
            if status != "failed" or name in recovered_tools:
                continue
            output = event.get("tool_result") or ""
            if kind == "permission" or output.startswith("Permission denied"):
                return {**evidence, "category": "permission", "signal": "A tool attempt was denied."}
            if kind in {"validation", "model"} or output.startswith("Unknown tool:"):
                return {**evidence, "category": "model", "signal": "A tool request was invalid or unavailable."}
            return {**evidence, "category": "tool", "signal": "A tool returned a failed result."}
        if event_type == "provider_finished":
            if status == "ok":
                provider_succeeded = True
            elif status == "failed" and not provider_succeeded:
                return {**evidence, "category": "environment", "signal": "The model provider request failed.", "reason": event.get("reason")}

    if failed_checks:
        return {"category": "model", "certainty": "hypothesis", "signal": "Observed behavior missed the rubric.", "check": failed_checks[0]["index"]}
    return {"category": "unknown", "certainty": "unknown", "signal": "The trace has no more specific failure signal."}


def summarize(results: list[dict]) -> dict:
    """Aggregate report rows without replacing missing measurements with zeroes."""
    total = len(results)
    passed = sum(row["success"] is True for row in results)
    failed = sum(
        row["success"] is False and row["error_type"] in {None, "AGENT_FAILURE"}
        for row in results
    )
    error_types = Counter(row["error_type"] for row in results if row["error_type"] is not None)
    latencies = sorted(row["latency"] for row in results if row["latency"] is not None)
    tool_counts = [row["tool_calls"] for row in results if row["tool_calls"] is not None]
    tool_finished = sum(row.get("tool_finished", 0) for row in results)
    tool_failures = sum(row.get("tool_failures", 0) for row in results)
    causes = Counter(row["root_cause"]["category"] for row in results if row.get("root_cause"))
    provider_fields: Counter[str] = Counter()
    for row in results:
        provider_fields.update(row.get("provider_usage_field_coverage") or {})
    usage_samples = [row["token_usage"] for row in results if row["token_usage"] is not None]
    usage_totals: Counter[str] = Counter()
    usage_field_coverage: Counter[str] = Counter()
    for usage in usage_samples:
        usage_totals.update(usage)
        usage_field_coverage.update(usage.keys())

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": total - passed - failed,
        "error_types": dict(error_types),
        "success_rate": passed / total if total else None,
        "latency": {
            "sample_count": len(latencies),
            "median_seconds": median(latencies) if latencies else None,
            "p95_seconds": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
        },
        "tool_calls": sum(tool_counts) if tool_counts else None,
        "tool_calls_coverage": len(tool_counts),
        "tool_finished": tool_finished,
        "tool_failures": tool_failures,
        "tool_failure_rate": tool_failures / tool_finished if tool_finished else None,
        "failure_categories": dict(causes),
        "provider_requests": sum(row.get("provider_requests", 0) for row in results),
        "provider_finished": sum(row.get("provider_finished", 0) for row in results),
        "provider_failures": sum(row.get("provider_failures", 0) for row in results),
        "provider_usage_count": sum(row.get("provider_usage_count", 0) for row in results),
        "provider_usage_field_coverage": dict(provider_fields),
        "token_usage_coverage": len(usage_samples),
        "token_usage_totals": dict(usage_totals) if usage_samples else None,
        "token_usage_field_coverage": dict(usage_field_coverage),
    }
