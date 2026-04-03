# responses-proxy

A lightweight usage-logging proxy for the [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses). It sits between your client and the upstream API, transparently streaming responses back while extracting token-usage data from `response.completed` SSE events and appending structured JSONL entries to a log file.

## How it works

1. Your client sends requests to the proxy instead of the upstream API.
2. `POST /responses` and `POST /v1/responses` are intercepted — the proxy streams the SSE response through unchanged, but parses out token usage from `response.completed` events and logs them.
3. All other HTTP methods and paths are passed through to the upstream without modification.

## Requirements

- Python 3.10+
- Dependencies: `httpx`, `fastapi`, `uvicorn`

Install dependencies:

```bash
pip install httpx fastapi uvicorn
```

## Usage

### Start the proxy

```bash
python responses-proxy.py --upstream https://api.openai.com/v1
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Local address to bind on |
| `--port` | `8318` | Local port to listen on |
| `--upstream` | *(required)* | Upstream base URL (include the `v1` suffix) |
| `--log` | `usage-log.jsonl` | Path to the JSONL log file |

Then point your OpenAI client (e.g. Codex) at `http://127.0.0.1:8318` (without `v1`) instead of the usual API base URL.

### View usage stats

```bash
python print-stats.py
```

**Options:**

| Flag | Description |
|------|-------------|
| `--log <path>` | Path to JSONL log file (default: `usage-log.jsonl`) |
| `--last <window>` | Restrict to a time window, e.g. `24h`, `7d`, `2w` |
| `--no-comma` | Disable thousand separators in output |

Example output:

```
gpt-4o  (42 requests)
  input_tokens: 125,340
  cached_tokens: 98,000
  output_tokens: 8,210
  reasoning_tokens: 0
  total_tokens: 133,550
```

## Log format

The log file (`usage-log.jsonl`) contains one JSON object per line. Two types of entries are written:

- **Proxy start** — recorded on startup with the upstream URL and timestamp.
- **Usage entry** — one per completed response, with fields: `response_id`, `model`, `input_tokens`, `cached_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`, and `timestamp`.
- **Warning entry** — logged when an SSE stream ends without a `response.completed` event.

## Architecture

```
Client → responses-proxy (:8318) → Upstream API
                 ↓
         usage-log.jsonl
```

The proxy is built with FastAPI + httpx. SSE events are parsed incrementally from the stream buffer, so the full response is forwarded to the client without delay.
