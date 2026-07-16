"""HTTP client for the Drone API.

This module is deliberately NOT shared with the other agent-tool CLIs, and it is
the clearest example of why (see the shared package's README): every rule below
either differs from OpenProject's or outright inverts it.

* **Auth** is ``Authorization: Bearer <token>`` — not basic auth.
* **Pagination inverts.** OpenProject returns an authoritative ``total``, so its
  client must *never* stop on a short page (the server caps pageSize). Drone
  returns a bare JSON array with no total, so a short page **is** the terminator.
  Hoisting that rule into shared code would have been a silent, data-losing bug.
* **500 is not transient.** Drone maps SCM auth failures to ``500`` with body
  ``{"message":"Unauthorized"}`` (verified live: ``POST /api/user/repos`` with no
  SCM token). Retrying that is pointless and hides the real cause.
* **No optimistic locking.** Nothing 409s; there is no ``lockVersion`` dance.
"""

from __future__ import annotations

import json as jsonlib
import random
import time
from typing import Any, Iterator

import httpx

from .errors import (
    ApiError,
    AuthError,
    DryRun,
    NotFoundError,
    NotImplementedOnServer,
    ValidationError,
)

_WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")
_IDEMPOTENT = ("GET", "HEAD", "PUT", "DELETE")

# 429 for everything; 5xx only for idempotent methods. NOTE 500 is absent on
# purpose: Drone returns it for SCM auth failures, which never recover on retry.
_TRANSIENT_STATUS = {429, 502, 503, 504}

_MAX_ATTEMPTS = 4
_PER_PAGE = 100


class Client:
    """A thin, typed wrapper over Drone's REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        dry_run: bool = False,
        user_agent: str = "agent-tool-drone-cli",
    ) -> None:
        self.api_root = base_url.rstrip("/") + "/api"
        self.web_root = base_url.rstrip("/")
        self.token = token
        self.dry_run = dry_run
        self._client = httpx.Client(
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
        )

    # ---- plumbing ----------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.api_root}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        content: str | bytes | None = None,
        raw: bool = False,
    ) -> Any:
        url = self._url(path)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        # --dry-run: one chokepoint in the transport, so every write command --
        # present and future -- gets it for free and none can bypass it. Reads
        # still execute: resolving a commit to a build number must really happen
        # or the printed request would be a guess.
        if self.dry_run and method.upper() in _WRITE_METHODS:
            raise DryRun(
                {
                    "method": method.upper(),
                    "url": url,
                    "params": clean_params or None,
                    "body": json if json is not None else content,
                }
            )

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._client.request(
                    method, url, params=clean_params or None, json=json, content=content
                )
            except httpx.ConnectError as exc:
                # Never reached the server -> safe to retry regardless of method.
                last_exc = exc
                if attempt == _MAX_ATTEMPTS:
                    raise ApiError(f"cannot reach {self.web_root}: {exc}") from exc
                self._backoff(attempt, None)
                continue
            except httpx.HTTPError as exc:
                raise ApiError(f"request failed: {exc}") from exc

            retryable = resp.status_code in _TRANSIENT_STATUS and (
                resp.status_code == 429 or method.upper() in _IDEMPOTENT
            )
            if retryable and attempt < _MAX_ATTEMPTS:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue

            if resp.status_code >= 400:
                self._raise_for_error(resp)
            if raw:
                return resp.text
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                # Not every Drone response is JSON: a wrong /api path returns a
                # plain-text "404 page not found". Hand back the text rather than
                # exploding in json.loads.
                return resp.text
        raise ApiError(f"request failed after {_MAX_ATTEMPTS} attempts: {last_exc}")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        delay = 0.5 * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 30.0) + random.uniform(0, 0.25))

    @staticmethod
    def _raise_for_error(resp: httpx.Response) -> None:
        status = resp.status_code
        try:
            body = resp.json()
            msg = body.get("message") if isinstance(body, dict) else str(body)
        except ValueError:
            body = resp.text
            msg = (resp.text or "").strip() or f"HTTP {status}"

        # Drone maps SCM auth failure to 500 + {"message":"Unauthorized"}. Give
        # that its own message: "your Drone token is fine, your SCM link is dead"
        # is a completely different fix from "log in again", and the raw 500 sends
        # people hunting in the wrong place entirely.
        if status == 500 and isinstance(msg, str) and "unauthorized" in msg.lower():
            raise AuthError(
                "the server rejected its own SCM credentials (HTTP 500 'Unauthorized'). "
                "Your Drone token is probably fine — the Drone user's link to the git "
                "provider is broken or expired. Re-authorise Drone against the SCM, "
                "or check `drone-cli server doctor`.",
                detail=body,
            )
        if status in (401, 403):
            raise AuthError(msg or "unauthorized", detail=body)
        if status == 404:
            raise NotFoundError(msg or "not found", detail=body)
        if status == 400:
            # Drone has no 422; 400 is its validation status.
            raise ValidationError(msg or "invalid request", detail=body)
        if status == 501:
            raise NotImplementedOnServer(
                msg or "this server does not implement that endpoint", detail=body
            )
        raise ApiError(msg or f"HTTP {status}", status=status, detail=body)

    # ---- verbs -------------------------------------------------------

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> Any:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw) -> Any:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self.request("DELETE", path, **kw)

    def version(self) -> Any:
        """Server version. Note: ``/version`` on the WEB root, not ``/api/version``.

        ``GET /api/version`` is a 404 — verified live. It does not exist, despite
        looking like it should.
        """
        return self.request("GET", f"{self.web_root}/version")

    # ---- pagination --------------------------------------------------

    def paginate(self, path: str, *, params: dict | None = None, limit: int = 0) -> Iterator[dict]:
        """Yield items across pages, newest first.

        **The stop rule is the inverse of OpenProject's.** There is no `total`
        anywhere in Drone's API, so a short page IS the end. Copying the
        OpenProject rule ("never stop on a short page") would loop forever.

        `per_page` is clamped to 100 deliberately: `handler/api/repos/builds/
        list.go` resets an out-of-range value to **25** rather than clamping to
        the max, so asking for 500 silently gets you 25 — the opposite of what
        the caller wanted, and a truncation bug that looks like missing data.
        """
        page = 1
        seen = 0
        per_page = min(_PER_PAGE, limit) if limit else _PER_PAGE
        while True:
            batch = self.get(path, params={**(params or {}), "page": page, "per_page": per_page})
            if not isinstance(batch, list) or not batch:
                return
            for item in batch:
                yield item
                seen += 1
                if limit and seen >= limit:
                    return
            if len(batch) < per_page:
                return  # short page == last page. No total to check against.
            page += 1

    def close(self) -> None:
        self._client.close()
