"""Offline test of make_zip.py: the secret scanner and the exclusion rules.

The point of this test is the scanner. A zip that ships a real key is the worst
possible outcome, so the fake keys below are checked one by one.

    python tests/test_make_zip.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import make_zip as M  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# Fake, syntactically valid keys. None of these is real.
FAKE_KEYS = {
    "Groq": "gsk_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6",
    "OpenRouter": "sk-or-v1-" + "0123456789abcdef0123456789abcdef",
    "OpenAI": "sk-proj-" + "AbCdEfGhIjKlMnOpQrStUvWx",
    "Anthropic": "sk-ant-api03-" + "AbCdEfGhIjKlMnOpQrStUvWx",
    "Google": "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6q",
    "Telegram": "8123456789:AAH" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7",
}


def main() -> int:
    tmp = Path("runs/_zip_test")
    tmp.mkdir(parents=True, exist_ok=True)

    # 1. every key shape is caught
    for label, key in FAKE_KEYS.items():
        f = tmp / f"leak_{label}.py"
        f.write_text(f'API_KEY = "{key}"\n', encoding="utf-8")
        found = M.scan([f])
        check(f"catches a {label} key", bool(found), found[0] if found else "NOT CAUGHT")

    # 2. a key hidden in the middle of a file is still caught, with its line
    f = tmp / "buried.py"
    f.write_text("\n".join(["# fine"] * 30 + [f'token = "{FAKE_KEYS["Telegram"]}"']), encoding="utf-8")
    found = M.scan([f])
    check("catches a key buried deep in a file", bool(found))
    check("reports the line number", bool(found) and ":31:" in found[0], found[0] if found else "")

    # 3. placeholders are not secrets
    f = tmp / "env_example.example"
    f.write_text(
        "GROQ_API_KEY=your-groq-key-here\n"
        "TELEGRAM_BOT_TOKEN=your-botfather-token-here\n"
        "MODEL=openai/gpt-oss-120b\n",
        encoding="utf-8",
    )
    check("placeholders are allowed through", M.scan([f]) == [], str(M.scan([f])))

    # 4. ordinary code is not flagged
    f = tmp / "normal.py"
    f.write_text(
        'url = "https://api.groq.com/openai/v1"\n'
        'model = "openai/gpt-oss-120b"\n'
        'sha = "sk-" # a comment about sk- prefixes\n'
        "digits = 1234567890\n",
        encoding="utf-8",
    )
    check("normal code is not flagged", M.scan([f]) == [], str(M.scan([f])))

    # 5. the real .env.example in this repo is clean
    check("the repo's .env.example passes", M.scan([Path(".env.example")]) == [],
          str(M.scan([Path(".env.example")])))

    # 6. exclusion rules
    for bad in ("agent1/Lib/x.py", "__pycache__/x.pyc", "runs/2026/a.png",
                ".env", "x.pyc", "out.zip", ".git/config"):
        check(f"excludes {bad}", M.skip(Path(bad)))
    for good in ("agent/bot.py", "README.md", "reports/t/trace.json",
                 "reports/t/01.png", ".env.example", "tasks.json"):
        check(f"includes {good}", not M.skip(Path(good)))

    # 7. collect() never picks up an excluded path
    files = [str(p.relative_to(M.ROOT)).replace("\\", "/") for p in M.collect()]
    check("collect finds the source", "agent/browser.py" in files and "agent/bot.py" in files)
    check("collect finds the docs it needs", "tasks.json" in files and ".env.example" in files)
    check("collect excludes the venv", not any(f.startswith("agent1/") for f in files))
    check("collect excludes runs/", not any(f.startswith("runs/") for f in files))
    check("collect excludes the real .env", ".env" not in files)
    check("collect excludes caches", not any("__pycache__" in f for f in files))

    # 8. a real build, to a temp name, then checked from the inside
    out = tmp / "build_test.zip"
    code = M.main.__wrapped__ if hasattr(M.main, "__wrapped__") else None
    argv = sys.argv
    sys.argv = ["make_zip.py", "testid", "--out", str(out)]
    try:
        rc = M.main()
    finally:
        sys.argv = argv
    check("a clean tree builds successfully", rc == 0, f"exit {rc}")
    if out.exists():
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
        check("the zip has the code", "agent/agent.py" in names)
        check("the zip has .env.example", ".env.example" in names)
        check("the zip has no .env", ".env" not in names)
        check("the zip has no venv", not any(n.startswith("agent1/") for n in names))
        check("the zip has no runs/", not any(n.startswith("runs/") for n in names))
        check("the zip has no __pycache__", not any("__pycache__" in n for n in names))
        out.unlink()

    # 9. naming
    check("zip is named <id>_challenge-6.zip", f"{'abc123'}_challenge-6.zip" == "abc123_challenge-6.zip")

    for f in tmp.glob("*"):
        f.unlink()
    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
