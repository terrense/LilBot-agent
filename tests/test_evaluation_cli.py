import json
import subprocess
import sys
from pathlib import Path

from evaluation.trace import read_trace


def test_cli_writes_smoke_reports_and_traces(tmp_path, monkeypatch):
    monkeypatch.delenv("LILBOT_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "smoke"
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "evaluation", "run", "--dataset", "evaluation/datasets/smoke.jsonl",
         "--mode", "mock", "--output", str(output)],
        cwd=root, capture_output=True, timeout=30,
    )
    assert completed.returncode == 1, completed.stderr.decode(errors="replace")
    report = (output / "report.md").read_text(encoding="utf-8")
    payload = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert payload["summary"]["passed"] == 4
    assert payload["summary"]["failed"] == 3
    assert payload["summary"]["errors"] == 0
    for result in payload["results"]:
        assert result["trace_path"] in report
        terminal = read_trace(output / result["trace_path"])[-1]
        assert terminal["trial_id"] == result["trial_id"]
        assert terminal["status"] == ("PASS" if result["success"] else "FAIL")
    results = {result["task_id"]: result for result in payload["results"]}
    assert results["claimed_write"]["error_type"] == "AGENT_FAILURE"
    assert results["text_only"]["token_usage"] is None
