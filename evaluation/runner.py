"""Run each evaluation trial in a fresh workspace and supervised worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from lilbot.config import LilBotConfig

from .dataset import Dataset, TaskSpec, resolve_safe_path
from .grader import grade
from .metrics import MODEL_NAME, classify_failure, make_result, provider_observations, tool_observations
from .trace import TEXT_LIMIT, TraceWriter, publish_trace, read_trace, sanitize


_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from evaluation.runner import _worker; _worker(sys.argv[2])"
)


def run_dataset(
    dataset: Dataset, output_dir: Path, *, live_config: LilBotConfig | None = None,
    on_trial: Callable[[dict], None] | None = None,
) -> list[dict]:
    traces = output_dir.resolve() / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    results = []
    for task in dataset.tasks:
        result = _run_trial(task, dataset, traces, live_config)
        results.append(result)
        if on_trial is not None:
            on_trial(result)
    return results


def _run_trial(
    task: TaskSpec, dataset: Dataset, traces: Path, live_config: LilBotConfig | None,
) -> dict:
    trial_id = uuid4().hex
    destination = traces / f"{trial_id}.jsonl"
    with TemporaryDirectory(prefix="lilbot-eval-") as temp:
        root = Path(temp).resolve()
        workspace = root / "workspace"
        source = root / "trace.jsonl"
        error_type = detail = latency = None
        checks = []
        try:
            workspace.mkdir()
            fixture = task.setup.get("fixture")
            if fixture is not None:
                for relative, data in dataset.fixtures[fixture]:
                    path = resolve_safe_path(workspace, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            latency, error_type, detail = _run_worker(task, trial_id, root, workspace, source, live_config)
        except Exception as exc:
            error_type, detail = "INFRA_ERROR", f"{type(exc).__name__}: {exc}"

        try:
            events = read_trace(source) if source.is_file() else []
        except (ValueError, OSError) as exc:
            error_type, detail = "INFRA_ERROR", f"Unable to read worker trace: {type(exc).__name__}"
            source, events = root / "missing.jsonl", []

        terminal = next((row for row in reversed(events) if row["event_type"] == "runtime_finished"), {})
        worker_error = next((row for row in reversed(events) if row["event_type"] == "worker_error"), None)
        tool_names = [row["tool_name"] for row in events if row["event_type"] == "tool_started"]
        started = any(row["event_type"] == "runtime_started" for row in events)
        reason = terminal.get("termination_reason")
        final_output = sanitize(terminal.get("final_output"))
        provider_stats = provider_observations(events)
        if error_type is None:
            if worker_error is not None:
                error_type, detail = "INFRA_ERROR", worker_error.get("detail")
            elif terminal.get("status") == "AGENT_FAILURE":
                error_type, detail = "AGENT_FAILURE", terminal.get("detail")
            elif terminal.get("status") != "completed":
                error_type, detail = "INFRA_ERROR", "Worker returned no runtime outcome"
            else:
                try:
                    checks = grade(
                        task.graders, workspace, tool_names=tool_names,
                        completed=None if reason is None else reason == "completed",
                        final_output=terminal.get("final_output"), events=events,
                    )
                    if any(not check["passed"] for check in checks):
                        error_type = "AGENT_FAILURE"
                except Exception as exc:
                    error_type, detail = "GRADER_ERROR", f"{type(exc).__name__}: {exc}"

        status = "PASS" if error_type is None else ("FAIL" if error_type == "AGENT_FAILURE" else error_type)
        detail = sanitize(detail)
        publish_trace(source, destination, trial_id, status=status, final_output=final_output, detail=detail)
        result = make_result(
            task_id=task.task_id, trial_id=trial_id, dataset_version=dataset.version,
            success=error_type is None, latency=latency, tool_calls=len(tool_names) if started else None,
            token_usage=(provider_stats["provider_usage_totals"] if live_config else terminal.get("usage")),
            error_type=error_type,
            trace_path=f"traces/{destination.name}", checks=sanitize(checks),
            final_output=None if final_output is None else final_output[:TEXT_LIMIT],
            detail=None if detail is None else detail[:TEXT_LIMIT],
            mode="live" if live_config else "mock",
            model_name=sanitize(live_config.model) if live_config else MODEL_NAME,
            category=task.category,
            risk=task.risk,
        )
        result.update(tool_observations(events))
        result.update(provider_stats)
        result.update({
            "termination_reason": reason,
            "success_criteria": sanitize(task.success_criteria),
            "root_cause": sanitize(classify_failure(error_type, checks, events)),
        })
        return result


def _run_worker(
    task: TaskSpec, trial_id: str, root: Path, workspace: Path, source: Path,
    live_config: LilBotConfig | None,
) -> tuple[float, str | None, str | None]:
    package_root = Path(__file__).resolve().parent.parent
    worker_temp = root / "temp"
    worker_temp.mkdir()
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "SystemDrive") if name in os.environ}
    env.update({name: str(worker_temp) for name in ("TEMP", "TMP", "TMPDIR")})
    provider_settings = None
    if live_config is not None:
        # Send the selected credential only to this isolated worker. The
        # request file, CLI arguments, and report never carry the credential.
        env["LILBOT_API_KEY"] = live_config.api_key
        provider_settings = {
            "provider": live_config.provider,
            "model": live_config.model,
            "base_url": live_config.base_url,
            "context_window": live_config.context_window,
        }
    request = root / "request.json"
    request.write_text(json.dumps({
        "trial_id": trial_id,
        "workspace": str(workspace),
        "trace": str(source),
        "input": task.input,
        "setup": task.setup,
        "max_steps": task.max_steps,
        "provider_settings": provider_settings,
    }), encoding="utf-8")

    # The timeout covers worker startup and execution. subprocess.run kills and
    # reaps this worker before the parent reads partial trace or cleans up files.
    started = perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, str(package_root), str(request)],
            cwd=workspace, env=env, timeout=task.timeout,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return perf_counter() - started, "TIMEOUT", f"Worker exceeded {task.timeout:g} seconds"
    elapsed = perf_counter() - started
    if completed.returncode != 0:
        return elapsed, "INFRA_ERROR", f"Worker exited with code {completed.returncode}"
    return elapsed, None, None


def _worker(request_path: str) -> None:
    from .adapter import LilBotAdapter

    with Path(request_path).open(encoding="utf-8") as handle:
        request = json.load(handle)
    with TraceWriter(Path(request["trace"]), request["trial_id"], staging=True) as trace:
        try:
            workspace = Path(request["workspace"])
            settings = request["provider_settings"]
            live_config = None
            if settings is not None:
                live_config = LilBotConfig(workspace=workspace, api_key=os.environ["LILBOT_API_KEY"], **settings)
            LilBotAdapter().run(
                request["input"], request["setup"], request["max_steps"], workspace, trace,
                live_config=live_config,
            )
        except Exception as exc:
            trace.record("worker_error", status="INFRA_ERROR", detail=f"{type(exc).__name__}: {exc}")
