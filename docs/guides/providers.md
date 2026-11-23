# Provider guide

LilBot is model-agnostic. It speaks the OpenAI Chat Completions shape and ships
presets for common endpoints. Local servers need no API key; the offline provider
needs neither a key nor a network.

## Configuration

Copy `.env.example` to `.env` and set:

| Variable | Meaning |
|---|---|
| `LILBOT_PROVIDER` | Preset name or any custom name paired with `LILBOT_BASE_URL` |
| `LILBOT_MODEL` | Model id served by that endpoint |
| `LILBOT_BASE_URL` | OpenAI-compatible base URL (without `/chat/completions`) |
| `LILBOT_API_KEY` | Key for cloud providers; empty for local servers |
| `LILBOT_FALLBACK_MODEL` | Optional second model for the rest of a turn after a terminal provider failure |
| `LILBOT_AUX_MODEL` | Optional cheaper model for memory recall, extraction, session notes, and delegation planning |

CLI flags override the file:

```bash
python -m lilbot --provider deepseek --model deepseek-chat
python -m lilbot --provider ollama --model qwen2.5:3b
python -m lilbot --provider mock
```

## Presets

| Provider | Base URL | Key |
|---|---|---|
| `deepseek` | `https://api.deepseek.com` | Required |
| `openai` | `https://api.openai.com/v1` | Required |
| `moonshot` / `kimi` | `https://api.moonshot.cn/v1` | Required |
| `zhipu` / `glm` | `https://open.bigmodel.cn/api/paas/v4` | Required |
| `dashscope` / `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Required |
| `openrouter` | `https://openrouter.ai/api/v1` | Required |
| `groq` | `https://api.groq.com/openai/v1` | Required |
| `mistral` | `https://api.mistral.ai/v1` | Required |
| `xai` / `grok` | `https://api.x.ai/v1` | Required |
| `together`, `siliconflow`, `perplexity`, `fireworks`, `anyscale` | Vendor endpoints | Required |
| `ollama` | `http://localhost:11434/v1` | Not required |
| `vllm` | `http://localhost:8000/v1` | Not required |
| `lmstudio` | `http://localhost:1234/v1` | Not required |
| `localai` | `http://localhost:8080/v1` | Not required |
| `mock` | None | Not required |

Any other OpenAI-compatible server works with a custom name and `--base-url`:

```bash
python -m lilbot --provider my-server --base-url http://10.0.0.5:8080/v1 --model my-model
```

## Switching at runtime

```text
/model flash                     # DeepSeek preset
/model deepseek:deepseek-chat    # provider:model
/model openrouter/meta-llama/llama-3.1-70b-instruct
/model qwen2.5:3b                # keep the current provider, change the model
```

## Retries, fallback, and errors

- Transient failures (429, 5xx, timeouts, connection resets) are classified and
  retried with backoff.
- If the main model cannot complete the turn, `LILBOT_FALLBACK_MODEL` can serve
  the rest of the turn on the same endpoint and key.
- Context overflow and output truncation have separate recovery paths; they are
  not treated as generic retries.
- A 401 usually means the key is missing, expired, or sent to the wrong endpoint.
  A 404 usually means the model id or base URL is wrong. A 400 with a tool-schema
  message usually means the model does not support the tools you enabled.

## Failure recovery

Every provider failure is classified before it is shown to the user. The chain
is ordered and bounded:

1. context overflow -> local prune/compact, then a bounded reactive compact;
2. 429/5xx/timeout/network -> backoff retry (honors `Retry-After`);
3. a configured fallback model -> the rest of the turn, once;
4. exhausted -> the classified error is surfaced.

A failure after partial output does not lose the text: the partial answer is
kept, marked incomplete, and never saved as a final answer. A response cut off
at the output limit resumes from the breakpoint; exhausted truncation ends with
an explicit `output_truncated` outcome.

## Cost and budgets

LilBot measures tokens itself and converts them to USD with a local price table
(`lilbot/core/model_cost.py`). The table is a snapshot, not a billing
authority; `/tokens` shows the price source and the table date.

```bash
python -m lilbot --budget-usd 2.50
```

- 80% of the budget: a warning is emitted and a `budget_warning` transition is
  recorded.
- 100%: the runtime stops before the next provider request. The turn that
  already ran is preserved and the reason is visible in `/tokens`.
- `--resume` restores the accumulated total; `/clear` resets it.
- Unknown cloud models show `price unknown`; a local endpoint and the offline
  provider are free.

## Offline provider

`--provider mock` uses a deterministic rule-based provider. It can call a small
set of tools so the loop, permission gate, and TUI can be exercised without a
model. It is not a substitute for a real model; use it for demos, tests, and
debugging the runtime.
