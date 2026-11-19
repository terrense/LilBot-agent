# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | Yes |
| 0.1.x | No |

## Reporting a vulnerability

Please use GitHub's private security advisory flow:

<https://github.com/terrense/LilBot-agent/security/advisories/new>

Do not open a public issue, and do not include a working exploit in a public
discussion. If the advisory flow is unavailable, contact the maintainer through
the GitHub profile listed in [AUTHORS](AUTHORS).

Include:

- the LilBot version and commit;
- operating system and Python version;
- the provider and permission mode in use;
- a minimal reproduction (prompt, tool call, config, or script);
- the impact you believe the issue has;
- logs with secrets and personal paths removed.

## Scope

Reports are especially useful for:

- permission-gate bypasses, including a tool path that skips `ToolRegistry`;
- workspace path escapes or symlink tricks;
- shell injection or command-classification bypasses, especially through
  PowerShell parsing, redirection, subprocesses, or background launches;
- secret exposure through tool output, child-process environments, session files,
  logs, or error messages;
- prompt-injection paths that turn untrusted repository content into destructive
  tool calls;
- MCP trust-boundary problems (a server that can escape its declared tools);
- dependency vulnerabilities that are reachable from the default runtime.

## Out of scope

- The model saying something incorrect in ordinary conversation.
- A user explicitly running with `--permission-mode accept-all` and approving a
  destructive action.
- Missing OS-level isolation: the sandbox is a path boundary, not a container.
  Reports that require an attacker who already has code execution in the host
  process are usually out of scope, but tell us anyway if the boundary is weaker
  than documented.
- Denial of service from a deliberately huge workspace or model response, unless
  it bypasses an existing budget or guardrail.

## Response

Maintainers will acknowledge a report as soon as practical, confirm the impact,
and coordinate a fix and disclosure. This is a personal project, so response is
best-effort rather than a contractual SLA.

## Safe harbor

We will not pursue legal action for good-faith research that follows this policy,
stays within your own workspace, and avoids privacy violations, data destruction,
and service disruption.
