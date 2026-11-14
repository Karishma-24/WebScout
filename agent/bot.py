"""Telegram front end.

Plugs into the agent's existing callbacks and changes nothing in the loop:
  - on_step   -> one message per step (screenshot + one-line caption)
  - ask_user  -> a question in the chat, answered by the user's next message

Run it with:  python -m agent.bot
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent.agent import RunResult, Step, TaskAgent

load_dotenv()

log = logging.getLogger("bot")

ASK_TIMEOUT = 300           # seconds to wait for an answer to ask_user
CAPTION_LIMIT = 1000        # Telegram allows 1024; leave room for the ellipsis

USAGE = (
    "I run web tasks in a real browser.\n\n"
    "Send me a task and a URL in one message, for example:\n"
    "  log in as standard_user / secret_sauce and tell me the cheapest item "
    "https://www.saucedemo.com\n\n"
    "I report every step with a screenshot, and finish with the result and the "
    "evidence I found for it. If I need something from you (a username, a "
    "choice) I will ask, and your next message is the answer.\n\n"
    "/stop  cancel the task I am running\n"
    "/start show this message"
)

# A bare domain counts as a URL; anything else means the task has no site.
URL_RE = re.compile(
    r"\b((?:https?://|www\.)[^\s]+|[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+(?:/[^\s]*)?)",
    re.I,
)
# Words that look like a domain but are almost certainly not one.
NOT_A_URL = re.compile(r"\.(?:com?|net|org|io|dev|ai|co|uk|de|fr|in|gov|edu)\b", re.I)


@dataclass
class Run:
    """Whatever is happening right now. One at a time, process-wide."""

    task: Optional[asyncio.Task] = None
    chat_id: Optional[int] = None
    goal: str = ""
    outbox: Optional[asyncio.Queue] = None
    pending: Optional[asyncio.Future] = None
    ask_timed_out: bool = False
    stopped: bool = False

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


state = Run()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def allowed(update: Update) -> bool:
    """Honour TELEGRAM_ALLOWED_USER_ID when it is set; otherwise allow anyone."""
    want = (os.getenv("TELEGRAM_ALLOWED_USER_ID") or "").strip()
    if not want:
        return True
    user = update.effective_user
    return bool(user) and str(user.id) == want


def split_task_and_url(text: str) -> tuple[str, Optional[str]]:
    """Pull the site out of the message; the rest is the goal."""
    for m in URL_RE.finditer(text):
        raw = m.group(1).rstrip(".,;:)!?")
        if "://" not in raw and not raw.lower().startswith("www.") and not NOT_A_URL.search(raw):
            continue
        url = raw if "://" in raw else "https://" + raw
        rest = text[: m.start()] + " " + text[m.end():]
        goal = re.sub(r"\s+", " ", rest).strip(" .,;:\n")
        return goal, url
    return text.strip(), None


def caption_for(step: Step) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in step.args.items())
    line = f"step {step.n}: {step.action}({args}) -> {step.result}"
    return line[:CAPTION_LIMIT] + ("..." if len(line) > CAPTION_LIMIT else "")


def final_message(result: RunResult, timed_out: bool) -> str:
    """done / blocked / failed, with the evidence and where the trace lives."""
    if result.status == "done":
        head = "DONE" + (" (verified)" if result.verified else "")
    elif result.status in ("blocked", "needs_input"):
        head = "BLOCKED"
    elif result.status == "max_steps":
        head = "FAILED (ran out of steps)"
    else:
        head = "FAILED"

    lines = [head, "", result.summary]
    if timed_out:
        lines.append("\nI stopped because nobody answered my question in time.")
    if result.evidence:
        lines.append(f"\nEvidence from the page:\n{result.evidence}")
    lines.append(f"\nSteps: {len(result.steps)}")
    lines.append(f"Trace: {result.run_dir}")
    return "\n".join(lines)[:4000]


# --------------------------------------------------------------------------
# the two agent callbacks
# --------------------------------------------------------------------------

async def drain(bot, chat_id: int, outbox: asyncio.Queue) -> None:
    """Send step messages in order, one image per step at most.

    on_step only drops the step in a queue, so a slow or failing Telegram call
    can never stall or crash the browser loop.
    """
    while True:
        step = await outbox.get()
        if step is None:
            return
        try:
            shot = step.screenshot
            if shot and Path(shot).exists():
                with open(shot, "rb") as fh:
                    await bot.send_photo(chat_id, photo=fh, caption=caption_for(step))
            else:
                await bot.send_message(chat_id, caption_for(step))
        except TelegramError as exc:
            log.warning("could not send step %s: %s", step.n, exc)
            try:
                await bot.send_message(chat_id, caption_for(step))
            except TelegramError:
                pass


def make_ask_user(bot, chat_id: int):
    """ask_user: post the question, wait for the next message from the user."""

    async def ask(question: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        state.pending = loop.create_future()
        try:
            await bot.send_message(
                chat_id,
                f"I need something from you:\n\n{question}\n\n"
                f"(reply here within {ASK_TIMEOUT // 60} minutes, or I will stop)",
            )
            return await asyncio.wait_for(state.pending, timeout=ASK_TIMEOUT)
        except asyncio.TimeoutError:
            state.ask_timed_out = True
            try:
                await bot.send_message(chat_id, "No answer, so I am stopping the task.")
            except TelegramError:
                pass
            return None
        except TelegramError as exc:
            log.warning("could not ask the user: %s", exc)
            return None
        finally:
            state.pending = None

    return ask


# --------------------------------------------------------------------------
# running a task
# --------------------------------------------------------------------------

async def run_task(app: Application, chat_id: int, goal: str, url: str) -> None:
    bot = app.bot
    state.outbox = asyncio.Queue()
    state.ask_timed_out = False
    state.stopped = False
    sender = asyncio.create_task(drain(bot, chat_id, state.outbox))

    try:
        agent = TaskAgent(
            # Sync and non-blocking: ordering is the sender's job, not the loop's.
            on_step=lambda step: state.outbox.put_nowait(step),
            ask_user=make_ask_user(bot, chat_id),
        )
    except RuntimeError as exc:           # no API key configured
        await bot.send_message(chat_id, f"I cannot start: {exc}")
        state.outbox.put_nowait(None)
        await sender
        return

    await bot.send_message(
        chat_id,
        f"Working on it.\n\nTask: {goal}\nSite: {url}\n"
        f"Model: {agent.model}, up to {agent.max_steps} steps.\n"
        f"Send /stop to cancel.",
    )

    try:
        result = await agent.run(goal, url)
        await bot.send_message(chat_id, final_message(result, state.ask_timed_out))
    except asyncio.CancelledError:
        with_steps = f" after {len(agent.steps)} steps" if agent.steps else ""
        try:
            await bot.send_message(
                chat_id, f"Stopped{with_steps}.\nTrace: {agent.run_dir}"
            )
        except TelegramError:
            pass
        raise
    except Exception as exc:              # a crash must still reach the user
        log.exception("run failed")
        await bot.send_message(chat_id, f"FAILED\n\nSomething went wrong: {exc}")
    finally:
        state.outbox.put_nowait(None)
        await sender
        state.outbox = None


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(USAGE)


async def cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if not state.busy:
        await update.message.reply_text("Nothing is running.")
        return
    state.stopped = True
    if state.pending and not state.pending.done():
        state.pending.cancel()
    state.task.cancel()
    await update.message.reply_text("Stopping...")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("This bot is private.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # An answer to a question I asked takes priority over everything else.
    if state.pending and not state.pending.done():
        state.pending.set_result(text)
        await update.message.reply_text("Thanks, carrying on.")
        return

    if state.busy:
        await update.message.reply_text(
            f'Busy with: "{state.goal}"\n\nSend /stop to cancel it first.'
        )
        return

    goal, url = split_task_and_url(text)
    if not url:
        await update.message.reply_text(
            "I need a site to work on. Send the task and the URL together, like:\n"
            "  find the cheapest item https://www.saucedemo.com"
        )
        return
    if not goal:
        await update.message.reply_text(
            f"That is a URL but no task. What should I do on {url}?"
        )
        return

    chat_id = update.effective_chat.id
    state.chat_id, state.goal = chat_id, goal
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    state.task = asyncio.create_task(run_task(ctx.application, chat_id, goal, url))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error", exc_info=ctx.error)


# --------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set; copy .env.example to .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    log.info("bot is up; press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
