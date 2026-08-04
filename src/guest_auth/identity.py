# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Per-request identity resolved by the guest-auth middleware.

The ``GuestIdentity`` dataclass and its backing ContextVar live here so
downstream consumers (observability, per-recipient counters) can import
identity without pulling in the middleware or its Starlette dependencies.

The middleware sets the ContextVar on a valid cookie; consumers call
``get_current_guest()`` to read it. Outside an authenticated request
(including all of local dev), the value is ``None``.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class GuestIdentity:
    """The resolved identity behind an authenticated request.

    Attributes:
        token: The invite token presented (the credential itself).
        recipient: The human-readable name from the allowlist, e.g.
            ``"Jane Tester"`` — used for per-recipient spend attribution
            and usage reporting.
    """

    token: str
    recipient: str


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
