"""Publish the same trial results as JSON and Markdown."""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import summarize
from .trace import read_trace, sanitize


def _write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", errors="backslashreplace", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _status(result: dict) -> str:
    if result["success"]:
        return "PASS"
    error = result["error_type"]
    return "FAIL" if error in {None, "AGENT_FAILURE"} else error


def _stat(value: int | float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _failures(output_dir: Path, results: list[dict]) -> list[str]:
    failures = [row for row in results if not row["success"]]
    if not failures:
        return []
    lines = ["## Failed trials", "", "Categories are observed failures or candidate causes with their supporting evidence.", ""]
    for row in failures:
        lines += [f"### {row['task_id']} · {_status(row)}", "", f"[Published trace]({row['trace_path']})", ""]
        diagnostics = {
            "trial_id": row["trial_id"],
            "success_criteria": row.get("success_criteria"),
            "root_cause": row.get("root_cause"),
            "termination_reason": row.get("termination_reason"),
            "detail": row.get("detail"),
            "checks": row["checks"],
        }
        lines += ["```json", json.dumps(diagnostics, indent=2, ensure_ascii=False, allow_nan=False), "```", ""]
        events = read_trace(output_dir / row["trace_path"])
        if events:
            lines += ["Observed timeline", "", "```jsonl", *(_json(event) for event in events), "```", ""]
        else:
            lines += ["No events were observed.", ""]
    return lines


def write_report(output_dir: Path, results: list[dict], *, metadata: dict | None = None) -> dict:
    summary = summarize(results)
    payload = {"schema_version": 1, "summary": summary, "results": results}
    if metadata is not None:
        payload["run"] = sanitize(metadata)
    _write_text(output_dir / "results.json", json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    mode = "live" if any(result.get("mode") == "live" for result in results) else "offline"
    lines = [
        f"# LilBot {mode} evaluation", "",
        f"{summary['passed']} passed · {summary['failed']} failed · {summary['errors']} errors · {summary['total']} trials", "",
    ]
    if "run" in payload:
        run = payload["run"]
        run_details = {key: run[key] for key in ("dataset", "dataset_version", "model", "provider", "planned_trials", "started_at_utc", "finished_at_utc") if key in run}
        lines += ["## Run", "", "```json", json.dumps(run_details, indent=2, ensure_ascii=False), "```", ""]
    lines += [
        "## Measurement", "",
        f"- Success rate {_rate(summary['success_rate'])} over all executed trial rows, including errors.",
        f"- Observed tool failure rate {_rate(summary['tool_failure_rate'])} ({summary['tool_failures']}/{summary['tool_finished']} completed tool results). Rejected and guardrail-blocked results count as failures. Unfinished attempts are excluded.",
        f"- Worker wall time in seconds · median {_stat(summary['latency']['median_seconds'])} · P95 {_stat(summary['latency']['p95_seconds'])} · {summary['latency']['sample_count']} measurements. Includes worker startup and excludes fixture preparation and grading.",
        f"- Provider requests · {summary['provider_requests']} observed starts · {summary['provider_finished']} observed completions · {summary['provider_failures']} failures · {summary['provider_usage_count']} responses with usage.",
        f"- Returned token counters `{_json(summary['token_usage_totals'])}` over {summary['token_usage_coverage']} trials with usage. Counters retain the provider's field names and may overlap.",
        f"- Failure categories `{_json(summary['failure_categories'])}`.",
        "", "## Trials", "",
        "| Task | Status | Latency (s) | Tool calls | Trace |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for result in results:
        latency = "—" if result["latency"] is None else f"{result['latency']:.3f}"
        calls = "—" if result["tool_calls"] is None else str(result["tool_calls"])
        trace = f"[JSONL]({result['trace_path']})"
        lines.append(f"| `{result['task_id']}` | {_status(result)} | {latency} | {calls} | {trace} |")
    lines += ["", "[Per-trial results and metrics](results.json)", ""]
    lines += _failures(output_dir, results)
    _write_text(output_dir / "report.md", "\n".join(lines))
    return summary
