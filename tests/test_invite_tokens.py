# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Tests for the invite-token generator and its CLI.

Covers:
    - Minting: prefix, entropy, uniqueness
    - Names-file parsing: labels, inline private notes, blanks and comments
    - Merge-preservation: a known label keeps its token across runs
    - Orphan reporting: a label that disappeared is surfaced, never deleted
    - Links rendering: stable order, notes local-only, base-URL normalisation
    - Tokens file round-trip, absence, and malformed content
    - The CLI end to end, including the gitignore warning
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from guest_auth import mint_token
from guest_auth._invite_cli import _is_gitignored, main
from guest_auth.invite_tokens import (
    Recipient,
    generate,
    parse_names,
    read_tokens_file,
    render_links,
    write_tokens_file,
)

# ── Minting ──────────────────────────────────────────────────────────────────


def test_mint_token_shape_and_uniqueness():
    tokens = {mint_token() for _ in range(200)}
    assert len(tokens) == 200, "minting must not collide"
    for token in tokens:
        assert token.startswith("tok_")
        body = token.removeprefix("tok_")
        assert len(body) >= 20, "at least 128 bits of entropy, URL-safe encoded"
        assert body.replace("-", "").replace("_", "").isalnum()


# ── Names parsing ────────────────────────────────────────────────────────────


def test_parse_names_labels_notes_and_skips():
    text = (
        "# a whole-line comment\n"
        "\n"
        "   \n"
        "Alice\n"
        "Mike   # last name Goodwin, met at worlds\n"
        "  Bob Smith  \n"
    )
    assert parse_names(text) == [
        Recipient("Alice", ""),
        Recipient("Mike", "last name Goodwin, met at worlds"),
        Recipient("Bob Smith", ""),
    ]


# ── Merge behaviour ──────────────────────────────────────────────────────────


def test_generate_mints_for_new_labels():
    result = generate([Recipient("Alice"), Recipient("Bob")])
    assert sorted(result.tokens.values()) == ["Alice", "Bob"]
    assert sorted(result.minted) == ["Alice", "Bob"]
    assert result.orphaned == ()


def test_generate_preserves_an_existing_token():
    """The invariant: a link already sent must not stop working."""
    existing = {"tok_keepme": "Alice"}
    result = generate([Recipient("Alice"), Recipient("Bob")], existing)

    assert result.tokens["tok_keepme"] == "Alice"
    assert result.minted == ("Bob",)
    assert len(result.tokens) == 2


def test_generate_reports_orphans_without_deleting_them():
    """A vanished label is surfaced, not revoked — revocation is a decision."""
    existing = {"tok_gone": "Carol", "tok_here": "Alice"}
    result = generate([Recipient("Alice")], existing)

    assert result.orphaned == (("tok_gone", "Carol"),)
    assert result.tokens["tok_gone"] == "Carol", "still valid until removed by hand"


def test_rename_mints_a_second_token_and_orphans_the_first():
    """Identity is the label string; this is the trap `orphaned` exposes."""
    result = generate([Recipient("Mike G")], {"tok_old": "Mike"})
    assert result.minted == ("Mike G",)
    assert result.orphaned == (("tok_old", "Mike"),)


# ── Links rendering ──────────────────────────────────────────────────────────


def test_render_links_sorted_with_notes_and_clean_base():
    tokens = {"tok_b": "Bob", "tok_a": "alice"}
    recipients = [Recipient("alice", "the Tuesday league"), Recipient("Bob")]

    md = render_links(tokens, recipients, base_url="https://x.example.com/")

    assert "_Base: https://x.example.com_" in md, "trailing slash normalised"
    body = [ln for ln in md.splitlines() if ln.startswith("- ")]
    assert body == [
        "- **alice** (the Tuesday league) — https://x.example.com/?token=tok_a",
        "- **Bob** — https://x.example.com/?token=tok_b",
    ]


def test_notes_never_reach_the_tokens_mapping(tmp_path: Path):
    """A private note is for the links file only — the app must not see it."""
    result = generate([Recipient("Mike", "met at worlds")])
    assert list(result.tokens.values()) == ["Mike"]


# ── Tokens file I/O ──────────────────────────────────────────────────────────


def test_tokens_file_round_trip(tmp_path: Path):
    path = tmp_path / "nested" / "tokens.json"
    write_tokens_file(path, {"tok_a": "Alice"})
    assert read_tokens_file(path) == {"tok_a": "Alice"}


def test_read_tokens_file_absent_is_empty(tmp_path: Path):
    assert read_tokens_file(tmp_path / "nope.json") == {}


def test_read_tokens_file_rejects_non_object(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text('["not", "a", "mapping"]')
    with pytest.raises(ValueError, match="not a JSON object"):
        read_tokens_file(path)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _args(tmp_path: Path) -> list[str]:
    return [
        "gen",
        "--names", str(tmp_path / "names.txt"),
        "--out", str(tmp_path / "tokens.json"),
        "--links", str(tmp_path / "links.md"),
        "--base-url", "https://x.example.com",
    ]


def test_cli_creates_a_template_when_names_file_is_missing(tmp_path: Path, capsys):
    code = main(_args(tmp_path))
    assert code == 1
    assert (tmp_path / "names.txt").exists()
    assert "one name per line" in capsys.readouterr().out


def test_cli_gen_writes_both_files(tmp_path: Path, capsys):
    (tmp_path / "names.txt").write_text("Alice\nMike  # from worlds\n")

    assert main(_args(tmp_path)) == 0

    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert sorted(tokens.values()) == ["Alice", "Mike"]
    links = (tmp_path / "links.md").read_text()
    assert "(from worlds)" in links
    for token in tokens:
        assert f"?token={token}" in links
    assert "minted 2 new" in capsys.readouterr().out


def test_cli_rerun_is_merge_preserving(tmp_path: Path):
    (tmp_path / "names.txt").write_text("Alice\n")
    main(_args(tmp_path))
    first = json.loads((tmp_path / "tokens.json").read_text())

    (tmp_path / "names.txt").write_text("Alice\nBob\n")
    main(_args(tmp_path))
    second = json.loads((tmp_path / "tokens.json").read_text())

    assert first.popitem()[0] in second, "Alice's link must still work"
    assert sorted(second.values()) == ["Alice", "Bob"]


def test_cli_reports_orphans(tmp_path: Path, capsys):
    (tmp_path / "names.txt").write_text("Alice\nCarol\n")
    main(_args(tmp_path))
    (tmp_path / "names.txt").write_text("Alice\n")

    assert main(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "no longer listed" in out
    assert "Carol" in out
    assert "remain valid" in out


def test_cli_requires_a_base_url(tmp_path: Path):
    """No default: a wrong base yields links that look fine and don't work."""
    with pytest.raises(SystemExit):
        main(["gen", "--names", "n", "--out", "o", "--links", "l"])


# ── gitignore guard ──────────────────────────────────────────────────────────


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("secrets/\n")
    return tmp_path


def test_is_gitignored_distinguishes_the_three_answers(git_repo: Path):
    """Ignored / not ignored / no answer — the third must stay quiet.

    Note the paths are resolved against the *file's* repository, not the
    process cwd (which, running the suite, is guest-auth's own repo).
    """
    assert _is_gitignored(git_repo / "secrets" / "tokens.json") is True
    assert _is_gitignored(git_repo / "tokens.json") is False
    assert _is_gitignored(Path(git_repo.anchor) / "definitely-not-a-repo") is None


def test_cli_warns_when_outputs_are_not_gitignored(git_repo: Path, capsys):
    (git_repo / "names.txt").write_text("Alice\n")
    code = main(
        [
            "gen",
            "--names", str(git_repo / "names.txt"),
            "--out", str(git_repo / "tokens.json"),      # not ignored
            "--links", str(git_repo / "links.md"),       # not ignored
            "--base-url", "https://x.example.com",
        ]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "tokens.json" in err and "links.md" in err


def test_cli_is_quiet_when_outputs_are_gitignored(git_repo: Path, capsys):
    (git_repo / "names.txt").write_text("Alice\n")
    code = main(
        [
            "gen",
            "--names", str(git_repo / "names.txt"),
            "--out", str(git_repo / "secrets" / "tokens.json"),
            "--links", str(git_repo / "secrets" / "links.md"),
            "--base-url", "https://x.example.com",
        ]
    )
    assert code == 0
    assert "WARNING" not in capsys.readouterr().err
