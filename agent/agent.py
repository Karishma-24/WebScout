"""The perceive -> plan -> act -> verify loop.

Owns the LLM calls and the recovery logic. Talks to the browser only through
BrowserDriver, and knows nothing about any particular website.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent.browser import ActionError, BrowserDriver, Observation, new_run_dir
from agent.prompts import (
    VERIFY_SYSTEM,
    history_block,
    notes_block,
    system_prompt,
    task_prompt,
    verify_prompt,
)

load_dotenv()

# Groq, through its OpenAI-compatible endpoint. The model is text-only: the
# browser's screenshots are saved for the report but never sent to it.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
REASONING_EFFORT = "low"    # these are short, mechanical decisions

HISTORY_WINDOW = 4          # steps of history shown to the model
REPEAT_LIMIT = 3            # identical action N times in a row -> intervene
STALL_LIMIT = 4             # consecutive no-change steps -> give up
WAIT_STREAK_LIMIT = 5       # consecutive waits allowed while the page moves
MAX_NOTES = 25              # working-memory entries kept in the prompt
NOTE_CHARS = 200            # per note

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 5             # network/rate-limit attempts per call
BACKOFF_CAP = 30.0          # seconds

# Asked when the model emits ask_user. Returns the answer, or None if nobody
# can answer right now (the CLI asks on stdin; the bot asks on Telegram).
AskUser = Callable[[str], Awaitable[Optional[str]]]


@dataclass
class Step:
    n: int
    url: str
    title: str
    thought: str
    action: str
    args: dict[str, Any]
    result: str
    changed: Optional[bool] = None
    screenshot: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RunResult:
    status: str                      # done | blocked | needs_input | max_steps | error
    summary: str
    evidence: str = ""
    steps: list[Step] = field(default_factory=list)
    run_dir: str = ""
    screenshots: list[str] = field(default_factory=list)
    verified: Optional[bool] = None

    @property
    def ok(self) -> bool:
        return self.status == "done"


class TaskAgent:
    def __init__(
        self,
        driver: Optional[BrowserDriver] = None,
        ask_user: Optional[AskUser] = None,
        on_step: Optional[Callable[[Step], Any]] = None,
        model: Optional[str] = None,
        max_steps: Optional[int] = None,
        headless: Optional[bool] = None,
        run_dir: Optional[str | Path] = None,
    ) -> None:
        key = (os.getenv("GROQ_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set; copy .env.example to .env")
        self.model = model or os.getenv("GROQ_MODEL") or DEFAULT_MODEL
        # Turned off permanently if the model turns out not to support them.
        self._json_mode = True
        self._reasoning: Optional[str] = (
            os.getenv("REASONING_EFFORT", REASONING_EFFORT).strip() or None
        )
        self.max_steps = int(max_steps or os.getenv("MAX_STEPS", "25"))
        self.headless = (
            headless
            if headless is not None
            else os.getenv("HEADLESS", "true").strip().lower() not in ("0", "false", "no")
        )
        self.run_dir = Path(run_dir) if run_dir else new_run_dir()
        self.driver = driver
        self.ask_user = ask_user
        self.on_step = on_step

        self.client = AsyncOpenAI(
            api_key=key,
            base_url=os.getenv("GROQ_BASE_URL", GROQ_BASE_URL),
            max_retries=0,   # we do our own backoff, so it stays observable
            timeout=90.0,
        )
        self.steps: list[Step] = []
        self._history: list[str] = []
        self._notes: list[str] = []

    # -- entry point -------------------------------------------------------

    async def run(self, goal: str, start_url: str) -> RunResult:
        owned = self.driver is None
        if owned:
            self.driver = BrowserDriver(headless=self.headless, run_dir=self.run_dir)
            await self.driver.start()
        try:
            return await self._loop(goal, start_url)
        finally:
            if owned and self.driver:
                await self.driver.close()

    async def _loop(self, goal: str, start_url: str) -> RunResult:
        drv = self.driver
        assert drv is not None

        try:
            await drv.goto(start_url)
        except ActionError as exc:
            return self._finish("error", f"Could not open {start_url}: {exc}")

        last_fp: Optional[str] = None
        repeats = 0
        waits = 0
        stalls = 0
        last_sig = ""
        nudge = ""   # one-off note handed to the model next step

        for n in range(1, self.max_steps + 1):
            try:
                obs = await drv.observe(f"step{n}")
            except ActionError as exc:
                return self._finish("error", f"Lost the page: {exc}")

            # --- recovery signals, computed before the model sees anything --
            changed = last_fp is None or obs.fingerprint != last_fp
            if last_fp is not None and not changed:
                stalls += 1
            else:
                stalls = 0
            if self.steps:
                self.steps[-1].changed = changed
            if stalls >= STALL_LIMIT:
                return self._finish(
                    "blocked",
                    f"The page stopped responding to my actions after {n - 1} steps "
                    f"(last URL {obs.url}). Nothing I tried changed anything.",
                )

            hints = [nudge] if nudge else []
            nudge = ""
            if last_fp is not None and not changed:
                hints.append(
                    "YOUR LAST ACTION DID NOT CHANGE THE PAGE. Do not repeat it. "
                    "Try a different element, scroll to find what you need, or "
                    "reconsider whether this page can do what you want."
                )
            hints.append(self._overlay_hint(obs))
            last_fp = obs.fingerprint

            # --- plan ---------------------------------------------------
            try:
                plan = await self._decide(goal, start_url, n, obs, [x for x in hints if x])
            except Exception as exc:  # network / provider / bad JSON
                return self._finish("error", f"The model call failed: {_brief(exc)}")

            action = str(plan.get("action", "")).strip().lower()
            thought = str(plan.get("thought", "")).strip()
            self._remember(plan.get("notes"))

            # --- repeated-action detection -------------------------------
            # Waiting on a page that is still moving is the right thing to do,
            # so it must not look like a loop. Waiting on a page that is doing
            # nothing is a loop, and is counted as one.
            sig = json.dumps({k: v for k, v in plan.items() if k not in ("thought", "notes")},
                             sort_keys=True)
            productive_wait = action == "wait" and (changed or obs.busy)
            if productive_wait:
                waits += 1
                repeats = 0
                if waits > WAIT_STREAK_LIMIT:
                    return self._finish(
                        "blocked",
                        f"I waited {waits - 1} times in a row and the content I needed "
                        f"never finished loading. Last page: {obs.url}",
                    )
            else:
                waits = 0
                repeats = repeats + 1 if sig == last_sig else 0
            last_sig = sig
            if repeats >= REPEAT_LIMIT:
                return self._finish(
                    "blocked",
                    f"I kept trying the same thing ({action}) with no progress, so I "
                    f"stopped rather than loop forever. Last page: {obs.url}",
                )

            # --- terminal actions ----------------------------------------
            if action == "done":
                result = str(plan.get("result", "")).strip()
                evidence = str(plan.get("evidence", "")).strip()
                self._record(n, obs, thought, "done", {"result": result}, result)
                ok, why, hint = await self._verify(goal, result, evidence, obs)
                if ok:
                    out = self._finish("done", result, evidence)
                    out.verified = True
                    return out
                # Not proven: keep working, and tell the model exactly why.
                nudge = (
                    f"YOUR 'done' WAS REJECTED BY THE VERIFIER: {why} "
                    f"{('Next: ' + hint) if hint else ''} "
                    "Either carry on until the page really shows the goal met, or "
                    "report 'blocked' if it cannot be done."
                )
                self.steps[-1].result = f"done rejected by verifier: {why}"
                self._history.append(f"claimed done; verifier rejected it ({why})")
                last_fp = None      # re-read the page with fresh eyes
                continue

            if action == "blocked":
                reason = str(plan.get("reason", "")).strip() or "No reason given."
                self._record(n, obs, thought, "blocked", {"reason": reason}, reason)
                return self._finish("blocked", reason)

            if action == "ask_user":
                question = str(plan.get("question", "")).strip() or "What should I do next?"
                self._record(n, obs, thought, "ask_user", {"question": question}, question)
                answer = await self.ask_user(question) if self.ask_user else None
                if not answer:
                    return self._finish("needs_input", question)
                self._history.append(f'asked "{question}" -> user said "{answer}"')
                nudge = f'THE USER ANSWERED YOUR QUESTION "{question}": {answer}'
                last_fp = None
                continue

            # --- browser actions ------------------------------------------
            try:
                result, args = await self._act(action, plan)
                error = None
            except ActionError as exc:
                result, args, error = f"FAILED: {exc}", _args_of(action, plan), str(exc)
            except Exception as exc:
                result, args, error = f"FAILED: {_brief(exc)}", _args_of(action, plan), _brief(exc)

            self._record(n, obs, thought, action, args, result, error=error)

        return self._finish(
            "max_steps",
            f"I ran out of steps ({self.max_steps}) before finishing. "
            f"Last page: {self.steps[-1].url if self.steps else start_url}",
        )

    # -- the two LLM calls -------------------------------------------------

    async def _decide(
        self, goal: str, start_url: str, step: int, obs: Observation, hints: list[str]
    ) -> dict[str, Any]:
        text = "\n\n".join(
            [
                task_prompt(goal, start_url, step, self.max_steps),
                notes_block(self._notes),
                history_block(self._history[-HISTORY_WINDOW:]),
                "\n".join(hints) if hints else "",
                "CURRENT PAGE:",
                obs.to_prompt(),
                "Reply with one JSON action.",
            ]
        ).strip()

        return await self._chat(system_prompt(), text, temperature=0.1, max_tokens=700)

    async def _chat(
        self, system: str, text: str, temperature: float = 0.1, max_tokens: int = 700
    ) -> dict[str, Any]:
        """One decision from the model, as a dict.

        JSON mode does the work; the lenient parser catches the cases it does
        not, and a single corrective retry catches the rest. Only after that do
        we treat the reply as unusable.
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        raw = await self._complete(messages, temperature, max_tokens)
        try:
            return _parse_json(raw)
        except ActionError:
            pass

        # One retry, showing the model its own malformed reply.
        retry = messages + [
            {"role": "assistant", "content": raw[:1000]},
            {
                "role": "user",
                "content": (
                    "That was not valid JSON. Reply again with ONLY a single JSON "
                    "object for one action, no prose and no code fence."
                ),
            },
        ]
        raw2 = await self._complete(retry, 0.0, max_tokens)
        try:
            return _parse_json(raw2)
        except ActionError as exc:
            raise ActionError(f"{exc} (after one retry)") from None

    async def _complete(
        self, messages: list[dict[str, Any]], temperature: float, max_tokens: int
    ) -> str:
        """A chat completion, retrying rate limits and transient server errors."""
        delay = 2.0
        for attempt in range(MAX_RETRIES):
            kw: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if self._json_mode:
                kw["response_format"] = {"type": "json_object"}
            if self._reasoning:
                kw["reasoning_effort"] = self._reasoning
            try:
                resp = await self.client.chat.completions.create(**kw)
                return resp.choices[0].message.content or ""
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                # The model may not accept one of the optional parameters; drop
                # it and try again rather than failing the whole run.
                if status == 400 and self._drop_unsupported(exc):
                    continue
                if status not in RETRY_STATUS or attempt == MAX_RETRIES - 1:
                    raise
                wait = _retry_after(exc) or delay
                await asyncio.sleep(min(wait, BACKOFF_CAP) + random.uniform(0, 0.4))
                delay = min(delay * 2, BACKOFF_CAP)
        raise ActionError("the model call could not be completed")

    def _drop_unsupported(self, exc: BaseException) -> bool:
        """Turn off an optional parameter the model rejected. True if retryable."""
        msg = str(exc).lower()
        json_trouble = (
            "response_format", "json_object", "json mode",
            # Groq rejects its own generation when it does not validate as JSON.
            # The lenient parser handles free-form replies, so drop the
            # constraint rather than lose the run.
            "json_validate_failed", "failed to validate json",
        )
        if self._json_mode and any(t in msg for t in json_trouble):
            self._json_mode = False
            return True
        if self._reasoning and "reasoning" in msg:
            self._reasoning = None
            return True
        return False

    async def _verify(
        self, goal: str, result: str, evidence: str, obs: Observation
    ) -> tuple[bool, str, str]:
        """Second opinion on a claimed 'done'. Never take success on trust."""
        if not evidence:
            return False, "No evidence was given for the claim.", "Quote the page text that proves it."
        try:
            data = await self._chat(
                VERIFY_SYSTEM,
                verify_prompt(goal, result, evidence, obs.to_prompt(), self._notes),
                temperature=0.0,
                max_tokens=300,
            )
        except Exception as exc:
            # A broken verifier must not turn into a false success.
            return False, f"the check could not be run ({_brief(exc)})", ""
        verdict = str(data.get("verdict", "")).strip().lower()
        return verdict == "accept", str(data.get("why", "")).strip(), str(data.get("hint", "")).strip()

    # -- acting ------------------------------------------------------------

    async def _act(self, action: str, plan: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        drv = self.driver
        assert drv is not None
        args = _args_of(action, plan)

        if action == "click":
            return await drv.click(_need_id(plan)), args
        if action == "type":
            return await drv.type(_need_id(plan), str(plan.get("text", ""))), args
        if action == "select":
            return await drv.select(_need_id(plan), str(plan.get("value", ""))), args
        if action == "press":
            return await drv.press(str(plan.get("key", "Enter"))), args
        if action == "goto":
            url = str(plan.get("url", "")).strip()
            if not url:
                raise ActionError("goto needs a url")
            return await drv.goto(url), args
        if action == "wait":
            return await drv.wait(float(plan.get("seconds", 1) or 1)), args
        if action == "scroll":
            return await drv.scroll(str(plan.get("direction", "down"))), args
        if action == "back":
            return await drv.back(), args
        raise ActionError(
            f"'{action}' is not an action I can take. Use one of: click, type, select, "
            "press, goto, wait, scroll, back, ask_user, done, blocked."
        )

    def _remember(self, raw: Any) -> None:
        """Keep what the model asked to remember, in order, without repeats."""
        if not raw:
            return
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            note = re.sub(r"\s+", " ", str(item)).strip()[:NOTE_CHARS]
            if note and note not in self._notes:
                self._notes.append(note)
        del self._notes[:-MAX_NOTES]

    @staticmethod
    def _overlay_hint(obs: Observation) -> str:
        """Cheap pre-check so a consent wall does not eat the whole run."""
        words = ("cookie", "consent", "accept all", "gdpr", "privacy", "subscribe",
                 "newsletter", "sign up for", "we use cookies", "allow all")
        haystack = (obs.text[:1200] + " " + " ".join(e.name for e in obs.elements[:40])).lower()
        if any(w in haystack for w in words):
            return (
                "NOTE: this page may have a cookie/consent banner or a popup. If one is "
                "covering the content, deal with it first (accept or close it)."
            )
        return ""

    # -- bookkeeping -------------------------------------------------------

    def _record(
        self,
        n: int,
        obs: Observation,
        thought: str,
        action: str,
        args: dict[str, Any],
        result: str,
        error: Optional[str] = None,
    ) -> None:
        step = Step(
            n=n,
            url=obs.url,
            title=obs.title,
            thought=thought,
            action=action,
            args=args,
            result=result,
            screenshot=obs.screenshot_path,
            error=error,
        )
        self.steps.append(step)
        self._history.append(f"{action}({_fmt_args(args)}) -> {result}")
        if self.on_step:
            try:
                out = self.on_step(step)
                if asyncio.iscoroutine(out):
                    asyncio.ensure_future(out)
            except Exception:
                pass  # a reporting failure must never kill the run

    def _finish(self, status: str, summary: str, evidence: str = "") -> RunResult:
        shots = [s.screenshot for s in self.steps if s.screenshot]
        out = RunResult(
            status=status,
            summary=summary,
            evidence=evidence,
            steps=self.steps,
            run_dir=str(self.run_dir),
            screenshots=shots,
            verified=True if status == "done" else None,
        )
        self._write_trace(out)
        return out

    def _write_trace(self, out: RunResult) -> None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "trace.json").write_text(
                json.dumps(
                    {
                        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "model": self.model,
                        "status": out.status,
                        "summary": out.summary,
                        "evidence": out.evidence,
                        "verified": out.verified,
                        "notes": self._notes,
                        "steps": [asdict(s) for s in out.steps],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _retry_after(exc: BaseException) -> Optional[float]:
    """Honour the provider's own Retry-After when it sends one."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        for key in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            raw = headers.get(key)
            if raw:
                try:
                    return max(0.0, float(str(raw).rstrip("s")))
                except ValueError:
                    continue
    m = re.search(r"try again in ([0-9.]+)s", str(exc), re.I)
    return float(m.group(1)) if m else None


def _need_id(plan: dict[str, Any]) -> int:
    raw = plan.get("id", plan.get("element_id"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ActionError(
            f"'{raw}' is not a valid element number. Use one of the numbers in "
            "INTERACTIVE ELEMENTS on the current page."
        ) from None


def _args_of(action: str, plan: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "click": ("id",),
        "type": ("id", "text"),
        "select": ("id", "value"),
        "press": ("key",),
        "goto": ("url",),
        "wait": ("seconds",),
        "scroll": ("direction",),
        "back": (),
    }.get(action, tuple(k for k in plan if k not in ("action", "thought")))
    return {k: plan[k] for k in keys if k in plan}


def _fmt_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _parse_json(raw: str) -> dict[str, Any]:
    """Models wrap JSON in fences, prose, or both. Dig the object out."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, depth, data = text.find("{"), 0, None
        if start < 0:
            raise ActionError(f"the model did not return JSON: {text[:200]}") from None
        for i in range(start, len(text)):
            depth += (text[i] == "{") - (text[i] == "}")
            if depth == 0:
                try:
                    data = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    pass
                break
        if data is None:
            raise ActionError(f"the model did not return valid JSON: {text[:200]}") from None
    if not isinstance(data, dict):
        raise ActionError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _brief(exc: BaseException) -> str:
    return re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()[:200]
