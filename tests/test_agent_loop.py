"""Offline test of the agent loop: no API calls, no network.

The LLM is replaced by a scripted list of actions, so the recovery behaviour
(no-change detection, repeated actions, ActionError feedback, MAX_STEPS, the
verifier's veto) can be checked deterministically. The browser is real but
drives a local data: page, so nothing here depends on a website.

    python tests/test_agent_loop.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OPENROUTER_API_KEY", "offline-test-key")

from agent.agent import MAX_NOTES, NOTE_CHARS, WAIT_STREAK_LIMIT, TaskAgent  # noqa: E402
from agent.browser import BrowserDriver, new_run_dir  # noqa: E402

PAGE = (
    "data:text/html,<title>Form</title><h1>Test form</h1>"
    "<input id=a placeholder='Name'><button onclick=\""
    "document.getElementById('out').textContent='Saved as '+document.getElementById('a').value"
    "\">Save</button><p id=out></p><p>Ready.</p>"
)

# A page whose DOM keeps changing on its own: waiting on it is reasonable.
CHANGING_PAGE = (
    "data:text/html,<title>Busy</title><h1>Working</h1><p id=out>Ready.</p>"
    "<script>let n=0;setInterval(()=>{document.getElementById('out')"
    ".textContent='Ready. tick '+(n++)},300)</script>"
)
# A page showing a spinner, so the busy flag should fire.
BUSY_PAGE = (
    "data:text/html,<title>Load</title><h1>Please hold</h1>"
    "<div class='loading-spinner' style='width:40px;height:40px'>loading</div>"
    "<p>Ready.</p>"
)
# Content that only appears ~700ms after the click: it must still be captured
# by the very next observe(), which is what the DOM-quiet wait buys us.
LATE_PAGE = (
    "data:text/html,<title>Late</title><h1>Late loader</h1>"
    "<button onclick=\"setTimeout(()=>{document.getElementById('o')"
    ".innerHTML='Loaded!<button>Continue</button>'},700)\">Start</button>"
    "<div id=o></div>"
)

# A page whose DOM keeps changing on its own: waiting on it is reasonable.
CHANGING_PAGE = (
    "data:text/html,<title>Busy</title><h1>Working</h1><p id=out>Ready.</p>"
    "<script>let n=0;setInterval(()=>{document.getElementById('out')"
    ".textContent='Ready. tick '+(n++)},300)</script>"
)
# A page showing a spinner, so the busy flag should fire.
BUSY_PAGE = (
    "data:text/html,<title>Load</title><h1>Please hold</h1>"
    "<div class='loading-spinner' style='width:40px;height:40px'>loading</div>"
    "<p>Ready.</p>"
)
# Content that only appears ~700ms after the click. It must still be captured
# by the very next observe(), which is what waiting for DOM quiet buys us.
LATE_PAGE = (
    "data:text/html,<title>Late</title><h1>Late loader</h1>"
    "<button onclick=\"setTimeout(()=>{document.getElementById('o')"
    ".innerHTML='Loaded!<button>Continue</button>'},700)\">Start</button>"
    "<div id=o></div>"
)

# Hidden containers whose text must never reach the model.
HIDDEN_PAGE = (
    "data:text/html,<title>Hidden</title><p>visible text</p>"
    "<div style='display:none'><button>GhostButton</button><p>ghost text</p></div>"
    "<div style='visibility:hidden'>invisible text</div>"
)

failures: list[str] = []


def find_button(obs, name="Start"):
    for e in obs.elements:
        if name.lower() in e.name.lower():
            return e.id
    raise AssertionError(f"no {name!r} button in {[e.name for e in obs.elements]}")


def find_button(obs, name="Start"):
    for e in obs.elements:
        if name.lower() in e.name.lower():
            return e.id
    raise AssertionError(f"no {name!r} button in {[e.name for e in obs.elements]}")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


class ScriptedAgent(TaskAgent):
    """TaskAgent with the two LLM calls replaced by a script."""

    def __init__(self, script, verdicts=None, **kw):
        super().__init__(**kw)
        self.script = list(script)
        self.verdicts = list(verdicts or [])
        self.seen_notes: list[str] = []
        self.seen_memory: list[list[str]] = []
        self.seen_busy: list[bool] = []

    async def _decide(self, goal, start_url, step, obs, hints):
        self.seen_notes.append(" ".join(hints))
        self.seen_memory.append(list(self._notes))
        self.seen_busy.append(obs.busy)
        return self.script.pop(0) if self.script else {
            "action": "scroll", "direction": "down", "thought": "filler",
        }

    async def _verify(self, goal, result, evidence, obs):
        if self.verdicts:
            return self.verdicts.pop(0)
        return True, "looks right", ""


async def run(script, verdicts=None, max_steps=10, page=None):
    drv = BrowserDriver(headless=True, run_dir=new_run_dir())
    await drv.start()
    try:
        a = ScriptedAgent(script, verdicts, driver=drv, max_steps=max_steps, run_dir=drv.run_dir)
        return a, await a.run("fill in the form", page or PAGE)
    finally:
        await drv.close()


async def main() -> int:
    # 1. happy path: type, click, done -> accepted by the verifier
    a, r = await run([
        {"action": "type", "id": 1, "text": "Ada", "thought": "fill the name"},
        {"action": "click", "id": 2, "thought": "save it"},
        {"action": "done", "result": "Saved as Ada", "evidence": "Saved as Ada", "thought": "it shows"},
    ])
    check("happy path reports done", r.status == "done", r.status)
    check("evidence carried through", r.evidence == "Saved as Ada", r.evidence)
    check("verified flag set", r.verified is True)
    check("typing really happened", any("Ada" in s.result for s in r.steps))
    check("steps recorded", len(r.steps) == 3, str(len(r.steps)))

    # 2. trace.json written with every step
    trace = json.loads((Path(r.run_dir) / "trace.json").read_text(encoding="utf-8"))
    check("trace.json written", trace["status"] == "done")
    check("trace has all steps", len(trace["steps"]) == 3, str(len(trace["steps"])))
    check(
        "trace steps carry action, result and screenshot",
        all(s["action"] and s["result"] and s["screenshot"] for s in trace["steps"]),
    )
    check("screenshots exist on disk", all(Path(s).exists() for s in r.screenshots))

    # 3. verifier veto: a claimed done that is not proven must not be accepted
    a, r = await run(
        [
            {"action": "done", "result": "all finished", "evidence": "trust me", "thought": "claim"},
            {"action": "type", "id": 1, "text": "Ada", "thought": "actually do it"},
            {"action": "click", "id": 2, "thought": "save"},
            {"action": "done", "result": "Saved as Ada", "evidence": "Saved as Ada", "thought": "now real"},
        ],
        verdicts=[(False, "the page shows nothing was saved", "fill the form first")],
    )
    check("rejected done does not end the run", r.status == "done" and len(r.steps) == 4, r.status)
    check(
        "rejection is fed back to the model",
        any("REJECTED BY THE VERIFIER" in n for n in a.seen_notes),
    )

    # 4. no-change detection after an action that does nothing
    a, r = await run([
        {"action": "scroll", "direction": "down", "thought": "nothing to scroll"},
        {"action": "scroll", "direction": "down", "thought": "again"},
        {"action": "done", "result": "x", "evidence": "Ready.", "thought": "stop"},
    ])
    check(
        "no-change is detected and told to the model",
        any("DID NOT CHANGE THE PAGE" in n for n in a.seen_notes),
    )
    check("changed flag recorded on the step", r.steps[0].changed is False, str(r.steps[0].changed))

    # 5. repeated identical action is stopped
    same = {"action": "click", "id": 2, "thought": "same every time"}
    a, r = await run([dict(same) for _ in range(6)])
    check("repeated action is stopped", r.status == "blocked", r.status)
    check("blocked explains the loop", "same thing" in r.summary, r.summary[:60])

    # 6. ActionError is caught and fed back, the run continues
    a, r = await run([
        {"action": "click", "id": 999, "thought": "stale id"},
        {"action": "done", "result": "ok", "evidence": "Ready.", "thought": "carry on"},
    ])
    check("bad element id does not crash the run", r.status == "done", r.status)
    check("the failure is recorded as an error", r.steps[0].error is not None, str(r.steps[0].error)[:60])
    check("failure text reaches the model", "FAILED" in r.steps[0].result, r.steps[0].result[:50])

    # 7. unknown action name is reported back, not fatal
    a, r = await run([
        {"action": "teleport", "thought": "not a real action"},
        {"action": "done", "result": "ok", "evidence": "Ready.", "thought": "carry on"},
    ])
    check("unknown action is handled", r.status == "done" and r.steps[0].error is not None)

    # 8. MAX_STEPS is enforced
    a, r = await run([{"action": "scroll", "direction": "up", "thought": "idle"}], max_steps=3)
    check("max steps enforced", r.status == "max_steps", r.status)
    check("max steps count respected", len(r.steps) == 3, str(len(r.steps)))

    # 9. blocked is passed straight through
    a, r = await run([
        {"action": "blocked", "reason": "The account is locked out.", "thought": "banner"},
    ])
    check("blocked is reported honestly", r.status == "blocked" and "locked out" in r.summary)

    # 10. ask_user with nobody to ask -> needs_input, never invented
    a, r = await run([
        {"action": "ask_user", "question": "Which account should I use?", "thought": "no creds"},
    ])
    check("ask_user without a responder stops the run", r.status == "needs_input", r.status)
    check("the question is surfaced", "Which account" in r.summary, r.summary)

    # 11. ask_user with a responder continues the run
    drv = BrowserDriver(headless=True, run_dir=new_run_dir())
    await drv.start()
    try:
        answers = []

        async def answer(q):
            answers.append(q)
            return "use Ada"

        a = ScriptedAgent(
            [
                {"action": "ask_user", "question": "What name?", "thought": "need it"},
                {"action": "type", "id": 1, "text": "Ada", "thought": "use the answer"},
                {"action": "click", "id": 2, "thought": "save"},
                {"action": "done", "result": "Saved as Ada", "evidence": "Saved as Ada", "thought": "done"},
            ],
            driver=drv, ask_user=answer, max_steps=10, run_dir=drv.run_dir,
        )
        r = await a.run("fill in the form", PAGE)
    finally:
        await drv.close()
    check("ask_user answer resumes the run", r.status == "done", r.status)
    check("the answer is handed to the model", any("use Ada" in n for n in a.seen_notes))

    # --- A. working memory ---------------------------------------------
    a, r = await run([
        {"action": "type", "id": 1, "text": "Ada", "thought": "fill",
         "notes": ["chosen name: Ada", "price seen: $7.99"]},
        {"action": "click", "id": 2, "thought": "save", "notes": ["clicked save"]},
        {"action": "done", "result": "Saved as Ada for $7.99", "evidence": "Saved as Ada",
         "thought": "use the notes"},
    ])
    check("notes do not break the run", r.status == "done", r.status)
    check("the first step starts with no notes", a.seen_memory[0] == [], str(a.seen_memory[0]))
    check("notes are carried into the next step's prompt",
          a.seen_memory[1] == ["chosen name: Ada", "price seen: $7.99"], str(a.seen_memory[1]))
    check("notes accumulate rather than replace",
          a.seen_memory[2] == ["chosen name: Ada", "price seen: $7.99", "clicked save"],
          str(a.seen_memory[2]))

    a, r = await run([
        {"action": "scroll", "thought": "x", "notes": "single fact"},
        {"action": "scroll", "direction": "up", "thought": "y", "notes": ["single fact", "second"]},
        {"action": "done", "result": "ok", "evidence": "Ready.", "thought": "z"},
    ])
    check("a bare string note is accepted", a.seen_memory[1] == ["single fact"], str(a.seen_memory[1]))
    check("duplicate notes are not stored twice",
          a.seen_memory[2] == ["single fact", "second"], str(a.seen_memory[2]))

    a, r = await run([
        {"action": "click", "id": 2, "thought": "t", "notes": [f"note {i}"]} for i in range(6)
    ])
    check("changing notes do not mask a repeated action", r.status == "blocked", r.status)

    # The cap is a property of the memory itself, so test it directly: a long
    # run would be stopped by the repeat detector long before 40 steps.
    mem = ScriptedAgent([], driver=None, max_steps=5, run_dir=Path("runs/_notes_test"))
    for i in range(40):
        mem._remember([f"fact {i}"])
    check("the working memory is capped", len(mem._notes) == MAX_NOTES, str(len(mem._notes)))
    check("the cap keeps the most recent notes", mem._notes[-1] == "fact 39", mem._notes[-1])
    check("the cap drops the oldest", "fact 0" not in mem._notes)
    long_note = "x" * 500
    mem._remember([long_note])
    check("an over-long note is trimmed", len(mem._notes[-1]) <= NOTE_CHARS, str(len(mem._notes[-1])))
    mem._remember(None)
    mem._remember([])
    check("empty notes are ignored", mem._notes[-1][:1] == "x", mem._notes[-1][:8])

    # --- B. waiting for pages -------------------------------------------
    a, r = await run(
        [{"action": "wait", "seconds": 1, "thought": "still loading"} for _ in range(4)]
        + [{"action": "done", "result": "ok", "evidence": "Ready.", "thought": "done"}],
        page=CHANGING_PAGE,
    )
    check("repeated waits on a changing page are allowed", r.status == "done", r.status)
    check("every wait actually ran",
          len([s for s in r.steps if s.action == "wait"]) == 4,
          str([s.action for s in r.steps]))

    a, r = await run(
        [{"action": "wait", "seconds": 1, "thought": "hoping"} for _ in range(12)],
        page=CHANGING_PAGE, max_steps=12,
    )
    check("an endless wait streak is still stopped", r.status == "blocked", r.status)
    check("and explains the content never arrived",
          "never finished loading" in r.summary, r.summary[:70])
    check("the wait streak limit is respected",
          len([s for s in r.steps if s.action == "wait"]) == WAIT_STREAK_LIMIT,
          str(len([s for s in r.steps if s.action == "wait"])))

    a, r = await run([{"action": "wait", "seconds": 1, "thought": "pointless"} for _ in range(6)])
    check("waiting on a static page is treated as a loop", r.status == "blocked", r.status)
    check("and is reported as repetition", "same thing" in r.summary, r.summary[:60])

    a, r = await run(
        [{"action": "wait", "seconds": 1, "thought": "spinner showing"},
         {"action": "done", "result": "ok", "evidence": "Ready.", "thought": "done"}],
        page=BUSY_PAGE,
    )
    check("a loading indicator is detected", a.seen_busy[0] is True, str(a.seen_busy))

    # Delayed content must land in the NEXT observation, not a later one.
    drv = BrowserDriver(headless=True, run_dir=new_run_dir())
    await drv.start()
    try:
        await drv.goto(LATE_PAGE)
        obs = await drv.observe()
        before = "Loaded!" in obs.text
        await drv.click(find_button(obs))
        after = await drv.observe()
    finally:
        await drv.close()
    # Text inside a hidden container must not leak into the page view: it
    # describes a page the user cannot see, and sends the agent hunting for
    # controls that are not there.
    drv = BrowserDriver(headless=True, run_dir=new_run_dir())
    await drv.start()
    try:
        await drv.goto(HIDDEN_PAGE)
        hidden_obs = await drv.observe()
    finally:
        await drv.close()
    check("visible text is read", "visible text" in hidden_obs.text, hidden_obs.text)
    check("display:none text does not leak", "ghost" not in hidden_obs.text.lower(), hidden_obs.text)
    check("visibility:hidden text does not leak",
          "invisible" not in hidden_obs.text.lower(), hidden_obs.text)
    check("a hidden control is not offered as an element",
          not any("Ghost" in e.name for e in hidden_obs.elements),
          str([e.name for e in hidden_obs.elements]))

    check("late content is absent before the click", not before)
    check("late content is captured right after the click",
          "Loaded!" in after.text, after.text[:120])
    check("and the control it added is in the element list",
          any("Continue" in e.name for e in after.elements),
          str([e.name for e in after.elements]))

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
