# Copyright (c) 2026 Eric Cooper. Licensed under the MIT License; see LICENSE.
"""Command line for the invite-token generator: ``guest-auth-tokens gen``.

Kept separate from ``guest_auth.invite_tokens`` so the library half stays
free of ``argparse`` and ``subprocess`` — an app importing the middleware
should never pull in CLI machinery, and the generation logic stays
testable without driving a parser.

Everything app-specific is a flag. ``--base-url`` has no default on
purpose: a wrong base silently produces a file full of links that look
correct and don't work, which is worse than being asked for it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from guest_auth.invite_tokens import (
    generate,
    parse_names,
    read_tokens_file,
    render_links,
    write_tokens_file,
)

_TEMPLATE = """\
# One recipient per line. Blank lines and #-only lines are ignored.
# An inline '#' adds a private note, kept in the links file only:
#   Mike   # last name Goodwin, met at worlds
# Re-run `gen` after editing; a name already minted keeps its token.
"""


def _is_gitignored(path: Path) -> bool | None:
    """Is ``path`` matched by a .gitignore? ``None`` if we can't tell.

    ``git check-ignore`` exits 0 for ignored, 1 for not ignored, and 128
    when there's no repository — which is not a problem, just an absence
    of an answer, so it maps to ``None`` and the caller stays quiet.

    Run from the file's own directory, not ours: the output path may well
    live in a different repository than the one the command was invoked
    from, and asking the wrong repo gets a confident wrong answer.
    """
    directory = path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=directory,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _warn_if_not_ignored(paths: list[Path]) -> None:
    """Warn loudly when credential files would be committable.

    Both outputs contain working tokens. Relying on the operator to have
    set up a gitignore is how one of these ends up in a public repo, so
    the tool checks rather than trusting the convention travelled.
    """
    exposed = [p for p in paths if _is_gitignored(p) is False]
    if not exposed:
        return
    print(file=sys.stderr)
    print("  WARNING: these files contain working invite tokens and are", file=sys.stderr)
    print("  NOT matched by a .gitignore:", file=sys.stderr)
    for path in exposed:
        print(f"    {path}", file=sys.stderr)
    print("  Add them to .gitignore before committing.", file=sys.stderr)


def cmd_gen(args: argparse.Namespace) -> int:
    names_path = Path(args.names)
    out_path = Path(args.out)
    links_path = Path(args.links)

    if not names_path.exists():
        names_path.parent.mkdir(parents=True, exist_ok=True)
        names_path.write_text(_TEMPLATE, encoding="utf-8")
        print(f"created template {names_path} — add one name per line, then re-run.")
        return 1

    recipients = parse_names(names_path.read_text(encoding="utf-8"))
    if not recipients:
        print(f"{names_path} has no names (blank or comment-only).", file=sys.stderr)
        return 1

    result = generate(recipients, read_tokens_file(out_path))

    write_tokens_file(out_path, result.tokens)
    links_path.parent.mkdir(parents=True, exist_ok=True)
    links_path.write_text(
        render_links(result.tokens, recipients, base_url=args.base_url),
        encoding="utf-8",
    )

    print(
        f"gen: {len(recipients)} name(s) in {names_path.name}; "
        f"minted {len(result.minted)} new, {len(result.tokens)} total."
    )
    print(f"  tokens → {out_path}")
    print(f"  links  → {links_path}")

    if result.orphaned:
        # Not an error and not cleaned up automatically: these tokens still
        # work, and revoking one is a decision, not a side effect of a re-run.
        print()
        print(f"  {len(result.orphaned)} token(s) whose label is no longer listed:")
        for token, label in result.orphaned:
            print(f"    {token}  {label}")
        print("  They remain valid. Remove them by hand if that's wrong.")

    _warn_if_not_ignored([out_path, links_path])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="guest-auth-tokens",
        description="Mint guest-auth invite tokens for a list of recipients.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser(
        "gen",
        help="mint tokens for a names file; writes a {token: label} JSON and a links file",
    )
    gen.add_argument("--names", required=True, help="input file, one recipient per line")
    gen.add_argument("--out", required=True, help="output {token: label} JSON")
    gen.add_argument("--links", required=True, help="output invite-links markdown")
    gen.add_argument(
        "--base-url",
        required=True,
        help="link base, e.g. https://your-app.example.com (no default on purpose)",
    )
    gen.set_defaults(func=cmd_gen)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
