"""Offline test of the Groq call layer: JSON mode, fallback parsing, retry,
rate-limit backoff and unsupported-parameter degradation.

The HTTP call is replaced by a fake, so nothing here touches the network.

    python tests/test_llm_calls.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GROQ_API_KEY", "offline-test-key")

from agent.agent import TaskAgent, _retry_after  # noqa: E402
from agent.browser import ActionError  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class FakeError(Exception):
    def __init__(self, status, message="error", headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


def reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class FakeCompletions:
    """Returns/raises a scripted sequence, recording the kwargs it was called with."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0) if self.script else reply('{"action":"scroll"}')
        if isinstance(item, Exception):
            raise item
        return item


def agent_with(script):
    a = TaskAgent(max_steps=5, run_dir=Path("runs/_llm_test"))
    fake = FakeCompletions(script)
    a.client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return a, fake


async def main() -> int:
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):          # keep the test instant
        slept.append(s)
        await real_sleep(0)

    asyncio.sleep = fake_sleep        # noqa: patched for the duration of the test
    try:
        # 1. defaults: Groq endpoint, model from .env, JSON mode, low effort
        a, fake = agent_with([reply('{"action":"click","id":1}')])
        out = await a._chat("sys", "hi")
        check("returns the parsed action", out == {"action": "click", "id": 1}, str(out))
        kw = fake.calls[0]
        check("JSON mode requested", kw.get("response_format") == {"type": "json_object"}, str(kw.get("response_format")))
        check("low reasoning effort sent", kw.get("reasoning_effort") == "low", str(kw.get("reasoning_effort")))
        check("no image is ever sent", isinstance(kw["messages"][1]["content"], str))
        check("model comes from env", a.model == os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"), a.model)

        # 2. fallback parser handles a fenced / chatty reply
        a, fake = agent_with([reply('Sure:\n```json\n{"action":"press","key":"Enter"}\n```')])
        out = await a._chat("sys", "hi")
        check("fenced JSON still parses", out == {"action": "press", "key": "Enter"}, str(out))
        check("no retry needed for that", len(fake.calls) == 1, f"{len(fake.calls)} calls")

        # 3. unparseable -> exactly one corrective retry, then success
        a, fake = agent_with([reply("I cannot do that."), reply('{"action":"blocked","reason":"x"}')])
        out = await a._chat("sys", "hi")
        check("one retry recovers bad output", out["action"] == "blocked", str(out))
        check("retry really was one extra call", len(fake.calls) == 2, f"{len(fake.calls)} calls")
        check(
            "retry shows the model its bad reply",
            any("not valid JSON" in m["content"] for m in fake.calls[1]["messages"]),
        )

        # 4. still unparseable after the retry -> a clear error, no third call
        a, fake = agent_with([reply("nope"), reply("still nope")])
        try:
            await a._chat("sys", "hi")
            check("gives up after one retry", False, "no error raised")
        except ActionError as exc:
            check("gives up after one retry", "after one retry" in str(exc), str(exc)[:60])
        check("exactly two calls were made", len(fake.calls) == 2, f"{len(fake.calls)} calls")

        # 5. 429 is retried with backoff, then succeeds
        slept.clear()
        a, fake = agent_with([
            FakeError(429, "rate limited"),
            FakeError(429, "rate limited"),
            reply('{"action":"wait","seconds":1}'),
        ])
        out = await a._chat("sys", "hi")
        check("429 is retried until it succeeds", out["action"] == "wait", str(out))
        check("backoff slept between tries", len(slept) == 2, str(slept))
        check("backoff grows", slept[1] > slept[0], str(slept))

        # 6. Retry-After header is honoured
        slept.clear()
        a, fake = agent_with([
            FakeError(429, "slow down", headers={"retry-after": "7"}),
            reply('{"action":"wait","seconds":1}'),
        ])
        await a._chat("sys", "hi")
        check("Retry-After header is honoured", 7 <= slept[0] < 7.5, str(slept))

        # 7. Groq's "try again in 1.5s" message is honoured too
        slept.clear()
        a, fake = agent_with([FakeError(429, "Please try again in 1.5s."), reply('{"action":"wait"}')])
        await a._chat("sys", "hi")
        check("retry delay parsed from the message", 1.5 <= slept[0] < 2.0, str(slept))

        # 8. rate limiting that never clears -> raised, not looped forever
        slept.clear()
        a, fake = agent_with([FakeError(429) for _ in range(8)])
        try:
            await a._chat("sys", "hi")
            check("persistent 429 eventually raises", False, "no error")
        except FakeError:
            check("persistent 429 eventually raises", True)
        check("retries are capped", len(fake.calls) == 5, f"{len(fake.calls)} calls")
        check("backoff is capped at 30s", all(s <= 31 for s in slept), str(slept))

        # 9. a 400 that is not about our optional params is not retried
        a, fake = agent_with([FakeError(400, "context length exceeded")])
        try:
            await a._chat("sys", "hi")
            check("hard 400 is raised immediately", False, "no error")
        except FakeError:
            check("hard 400 is raised immediately", len(fake.calls) == 1, f"{len(fake.calls)} calls")

        # 10. a model that rejects JSON mode -> drop it and carry on
        a, fake = agent_with([
            FakeError(400, "response_format is not supported for this model"),
            reply('{"action":"click","id":2}'),
        ])
        out = await a._chat("sys", "hi")
        check("JSON mode is dropped when unsupported", out["action"] == "click", str(out))
        check("and not requested again", "response_format" not in fake.calls[1], str(fake.calls[1].keys()))
        check("the flag stays off", a._json_mode is False)

        # 10b. Groq rejecting its own generation as invalid JSON is survivable
        a, fake = agent_with([
            FakeError(400, "Failed to validate JSON. Please adjust your prompt."),
            reply('Here you go: {"action":"click","id":9}'),
        ])
        out = await a._chat("sys", "hi")
        check("a json_validate_failed 400 does not kill the run", out["action"] == "click", str(out))
        check("JSON mode is dropped after it fails validation", a._json_mode is False)
        check("the retry drops response_format", "response_format" not in fake.calls[1])

        # 11. same for reasoning_effort
        a, fake = agent_with([
            FakeError(400, "reasoning_effort is not supported"),
            reply('{"action":"click","id":3}'),
        ])
        out = await a._chat("sys", "hi")
        check("reasoning effort dropped when unsupported", out["action"] == "click", str(out))
        check("and not requested again", "reasoning_effort" not in fake.calls[1])

        # 12. a 500 is retried as well
        a, fake = agent_with([FakeError(503, "upstream"), reply('{"action":"scroll"}')])
        out = await a._chat("sys", "hi")
        check("transient 5xx is retried", out["action"] == "scroll", str(out))

        # 13. the verifier is shown the working memory, clearly marked as claims
        a, fake = agent_with([reply('{"verdict":"accept","why":"the page shows the order"}')])
        a._notes = ["cheapest item: Onesie at $7.99", "order placed"]
        ok, why, _ = await a._verify("buy the cheapest item", "bought the Onesie for $7.99",
                                     "THANK YOU FOR YOUR ORDER",
                                     SimpleNamespace(to_prompt=lambda: "CHECKOUT COMPLETE"))
        sent = fake.calls[0]["messages"][1]["content"]
        check("the verifier accepts a corroborated claim", ok is True, str((ok, why)))
        check("the verifier is given the agent's notes", "Onesie at $7.99" in sent)
        check("the notes are marked as claims, not proof", "not proof" in sent)
        check("the final page state is still sent", "CHECKOUT COMPLETE" in sent)

        # a verifier that rejects is still obeyed
        a, fake = agent_with([reply('{"verdict":"reject","why":"no order shown","hint":"finish it"}')])
        ok, why, hint = await a._verify("g", "r", "e", SimpleNamespace(to_prompt=lambda: "x"))
        check("a rejection is passed through", ok is False and hint == "finish it", str((ok, why, hint)))

        # 14. verifier failure must never become a false success
        a, fake = agent_with([FakeError(400, "context length exceeded")])
        ok, why, hint = await a._verify("goal", "result", "evidence", SimpleNamespace(to_prompt=lambda: "page"))
        check("a broken verifier rejects rather than accepts", ok is False, str((ok, why)))
        check("missing evidence is rejected without a call", (await a._verify("g", "r", "", None))[0] is False)
    finally:
        asyncio.sleep = real_sleep

    check("_retry_after reads plain seconds", _retry_after(FakeError(429, "x", {"retry-after": "3"})) == 3.0)
    check("_retry_after returns None when absent", _retry_after(FakeError(429, "x")) is None)

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
