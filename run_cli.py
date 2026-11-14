"""CLI entry point.

    python run_cli.py "find the cheapest item and tell me its price" https://site
    python run_cli.py "..." https://site --show --max-steps 15
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from agent.agent import Step, TaskAgent


def _say(text: str = "") -> None:
    """Windows consoles are often cp1252; never let an emoji kill a run."""
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")
    sys.stdout.flush()


def show_step(step: Step) -> None:
    _say(f"\n--- step {step.n}  {step.url}")
    if step.thought:
        _say(f"    think: {step.thought}")
    args = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
    _say(f"    do:    {step.action}({args})")
    _say(f"    got:   {step.result}")
    if step.screenshot:
        _say(f"    shot:  {step.screenshot}")


async def ask_on_stdin(question: str) -> Optional[str]:
    _say(f"\n>>> The agent needs input: {question}")
    _say(">>> Type your answer (or press Enter to stop the run):")
    answer = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
    return (answer or "").strip() or None


async def main() -> int:
    ap = argparse.ArgumentParser(description="Run a web task from the command line.")
    ap.add_argument("task", help="the goal, in plain English")
    ap.add_argument("url", help="the site to start on")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--show", action="store_true", help="run with a visible browser window")
    args = ap.parse_args()

    try:
        agent = TaskAgent(
            ask_user=ask_on_stdin,
            on_step=show_step,
            model=args.model,
            max_steps=args.max_steps,
            headless=False if args.show else None,
        )
    except RuntimeError as exc:
        _say(f"error: {exc}")
        return 2

    _say(f"goal:  {args.task}")
    _say(f"site:  {args.url}")
    _say(f"model: {agent.model}   max steps: {agent.max_steps}")
    _say(f"run:   {agent.run_dir}")

    result = await agent.run(args.task, args.url)

    _say("\n" + "=" * 70)
    _say(f"STATUS:   {result.status.upper()}" + ("  (verified)" if result.verified else ""))
    _say(f"SUMMARY:  {result.summary}")
    if result.evidence:
        _say(f"EVIDENCE: {result.evidence}")
    _say(f"STEPS:    {len(result.steps)}")
    _say(f"TRACE:    {result.run_dir}/trace.json")
    if result.screenshots:
        _say(f"LAST SHOT:{result.screenshots[-1]}")
    _say("=" * 70)
    return 0 if result.ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        _say("\ninterrupted")
        raise SystemExit(130)
