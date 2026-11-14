"""Run every task in tasks.json, one after another, and write reports/.

    python run_targets.py                 # all tasks
    python run_targets.py --only the-internet-login saucedemo-locked-out
    python run_targets.py --pause 30      # longer gap if you hit rate limits
    python run_targets.py --list          # show the tasks, run nothing

Each task writes reports/<task-id>/ (trace.json + screenshots) and the whole
batch writes reports/SUMMARY.md. The summary is rewritten after every task, so
a crash or a Ctrl+C still leaves a usable report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from agent.agent import RunResult, TaskAgent

REPORTS = Path("reports")
DEFAULT_PAUSE = 20          # seconds between runs, to stay under rate limits


def _say(text: str = "") -> None:
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")
    sys.stdout.flush()


def load_tasks(path: str = "tasks.json") -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = data["tasks"] if isinstance(data, dict) else data
    for t in tasks:
        missing = {"id", "goal", "url", "expect"} - set(t)
        if missing:
            raise SystemExit(f"task {t.get('id', '?')} is missing {sorted(missing)}")
    return tasks


def out_of_quota(text: str) -> bool:
    """A daily cap is not a transient 429: no amount of waiting helps today."""
    low = (text or "").lower()
    return "429" in low and any(
        s in low for s in ("per day", "tpd", "rpd", "daily", "quota")
    )


def matched(expect: str, status: str) -> bool:
    """Did the run end the way a correct, honest agent should have ended it?"""
    if expect == "done":
        return status == "done"
    if expect == "blocked":
        # Stopping to ask for something it was never given is honest too.
        return status in ("blocked", "needs_input")
    return False


def is_false_success(expect: str, status: str) -> bool:
    """The failure that matters most: claiming success where none was possible."""
    return expect == "blocked" and status == "done"


def write_summary(rows: list[dict[str, Any]], started: float) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    done = sum(1 for r in rows if r["matched"])
    false_wins = [r for r in rows if r["false_success"]]

    out = [
        "# Target task results",
        "",
        f"{done} of {len(rows)} runs matched the expected outcome.  "
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')}, "
        f"{(time.time() - started) / 60:.1f} min total.",
        "",
    ]
    if false_wins:
        out += [
            "> **FALSE SUCCESS: "
            + ", ".join(f"`{r['id']}`" for r in false_wins)
            + "** claimed *done* where the expected outcome was *blocked*. "
            "The agent reported a result it could not have achieved.",
            "",
        ]

    out += [
        "| Task | Status | Expected | Steps | Match | Result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        mark = "yes" if r["matched"] else ("**FALSE SUCCESS**" if r["false_success"] else "no")
        out.append(
            f"| `{r['id']}` | {r['status']} | {r['expect']} | {r['steps']} | {mark} | {r['result']} |"
        )

    out += ["", "## Runs", ""]
    for r in rows:
        out += [
            f"### {r['id']}",
            "",
            f"- **Goal:** {r['goal']}",
            f"- **Site:** {r['url']}",
            f"- **Expected:** {r['expect']}  **Got:** {r['status']}"
            + ("  (verified)" if r["verified"] else ""),
            f"- **Steps:** {r['steps']}  **Time:** {r['seconds']:.0f}s",
            f"- **Result:** {r['result']}",
        ]
        if r["evidence"]:
            out.append(f"- **Evidence from the page:** {r['evidence']}")
        out.append(f"- **Trace:** `{r['dir']}/trace.json`")
        if r["shots"]:
            out.append(f"- **Screenshots:** {len(r['shots'])} in `{r['dir']}/`")
        out.append("")

    (REPORTS / "SUMMARY.md").write_text("\n".join(out), encoding="utf-8")


def row_for(task: dict[str, Any], result: RunResult, seconds: float) -> dict[str, Any]:
    text = (result.summary or "").replace("\n", " ").replace("|", "/")
    return {
        "id": task["id"],
        "goal": task["goal"],
        "url": task["url"],
        "expect": task["expect"],
        "status": result.status,
        "steps": len(result.steps),
        "verified": bool(result.verified),
        "matched": matched(task["expect"], result.status),
        "false_success": is_false_success(task["expect"], result.status),
        "result": text[:300] + ("..." if len(text) > 300 else ""),
        "evidence": (result.evidence or "").replace("\n", " ")[:300],
        "dir": result.run_dir or str(REPORTS / task["id"]),
        "shots": result.screenshots,
        "seconds": seconds,
    }


async def run_one(task: dict[str, Any]) -> dict[str, Any]:
    out_dir = REPORTS / task["id"]
    # Start clean: screenshots left by an earlier attempt would otherwise sit
    # alongside this run's and make the report look like something it is not.
    if out_dir.exists():
        for stale in out_dir.iterdir():
            if stale.is_file():
                stale.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        agent = TaskAgent(run_dir=out_dir)
        result = await agent.run(task["goal"], task["url"])
    except Exception as exc:              # one bad task must not stop the batch
        _say(f"    run failed: {type(exc).__name__}: {exc}")
        result = RunResult(status="error", summary=f"{type(exc).__name__}: {exc}",
                           run_dir=str(out_dir))
    return row_for(task, result, time.time() - started)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Run every task in tasks.json.")
    ap.add_argument("--tasks", default="tasks.json")
    ap.add_argument("--only", nargs="*", default=None, help="task ids to run")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE,
                    help=f"seconds between runs (default {DEFAULT_PAUSE})")
    ap.add_argument("--list", action="store_true", help="list the tasks and exit")
    args = ap.parse_args()

    tasks = load_tasks(args.tasks)
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {t["id"] for t in tasks}
        if unknown:
            raise SystemExit(f"unknown task id(s): {sorted(unknown)}")
        tasks = [t for t in tasks if t["id"] in wanted]

    if args.list:
        for t in tasks:
            _say(f"  {t['id']:<32} expect {t['expect']:<8} {t['url']}")
        return 0

    _say(f"running {len(tasks)} task(s), {args.pause:g}s between runs\n")
    started = time.time()
    rows: list[dict[str, Any]] = []

    for i, task in enumerate(tasks, 1):
        _say(f"[{i}/{len(tasks)}] {task['id']}  (expect {task['expect']})")
        _say(f"    {task['goal'][:110]}")
        row = await run_one(task)
        rows.append(row)
        write_summary(rows, started)          # keep the report current as we go

        flag = "  <-- FALSE SUCCESS" if row["false_success"] else ""
        _say(f"    -> {row['status']} in {row['steps']} steps, "
             f"{'as expected' if row['matched'] else 'NOT as expected'}{flag}")
        _say(f"    {row['result'][:140]}\n")

        # Stop rather than march through the rest: every remaining task would
        # fail the same way, and each one would overwrite a usable report.
        if out_of_quota(row["result"]):
            _say("The model's daily quota is exhausted, so the remaining tasks "
                 "were not attempted.")
            _say(f"Tasks not run: {', '.join(t['id'] for t in tasks[i:]) or 'none'}")
            break

        if i < len(tasks) and args.pause > 0:
            await asyncio.sleep(args.pause)

    ok = sum(1 for r in rows if r["matched"])
    bad = [r["id"] for r in rows if r["false_success"]]
    _say("=" * 70)
    _say(f"{ok}/{len(rows)} matched the expected outcome")
    if bad:
        _say(f"FALSE SUCCESS in: {', '.join(bad)}")
    _say(f"report: {REPORTS / 'SUMMARY.md'}")
    _say("=" * 70)
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        _say("\ninterrupted; reports/SUMMARY.md holds whatever finished")
        raise SystemExit(130)
