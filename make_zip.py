"""Build the submission zip, refusing to ship secrets.

    python make_zip.py <your-id>        # -> <your-id>_challenge-6.zip
    python make_zip.py                  # uses SUBMISSION_ID from the environment

Every text file that would go in the zip is scanned first. If anything looks
like an API key or a bot token, nothing is written and the script says exactly
where the problem is.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# What goes in. Directories are added whole, minus the exclusions below.
INCLUDE_FILES = [
    "README.md",
    "LIMITS.md",
    "CLAUDE.md",
    "tasks.json",
    "requirements.txt",
    ".env.example",
    "run_cli.py",
    "run_targets.py",
    "make_zip.py",
]
INCLUDE_DIRS = ["agent", "tests", "slides", "reports"]

# What never goes in, no matter where it is found.
EXCLUDE_DIRS = {".env", "agent1", ".venv", "venv", "__pycache__", "runs", ".git",
                ".idea", ".vscode", ".pytest_cache", "node_modules"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".zip", ".log"}
EXCLUDE_NAMES = {".env", ".DS_Store"}

# Things that must never appear in a file we ship.
SECRETS = [
    ("Groq API key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("OpenRouter API key", re.compile(r"\bsk-or-[A-Za-z0-9-]{10,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}")),
    # Deliberately looser than the documented shape: a near-miss that gets
    # shipped is far worse than a false alarm that gets looked at.
    ("Telegram bot token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}")),
]
# Placeholders in .env.example and docs are not secrets.
ALLOW = re.compile(r"your-[a-z-]*(key|token)-here|<[^>]+>|xxx+|\.\.\.", re.I)

TEXT_SUFFIX = {".py", ".md", ".json", ".txt", ".example", ".yml", ".yaml", ".cfg", ".ini", ""}


def _say(text: str = "") -> None:
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")


def rel(path: Path) -> str:
    """Path as shown to the user; works whether or not it sits under ROOT."""
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def skip(path: Path) -> bool:
    parts = set(path.parts)
    return (
        bool(parts & EXCLUDE_DIRS)
        or path.name in EXCLUDE_NAMES
        or path.suffix.lower() in EXCLUDE_SUFFIX
    )


def collect() -> list[Path]:
    found: list[Path] = []
    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.is_file() and not skip(p.relative_to(ROOT)):
            found.append(p)
        elif not p.is_file():
            _say(f"  note: {name} is missing, skipping it")
    for name in INCLUDE_DIRS:
        d = ROOT / name
        if not d.is_dir():
            _say(f"  note: {name}/ is missing, skipping it")
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and not skip(p.relative_to(ROOT)):
                found.append(p)
    return found


def scan(paths: list[Path]) -> list[str]:
    """Look for anything key-shaped. Returns a list of human-readable problems."""
    problems: list[str] = []
    for p in paths:
        if p.suffix.lower() not in TEXT_SUFFIX:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{rel(p)}: could not be read ({exc})")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW.search(line):
                continue
            for label, pattern in SECRETS:
                m = pattern.search(line)
                if m:
                    shown = m.group(0)[:6] + "..." + m.group(0)[-4:]
                    problems.append(
                        f"{rel(p)}:{lineno}: looks like a {label} ({shown})"
                    )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the submission zip.")
    ap.add_argument(
        "submission_id",
        nargs="?",
        default=os.getenv("SUBMISSION_ID"),
        help="your id; the zip is named <id>_challenge-6.zip",
    )
    ap.add_argument("--out", default=None, help="override the output path")
    args = ap.parse_args()

    if not args.submission_id:
        _say("error: no id given.\n"
             "  python make_zip.py <your-id>     (or set SUBMISSION_ID in the environment)")
        return 2

    _say("collecting files...")
    files = collect()
    if not files:
        _say("error: nothing to zip")
        return 2

    _say(f"scanning {len(files)} files for secrets...")
    problems = scan(files)
    if problems:
        _say("\nREFUSING TO BUILD THE ZIP. Something key-shaped is in these files:\n")
        for p in problems:
            _say(f"  {p}")
        _say("\nRemove the real values (keep placeholders in .env.example) and run again.")
        return 1

    # Belt and braces: never ship the real .env even if the lists above change.
    shipped = {str(f.relative_to(ROOT)).replace("\\", "/") for f in files}
    if any(s == ".env" or s.endswith("/.env") for s in shipped):
        _say("error: .env is in the file list; refusing to build")
        return 1

    out = Path(args.out) if args.out else ROOT / f"{args.submission_id}_challenge-6.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())

    size = out.stat().st_size / 1_000_000
    _say(f"\nwrote {out.name}  ({len(files)} files, {size:.1f} MB)")
    for name in ("README.md", "LIMITS.md", "tasks.json", ".env.example"):
        _say(f"  {'ok  ' if name in shipped else 'MISSING '}{name}")
    if not any(s.startswith("reports/") for s in shipped):
        _say("  note: reports/ is empty; run run_targets.py before submitting")
    _say("  .env excluded, agent1/ excluded, runs/ excluded, __pycache__ excluded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
