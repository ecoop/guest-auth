# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Mint invite tokens for a list of recipients.

The chore every app behind this middleware repeats: turn a list of names
into tokens, and turn those tokens into links you can send people. The
middleware already defines what a token *is* (an opaque string that keys
``config.invite_tokens``), so minting them belongs here rather than in
each app's ``scripts/`` directory.

Deliberately narrow. This module knows about a names file, a
``{token: label}`` JSON file, and a markdown links file — all local. It
has no opinion on where an app's live allowlist lives, how it is
refreshed, or how a token is revoked; that is the app's business and is
explicitly out of scope (see issue #5).

The one invariant worth stating: generation is **merge-preserving**. A
name that already has a token keeps it, so a link you have already sent
someone never stops working because you re-ran the tool.

Stdlib only, and no import of the middleware — an ops tool shouldn't
require the app's runtime to be importable.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

TOKEN_PREFIX = "tok_"


def mint_token() -> str:
    """A fresh opaque, URL-safe invite token.

    ``tok_``-prefixed so it is recognisable in a log line or a cookie jar,
    and 128 bits of ``secrets`` entropy behind that — the token *is* the
    credential, so it has to be unguessable rather than merely unique.
    """
    return TOKEN_PREFIX + secrets.token_urlsafe(16)


@dataclass(frozen=True)
class Recipient:
    """One line of the names file.

    Attributes:
        label: What the app sees — the value stored against the token and
            surfaced as ``GuestIdentity.recipient``.
        note: A private annotation kept only in the local links file, so
            you can remember which Mike this is without that ending up in
            the app's allowlist.
    """

    label: str
    note: str = ""


@dataclass(frozen=True)
class GenerationResult:
    """What a ``generate()`` run produced.

    Attributes:
        tokens: The full ``{token: label}`` mapping after merging.
        minted: Labels that got a new token this run.
        orphaned: ``(token, label)`` pairs whose label is no longer in the
            names file. Reported, never deleted — see ``generate``.
    """

    tokens: dict[str, str]
    minted: tuple[str, ...] = ()
    orphaned: tuple[tuple[str, str], ...] = ()


def parse_names(text: str) -> list[Recipient]:
    """Parse the names file: one recipient per line.

    Blank lines and lines that are only a comment are skipped. An inline
    ``#`` starts a private note — everything before it is the label,
    everything after is kept local:

        Mike            # last name Goodwin, met at worlds

    Splitting on the first ``#`` means a label can't contain one. That is
    the same trade the original made, and a ``#`` in a person's display
    name is rare enough to be worth the simpler file format.
    """
    recipients: list[Recipient] = []
    for raw in text.splitlines():
        label, _, note = raw.partition("#")
        label, note = label.strip(), note.strip()
        if not label:
            continue
        recipients.append(Recipient(label=label, note=note))
    return recipients


def generate(
    recipients: list[Recipient],
    existing: dict[str, str] | None = None,
) -> GenerationResult:
    """Merge ``recipients`` into an existing ``{token: label}`` mapping.

    A label already present keeps its token. New labels are minted. Labels
    that have *disappeared* from the recipients list are reported in
    ``orphaned`` but left in the mapping — deleting them would revoke
    access, which is a decision for a person and not a side effect of
    regenerating a file.

    Note that identity here is the label string, so renaming someone mints
    them a second token and leaves the first one valid. That is what
    ``orphaned`` exists to make visible.
    """
    tokens = dict(existing or {})
    label_to_token = {label: token for token, label in tokens.items()}

    minted: list[str] = []
    for recipient in recipients:
        if recipient.label in label_to_token:
            continue
        token = mint_token()
        tokens[token] = recipient.label
        label_to_token[recipient.label] = token
        minted.append(recipient.label)

    wanted = {r.label for r in recipients}
    orphaned = tuple(
        (token, label) for token, label in sorted(tokens.items(), key=lambda kv: kv[1])
        if label not in wanted
    )
    return GenerationResult(
        tokens=tokens, minted=tuple(minted), orphaned=orphaned
    )


def render_links(
    tokens: dict[str, str],
    recipients: list[Recipient],
    *,
    base_url: str,
) -> str:
    """Render the shareable invite-links markdown.

    Sorted by label so re-running produces a stable diff. Private notes
    ride along here and nowhere else.
    """
    notes = {r.label: r.note for r in recipients if r.note}
    base = base_url.rstrip("/")
    lines = ["# Invite links", "", f"_Base: {base}_", ""]
    for token, label in sorted(tokens.items(), key=lambda kv: kv[1].lower()):
        note = notes.get(label, "")
        shown = f"**{label}**" + (f" ({note})" if note else "")
        lines.append(f"- {shown} — {base}/?token={token}")
    return "\n".join(lines) + "\n"


def read_tokens_file(path: Path) -> dict[str, str]:
    """Read a ``{token: label}`` JSON file; ``{}`` if it doesn't exist."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        # ValueError, not TypeError: the caller passed a perfectly good
        # Path — it's the file's *contents* that are wrong.
        raise ValueError(f"{path} is not a JSON object")  # noqa: TRY004
    return {str(k): str(v) for k, v in data.items()}


def write_tokens_file(path: Path, tokens: dict[str, str]) -> None:
    """Write the ``{token: label}`` mapping as sorted, pretty JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(tokens, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
