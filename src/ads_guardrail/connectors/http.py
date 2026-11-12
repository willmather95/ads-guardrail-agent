"""A tiny HTTP transport with retries, injectable so tests never touch the network."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class HttpResponse:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library transport. Only used with real credentials; never in tests or the demo."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def request(self, method, url, *, headers=None, params=None, json_body=None) -> HttpResponse:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (https only, fixed hosts)
                raw = resp.read().decode() or "null"
                return HttpResponse(resp.status, json.loads(raw), dict(resp.headers))
        except urllib.error.HTTPError as err:
            raw = err.read().decode() or "null"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"raw": raw}
            return HttpResponse(err.code, body, dict(err.headers or {}))


RETRYABLE = {429, 500, 502, 503, 504}


def request_with_retry(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> HttpResponse:
    """Exponential backoff on 429/5xx. Writes are retried only by callers that are idempotent."""
    attempt = 0
    while True:
        attempt += 1
        resp = transport.request(method, url, **kwargs)
        if resp.status not in RETRYABLE or attempt >= max_attempts:
            return resp
        retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay * 2 ** (attempt - 1)
        sleep(delay)
