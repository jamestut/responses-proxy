#!/usr/bin/env python3

"""Usage-logging proxy for the OpenAI Responses API.

Intercepts POST /responses SSE streams, extracts token usage from
``response.completed`` events, and appends structured JSONL entries to a log
file while transparently streaming the response back to the client unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("usage-proxy")

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _parse_sse_events(buf: str) -> tuple[list[dict], str]:
    """Return ``(events, remaining_buffer)`` from *buf*.

    Each event dict has ``event`` and ``data`` keys.  Incomplete trailing data
    is returned as the remaining buffer for the next call.
    """
    events: list[dict] = []
    # SSE events are separated by blank lines ("\n\n").
    while "\n\n" in buf:
        raw, buf = buf.split("\n\n", 1)
        event_type = "message"
        data_lines: list[str] = []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if data_lines:
            events.append({"event": event_type, "data": "\n".join(data_lines)})
    return events, buf


def _extract_usage(payload: dict) -> dict | None:
    """Extract usage fields from a ``response.completed`` payload."""
    resp = payload.get("response")
    if not isinstance(resp, dict):
        return None
    usage = resp.get("usage")
    if not isinstance(usage, dict):
        return None
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "response_id": resp.get("id"),
        "model": resp.get("model"),
        "input_tokens": usage.get("input_tokens", 0),
        "cached_tokens": input_details.get("cached_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "reasoning_tokens": output_details.get("reasoning_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _append_usage(log_path: Path, entry: dict) -> None:
    """Append a single JSONL line to *log_path*."""
    line = json.dumps(entry, default=str) + "\n"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(upstream: str, log_path: Path) -> FastAPI:
    client = httpx.AsyncClient(timeout=None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        header = {
            "proxy_start": datetime.now(timezone.utc).isoformat(),
            "upstream": upstream,
        }
        _append_usage(log_path, header)
        logger.info("Proxy started — upstream=%s  log=%s", upstream, log_path)
        yield
        # Shutdown
        await client.aclose()

    app = FastAPI(lifespan=lifespan)

    # --- POST /responses (SSE interception) ----------------------------

    @app.post("/responses")
    @app.post("/v1/responses")
    async def proxy_responses(request: Request) -> Response:
        body = await request.body()
        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }
        url = f"{upstream}/responses"

        async def _stream() -> AsyncIterator[bytes]:
            buf = ""
            completed_seen = False
            async with client.stream("POST", url, content=body, headers=fwd_headers) as resp:
                async for chunk in resp.aiter_bytes():
                    # Feed decoded text into the SSE parser.
                    buf += chunk.decode("utf-8", errors="replace")
                    events, buf = _parse_sse_events(buf)
                    for ev in events:
                        if ev["event"] == "response.completed":
                            completed_seen = True
                            try:
                                payload = json.loads(ev["data"])
                                usage = _extract_usage(payload)
                                if usage:
                                    usage["timestamp"] = datetime.now(
                                        timezone.utc
                                    ).isoformat()
                                    _append_usage(log_path, usage)
                                    logger.info(
                                        "response_id=%s model=%s "
                                        "input=%d cached=%d output=%d "
                                        "reasoning=%d total=%d",
                                        usage["response_id"],
                                        usage["model"],
                                        usage["input_tokens"],
                                        usage["cached_tokens"],
                                        usage["output_tokens"],
                                        usage["reasoning_tokens"],
                                        usage["total_tokens"],
                                    )
                            except (json.JSONDecodeError, KeyError):
                                logger.warning(
                                    "Failed to parse response.completed event"
                                )
                    yield chunk
                # After stream ends, log a warning if no completed event.
                if not completed_seen:
                    entry = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "warning": "stream ended without response.completed",
                    }
                    _append_usage(log_path, entry)
                    logger.warning("Stream ended without response.completed")

        return StreamingResponse(
            _stream(),
            status_code=200,
            media_type="text/event-stream",
        )

    # --- Catch-all pass-through -----------------------------------------

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def passthrough(request: Request, path: str) -> Response:
        url = f"{upstream}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        body = await request.body()
        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }
        resp = await client.request(
            request.method, url, content=body, headers=fwd_headers
        )
        excluded = {
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
        }
        out_headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in excluded
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=out_headers,
        )

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Usage-logging proxy for the OpenAI Responses API"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Local address to bind on"
    )
    parser.add_argument(
        "--port", type=int, default=8318, help="Local port to listen on"
    )
    parser.add_argument(
        "--upstream",
        required=True,
        help="Upstream base URL, include the 'v1' suffix (e.g.: https://api.openai.com/v1)",
    )
    parser.add_argument(
        "--log",
        default="usage-log.jsonl",
        help="Path to JSONL log file",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    upstream = args.upstream.rstrip("/")
    log_path = Path(args.log)

    app = create_app(upstream, log_path)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
