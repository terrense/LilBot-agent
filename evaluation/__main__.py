"""Run LilBot evaluation from the command line."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from lilbot.config import LilBotConfig, load_config
from lilbot.llm.providers import OpenAICompatibleProvider, choose_provider

from .dataset import load_dataset
from .report import write_report
from .runner import run_dataset
from .trace import sanitize


def _load_live_config() -> LilBotConfig:
    config = load_config(Path.cwd())
    endpoint = urlsplit(config.base_url)
    if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("live evaluation requires a configured endpoint without URL credentials or query parameters")
    loopback = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
    if endpoint.scheme != "https" and not (endpoint.scheme == "http" and loopback):
        raise ValueError("live evaluation requires HTTPS or a loopback HTTP endpoint")
    if not config.api_key or not config.model or config.model == "lilbot-rule-model":
        raise ValueError("live evaluation requires the configured LilBot model and API key")
    provider = choose_provider(config)
    try:
        if not isinstance(provider, OpenAICompatibleProvider):
            raise ValueError("live evaluation requires LilBot's real HTTP provider")
    finally:
        provider.close()
    return config


def _print_trial(result: dict) -> None:
    status = "PASS" if result["success"] else result["error_type"]
    print(f"{result['task_id']} {status}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run a JSONL dataset with the scripted or configured provider")
    run.add_argument("--dataset", required=True, type=Path)
    run.add_argument("--mode", choices=["mock", "live"], default="mock", help="Live explicitly uses the configured paid API")
    run.add_argument("--output", type=Path, help="New output directory (default: .lilbot/evaluation/<run-id>)")
    args = parser.parse_args(argv)

    try:
        dataset = load_dataset(args.dataset, mode=args.mode)
        live_config = _load_live_config() if args.mode == "live" else None
        output = (args.output or Path(".lilbot") / "evaluation" / uuid4().hex).absolute()
        output.mkdir(parents=True, exist_ok=False)
        started_at = datetime.now(timezone.utc).isoformat()
        if live_config is not None:
            print(f"Live model {sanitize(live_config.model)} · {len(dataset.tasks)} trials · {output}", flush=True)
        results = run_dataset(dataset, output, live_config=live_config, on_trial=_print_trial if live_config else None)
        summary = write_report(output, results, metadata={
            "mode": args.mode,
            "dataset": sanitize(args.dataset.name),
            "dataset_version": dataset.version,
            "planned_trials": len(dataset.tasks),
            "started_at_utc": started_at,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "provider": sanitize(live_config.provider) if live_config else None,
            "model": sanitize(live_config.model) if live_config else None,
            "endpoint_host": urlsplit(live_config.base_url).hostname if live_config else None,
        })
    except (ValueError, OSError) as exc:
        print(f"Evaluation setup failed: {sanitize(str(exc))}", file=sys.stderr)
        return 2
    print(f"{summary['passed']} passed, {summary['failed']} failed, {summary['errors']} errors")
    print(f"Report: {output / 'report.md'}")
    if summary["errors"]:
        return 2
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
