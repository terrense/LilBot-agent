# Assets

## Screenshots

| File | What it shows | How it was produced |
|---|---|---|
| `screenshots/dashboard.png` | The full-screen dashboard at startup: agent panel, 146-tool inventory, work panel, trace, composer | Real capture of `python -m lilbot --provider mock` in a 180x50 ConPTY, rendered from the terminal cell grid |
| `screenshots/coding-session.png` | A real tool run: `exec_shell` executes `python -m pytest -q`, the permission gate is approved, and the test output lands in the trace | Same session, after approving the permission prompt |

These are runtime captures, not mockups. The offline provider drives the same
agent loop, permission gate, and tool execution path as a cloud model; only the
model decisions are deterministic.

To reproduce, run LilBot in a VT-capable terminal against a demo workspace:

```bash
python -m lilbot --provider mock --workspace /path/to/demo
```

Then use `/tools` to inspect the registry or prefix a command with `!` to send it
through the permission gate.

## Adding assets

- Prefer PNG for terminal captures and screenshots; keep the width at or below
  1800 px.
- Do not add generated or illustrated images that could be mistaken for a
  screenshot of the real system.
- Keep provenance in the table above when you add a capture.
