# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""guest-auth: a static-allowlist gate for pre-production demos.

A pure-ASGI middleware that validates a per-recipient invite token from
``?token=...``, exchanges it for an httpOnly cookie, and exposes the
resolved identity to downstream code via a ContextVar.

Not a replacement for real authentication — no password handling, no
token rotation, no signed cookies, no account lifecycle. See the README
for the full scope statement.

Public surface (re-exported here so callers only import from
``guest_auth``):

    - ``InviteAuthMiddleware`` — the ASGI middleware class
    - ``GuestAuthConfig`` — Protocol the middleware's ``config`` arg
      must satisfy (``demo_mode: bool``, ``invite_tokens: Mapping``)
    - ``GuestIdentity`` — the resolved (token, recipient) pair
    - ``get_current_guest()`` — read the current request's identity
    - ``COOKIE_NAME`` / ``COOKIE_MAX_AGE`` — cookie contract for tests
    - ``PathScopedContextVarMiddleware`` — generic sibling that
      publishes a path-parsed value onto a caller-supplied ContextVar

The package takes no config from the environment and imports nothing
from any markdown / filesystem library. The host constructs the
middleware with a config object plus pre-rendered ``welcome_html``, and
the middleware serves it verbatim.
"""

from guest_auth.contextvar_middleware import PathScopedContextVarMiddleware
from guest_auth.identity import GuestIdentity, get_current_guest
from guest_auth.middleware import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    GuestAuthConfig,
    InviteAuthMiddleware,
)

__all__ = [
    "COOKIE_MAX_AGE",
    "COOKIE_NAME",
    "GuestAuthConfig",
    "GuestIdentity",
    "InviteAuthMiddleware",
    "PathScopedContextVarMiddleware",
    "get_current_guest",
]
