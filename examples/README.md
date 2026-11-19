# Examples

Small runnable demos that exercise the runtime without a cloud model. Run them
from the repository root so the `lilbot` package is importable.

| Script | What it demonstrates |
|---|---|
| `experiments/delegation_matrix.py` | The delegation router against a matrix of question, exploration, research, and writing prompts |
| `experiments/teams_demo.py` | A lead and two in-process teammates sharing a task board and mailbox |
| `experiments/sort_benchmark.py` | A small benchmark harness used to compare sorting strategies |

```bash
python -m examples.experiments.delegation_matrix
python -m examples.experiments.teams_demo
python -m examples.experiments.sort_benchmark
```

The scripts write any runtime state under the current workspace's `.lilbot/`
directory. They are intentionally small; the evaluation harness in
`evaluation/` is the place for reproducible, graded runs.
