# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Per-request identity resolved by the guest-auth middleware.

The ``GuestIdentity`` dataclass and its backing ContextVar live here so
downstream consumers (observability, per-recipient counters) can import
identity without pulling in the middleware or its Starlette dependencies.

The middleware sets the ContextVar on a valid cookie; consumers call
``get_current_guest()`` to read it. Outside an authenticated request
(including all of local dev), the value is ``None``.

Claims (``role`` / ``scopes``) ride along on the identity when the host
supplies a ``claims_resolver`` to the middleware. guest-auth defines the
*structure* of a claim and never interprets it: any string is a role, any
strings are scopes. Deciding whether a given role may do a given thing is
policy, and policy lives in the host app.
"""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class GuestClaims:
    """Authorization claims resolved for a token.

    The return type of a ``claims_resolver``. A dataclass rather than a
    bare ``(role, scopes)`` tuple so a third claim can be added later
    without breaking every resolver signature in every consumer.

    Attributes:
        role: An opaque role string, or ``None`` if the resolver has no
            role for this token. guest-auth attaches no meaning to the
            value — ``"level5"``, ``"admin"``, and ``"badger"`` are all
            equally valid.
        scopes: Opaque scope strings, or ``None`` if the resolver has no
            scopes claim for this token. A ``tuple`` (not a ``list``) so
            ``GuestIdentity`` stays hashable — see the note there.
    """

    role: str | None = None
    scopes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class GuestIdentity:
    """The resolved identity behind an authenticated request.

    Hashable, and deliberately kept that way: consumers use the identity
    as a dict key / set member for per-recipient attribution, so every
    field must be immutable. That is why ``scopes`` is a ``tuple`` and
    not a ``list`` — a single mutable field silently turns ``hash()``
    into a ``TypeError`` at the first request that carries claims.

    Attributes:
        token: The invite token presented (the credential itself).
        recipient: The human-readable name from the allowlist, e.g.
            ``"Jane Tester"`` — used for per-recipient spend attribution
            and usage reporting.
        role: The role claim, or ``None``. **``None`` means unresolved,
            not unprivileged**: it is what you get when no resolver is
            configured, when the resolver had nothing for this token, and
            when the resolver *failed*. Callers deciding access must map
            ``None`` to their own floor explicitly rather than treating
            it as a privileged default.
        scopes: The scopes claim, or ``None``. Same unresolved-vs-empty
            distinction: ``None`` is "no scopes claim", ``()`` is "the
            resolver returned zero scopes". What either means for access
            is the host's call.
    """

    token: str
    recipient: str
    role: str | None = None
    scopes: tuple[str, ...] | None = None


# A host-supplied callable mapping a token to its claims. May be sync or
# async: the middleware awaits an async resolver and runs a sync one in
# Starlette's threadpool, so a resolver that hits the network or disk
# never blocks the event loop. Returning ``None`` is equivalent to
# returning an empty ``GuestClaims``.
ClaimsResolver = Callable[[str], "GuestClaims | None | Awaitable[GuestClaims | None]"]


# Per-request identity, set by the middleware on a valid cookie and read
# by downstream consumers. None outside an authenticated request —
# including all of local dev.
_invite_identity: ContextVar[GuestIdentity | None] = ContextVar(
    "guest_auth_identity", default=None
)


def get_current_guest() -> GuestIdentity | None:
    """Return the guest identity for the current request, or None.

    Returns ``None`` when demo mode is off, when no valid cookie was
    presented, or when called outside a request context. Call sites that
    want to attribute spend or log usage per recipient consult this.
    """
    return _invite_identity.get()
