"""Offline test of agent/bot.py: URL parsing, the auth gate, step reporting,
ask_user over chat, busy/stop handling and the final message.

Telegram is replaced by a fake bot that records what would have been sent, and
the agent is replaced by a scripted stand-in, so nothing here touches the
network or spends API calls.

    python tests/test_bot.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "offline-test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "offline:token")

from agent import bot as B  # noqa: E402
from agent.agent import RunResult, Step  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeBot:
    def __init__(self):
        self.texts: list[str] = []
        self.photos: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.texts.append(text)

    async def send_photo(self, chat_id, photo=None, caption="", **kw):
        self.photos.append((getattr(photo, "name", "?"), caption))

    async def send_chat_action(self, *a, **kw):
        pass


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def make_update(text, user_id=42, chat_id=7):
    msg = FakeMessage(text)
    return SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def make_ctx(app, bot):
    return SimpleNamespace(application=app, bot=bot, error=None)


def step(n, action="click", result="clicked [1] Login", shot=None):
    return Step(n=n, url="http://x", title="t", thought="because",
                action=action, args={"id": 1}, result=result, screenshot=shot)


async def main() -> int:
    # --- URL / task splitting -----------------------------------------
    cases = [
        ("find the cheapest item https://www.saucedemo.com",
         "find the cheapest item", "https://www.saucedemo.com"),
        ("https://example.com/path?q=1 log in and check the cart",
         "log in and check the cart", "https://example.com/path?q=1"),
        ("search for playwright on wikipedia.org", "search for playwright on", "https://wikipedia.org"),
        ("check www.bbc.co.uk for headlines", "check for headlines", "https://www.bbc.co.uk"),
    ]
    for text, want_goal, want_url in cases:
        goal, url = B.split_task_and_url(text)
        check(f"splits {text[:34]!r}", (goal, url) == (want_goal, want_url), f"{goal!r} {url!r}")

    for text in ("just book me a flight", "tell me about version 3.11 updates", "hello"):
        goal, url = B.split_task_and_url(text)
        check(f"no URL found in {text[:28]!r}", url is None, str(url))

    # --- auth gate -----------------------------------------------------
    os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
    check("open when no allow-list is set", B.allowed(make_update("hi", user_id=999)))
    os.environ["TELEGRAM_ALLOWED_USER_ID"] = "42"
    check("allows the configured user", B.allowed(make_update("hi", user_id=42)))
    check("rejects anyone else", not B.allowed(make_update("hi", user_id=43)))

    upd, bot = make_update("do a thing https://x.com", user_id=43), FakeBot()
    await B.on_message(upd, make_ctx(None, bot))
    check("stranger is turned away, no run started", upd.message.replies == ["This bot is private."])
    check("and nothing was sent to the chat", not bot.texts and not bot.photos)
    os.environ["TELEGRAM_ALLOWED_USER_ID"] = "42"

    # --- missing url / missing task ------------------------------------
    upd, bot = make_update("book me a flight"), FakeBot()
    await B.on_message(upd, make_ctx(None, bot))
    check("asks for a URL when there is none", "I need a site" in upd.message.replies[0])
    check("no task was started", not B.state.busy)

    upd = make_update("https://www.saucedemo.com")
    await B.on_message(upd, make_ctx(None, FakeBot()))
    check("asks for a task when only a URL is sent", "no task" in upd.message.replies[0])

    # --- /start --------------------------------------------------------
    upd = make_update("/start")
    await B.cmd_start(upd, None)
    check("/start explains usage", "task and a URL" in upd.message.replies[0])
    check("/start mentions /stop", "/stop" in upd.message.replies[0])

    # --- step reporting: one image per step, text when there is none ----
    shot = Path("runs/_bot_test/shot.png")
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    bot = FakeBot()
    q: asyncio.Queue = asyncio.Queue()
    sender = asyncio.create_task(B.drain(bot, 7, q))
    q.put_nowait(step(1, shot=str(shot)))
    q.put_nowait(step(2, action="type", result="typed into [2]"))
    q.put_nowait(step(3, shot=str(shot)))
    q.put_nowait(None)
    await sender
    check("one photo per step that has one", len(bot.photos) == 2, str(len(bot.photos)))
    check("a step without a shot still reports", len(bot.texts) == 1, str(bot.texts))
    check("caption is one line with step, action and result",
          bot.photos[0][1] == "step 1: click(id=1) -> clicked [1] Login", bot.photos[0][1])
    check("steps arrive in order",
          [c.split(":")[0] for _, c in bot.photos] == ["step 1", "step 3"], str(bot.photos))

    # --- a huge result is truncated, not rejected -----------------------
    long_step = step(4, result="x" * 3000)
    check("caption fits Telegram's limit", len(B.caption_for(long_step)) <= B.CAPTION_LIMIT + 3)

    # --- ask_user over chat --------------------------------------------
    bot = FakeBot()
    ask = B.make_ask_user(bot, 7)
    fut = asyncio.create_task(ask("Which account should I use?"))
    await asyncio.sleep(0)
    check("the question reaches the chat", "Which account" in bot.texts[0], bot.texts[0][:40])
    check("the chat is told about the time limit", "5 minutes" in bot.texts[0])
    upd = make_update("use standard_user")
    await B.on_message(upd, make_ctx(None, bot))
    check("the reply is taken as the answer", await fut == "use standard_user")
    check("the user is acknowledged", "carrying on" in upd.message.replies[0])
    check("the pending question is cleared", B.state.pending is None)

    # --- ask_user timeout -> blocked -----------------------------------
    real_wait = asyncio.wait_for

    async def instant_timeout(aw, timeout):
        return await real_wait(aw, 0.05)

    asyncio.wait_for = instant_timeout
    try:
        bot = FakeBot()
        answer = await B.make_ask_user(bot, 7)("Who are you?")
    finally:
        asyncio.wait_for = real_wait
    check("timeout gives no answer", answer is None, str(answer))
    check("timeout is announced", any("stopping" in t for t in bot.texts), str(bot.texts))
    check("timeout is remembered", B.state.ask_timed_out is True)

    out = B.final_message(RunResult(status="needs_input", summary="Who are you?"), timed_out=True)
    check("an unanswered question ends as BLOCKED", out.startswith("BLOCKED"), out[:40])
    check("and says why", "nobody answered" in out, out)
    B.state.ask_timed_out = False

    # --- final messages for each outcome --------------------------------
    done = RunResult(status="done", summary="The cheapest item is $7.99.",
                     evidence="Sauce Labs Onesie $7.99", steps=[step(1)],
                     run_dir="runs/20261031-1200", verified=True)
    out = B.final_message(done, False)
    check("done is reported as DONE (verified)", out.startswith("DONE (verified)"), out[:30])
    check("the result is included", "cheapest item is $7.99" in out)
    check("the evidence is included", "Sauce Labs Onesie" in out)
    check("the trace folder is included", "runs/20261031-1200" in out)

    check("blocked is reported as BLOCKED",
          B.final_message(RunResult(status="blocked", summary="locked out"), False).startswith("BLOCKED"))
    check("an error is reported as FAILED",
          B.final_message(RunResult(status="error", summary="boom"), False).startswith("FAILED"))
    check("running out of steps is FAILED too",
          "ran out of steps" in B.final_message(RunResult(status="max_steps", summary="x"), False))

    # --- busy and /stop -------------------------------------------------
    bot = FakeBot()
    started = asyncio.Event()
    B.state.goal = "a long job"

    async def long_job():
        started.set()
        await asyncio.sleep(30)

    B.state.task = asyncio.create_task(long_job())
    await started.wait()
    check("state reports busy while a task runs", B.state.busy)

    upd = make_update("another task https://example.com")
    await B.on_message(upd, make_ctx(None, bot))
    check("a second task is refused with busy", "Busy with" in upd.message.replies[0], upd.message.replies[0][:40])
    check("the running task is named", "a long job" in upd.message.replies[0])

    upd = make_update("/stop")
    await B.cmd_stop(upd, None)
    check("/stop acknowledges", "Stopping" in upd.message.replies[0])
    await asyncio.sleep(0.05)
    check("/stop really cancels the run", B.state.task.cancelled())
    check("and the bot is free again", not B.state.busy)

    upd = make_update("/stop")
    await B.cmd_stop(upd, None)
    check("/stop with nothing running says so", "Nothing is running" in upd.message.replies[0])

    # --- a cancelled run tells the chat and names the trace -------------
    class FakeAgent:
        model, max_steps = "test-model", 25
        run_dir, steps = "runs/_bot_test", [step(1)]

        async def run(self, goal, url):
            await asyncio.sleep(30)

    bot = FakeBot()
    B.TaskAgent = lambda **kw: FakeAgent()          # type: ignore[assignment]
    try:
        task = asyncio.create_task(B.run_task(SimpleNamespace(bot=bot), 7, "g", "https://u"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        check("cancelling tells the chat", any("Stopped" in t for t in bot.texts), str(bot.texts[-1:]))
        check("and points at the trace", any("runs/_bot_test" in t for t in bot.texts))

        # --- a finished run sends the final message -------------------
        class DoneAgent(FakeAgent):
            async def run(self, goal, url):
                return done

        bot = FakeBot()
        B.TaskAgent = lambda **kw: DoneAgent()      # type: ignore[assignment]
        await B.run_task(SimpleNamespace(bot=bot), 7, "g", "https://u")
        check("the run announces itself first", "Working on it" in bot.texts[0], bot.texts[0][:30])
        check("the final message is sent last", bot.texts[-1].startswith("DONE"), bot.texts[-1][:30])
    finally:
        B.TaskAgent = TaskAgentReal

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


TaskAgentReal = B.TaskAgent

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
