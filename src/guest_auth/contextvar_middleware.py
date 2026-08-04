# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Publish a value parsed from the request path onto a ContextVar.

Sibling of ``guest_auth.middleware``. Same pure-ASGI rationale: a
ContextVar set inside a plain ASGI middleware propagates into the
threadpool where sync endpoints (and their downstream sync calls) run;
one set inside ``BaseHTTPMiddleware`` or a FastAPI dependency does not,
because those run the downstream app in a separate anyio task and the
context copy is taken too early.

The class is deliberately mechanism-only: the host supplies the regex,
the ContextVar, and (optionally) how to turn the match into the stored
value. Bundling this here keeps the "how to publish per-request state"
pattern in one place — the auth ContextVar in ``guest_auth.identity`` and
any host-side path-scoped ContextVar share the same wiring story.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


class PathScopedContextVarMiddleware:
    """Pure-ASGI middleware that publishes a path-parsed value to a ContextVar.

    On every HTTP request, match ``pattern`` against the request path. On
    a match, run ``extractor`` on the ``re.Match`` and store the result on
    ``contextvar`` for the duration of the request. On no match, store
    ``None``. Always reset the ContextVar after the request so values
    never leak between requests on a reused worker.

    Args:
        app: The downstream ASGI app.
        pattern: A compiled ``re.Pattern`` or a string pattern to match
            against ``scope["path"]``. Matched with ``pattern.match``, so
            anchor with ``^`` if you want a prefix match (or leave the
            first character loose to match anywhere the beginning
            aligns).
        contextvar: The ``ContextVar`` to publish the extracted value on.
            Its default should be the "unset" sentinel readers expect
            (typically ``None``).
        extractor: How to turn the ``re.Match`` into the value to store.
            Defaults to ``lambda m: m.group(1)`` — the first capture
            group, which fits the common case of a single id in the
            path.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        pattern: re.Pattern[str] | str,
        contextvar: ContextVar[Any],
        extractor: Callable[[re.Match[str]], Any] = lambda m: m.group(1),
    ) -> None:
        self.app = app
        self.pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        self.contextvar = contextvar
        self.extractor = extractor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        match = self.pattern.match(scope.get("path", ""))
        value = self.extractor(match) if match else None
        token = self.contextvar.set(value)
        try:
            await self.app(scope, receive, send)
        finally:
            self.contextvar.reset(token)
