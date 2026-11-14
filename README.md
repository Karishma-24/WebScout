# Web Task Agent

Give it a goal in plain English and a site. It launches its own headless
Chromium, works out what the page is, acts one step at a time, checks its own
claim of success against the final page, and reports back with screenshots —
from the command line or from Telegram.

It has no idea what any particular website is. There are no hardcoded
selectors and no scripted step sequences anywhere in `agent/`; the only file
that names a site is `tasks.json`, which is the test list.

```
you: log in as standard_user / secret_sauce and tell me the cheapest item
     https://www.saucedemo.com

step 1: type(id=1, text='standard_user')  -> typed "standard_user" into [1] Username
step 2: type(id=2, text='secret_sauce')   -> typed "secret_sauce" into [2] Password
step 3: click(id=3)                       -> clicked [3] login-button
step 4: select(id=3, value='Price (low to high)') -> selected ...
step 5: done                              -> Sauce Labs Onesie at $7.99

STATUS:   DONE (verified)
EVIDENCE: Sauce Labs Onesie ... $7.99
```

## Setup

Python 3.10 or newer.

```bash
python -m venv agent1
agent1\Scripts\activate          # Windows
# source agent1/bin/activate     # macOS / Linux

pip install -r requirements.txt
playwright install chromium
```

Then copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

| Variable | What it is |
| --- | --- |
| `GROQ_API_KEY` | Groq API key. Required. |
| `GROQ_MODEL` | Defaults to `openai/gpt-oss-120b`. |
| `TELEGRAM_BOT_TOKEN` | From BotFather. Only needed for the bot. |
| `TELEGRAM_ALLOWED_USER_ID` | Optional. If set, the bot ignores everyone else. |
| `MAX_STEPS` | Hard cap on steps per run. Default 25. |
| `HEADLESS` | `false` to watch the browser work. Default `true`. |

`.env` is gitignored and is never included in the submission zip.

## Running it

**Command line**

```bash
python run_cli.py "find the cheapest item and tell me its price" https://www.saucedemo.com
python run_cli.py "..." https://site --show --max-steps 15
```

`--show` opens a visible browser window. Every run writes
`runs/<timestamp>/` with a screenshot per step and a `trace.json`.

**Telegram**

```bash
python -m agent.bot
```

Then message the bot. Any plain message is a task plus a URL in one line. It
replies with a screenshot per step, asks you a question if it needs something
it was not given, and finishes with the status, the result, the evidence and
the trace path. `/stop` cancels the current run; it runs one task at a time.

**The target tasks**

```bash
python run_targets.py              # all of tasks.json, with a pause between runs
python run_targets.py --list       # show them, run nothing
python run_targets.py --only the-internet-login
python run_targets.py --pause 30   # longer gap if you hit rate limits
```

Writes `reports/<task-id>/` (trace plus screenshots) and `reports/SUMMARY.md`,
a table of task, status, steps, whether it matched expectation, and the result.
Any run that claimed *done* where *blocked* was expected is flagged loudly as a
**FALSE SUCCESS** — that is the failure that matters most here.

**Tests** — all offline. No network, no API spend.

```bash
python tests/test_browser_saucedemo.py   # the driver against a real site
python tests/test_agent_loop.py          # the loop, with a scripted model
python tests/test_llm_calls.py           # JSON mode, retries, backoff
python tests/test_bot.py                 # the Telegram layer, with a fake bot
python tests/test_make_zip.py            # the secret scanner
```

Only the first opens a browser and touches a live site.

**Packaging and slides**

```bash
python make_zip.py <your-id>     # -> <your-id>_challenge-6.zip
python slides/make_deck.py       # -> slides/web-task-agent.pptx
```

## Architecture

Three layers that do not reach into each other.

```
agent/browser.py   Playwright only. No LLM, no prompts, no goal.
       |           observe() -> numbered elements + page text + screenshot
       |           click / type / select / press / goto / wait / scroll / back
       v
agent/agent.py     The loop. Owns the model calls and the recovery rules.
       |           observe -> plan -> act -> verify, and the trace.
       v
agent/bot.py       Telegram. Plugs into the agent's on_step and ask_user
                   callbacks and changes nothing in the loop.
agent/prompts.py   All prompt text, so the loop stays readable.
```

`run_cli.py` and `run_targets.py` are the other two front ends, using the same
`TaskAgent` with different `on_step` and `ask_user` callbacks. The CLI asks on
stdin; the bot asks in the chat; the batch runner never asks.

### The loop

Each step: `observe()` the page, send that view plus the goal and the last four
steps to the model, get back exactly one JSON action, execute it, repeat.

**Recovery**, all of it site-agnostic:

- **Nothing happened.** Every observation has a fingerprint (URL + elements +
  text). If it is unchanged after an action, the model is told its action had
  no effect and not to repeat it. Four stalled steps ends the run.
- **Going in circles.** The same action three times in a row ends the run as
  blocked rather than burning the step budget.
- **Overlays.** A cheap keyword pre-check warns the model about cookie and
  consent banners before they swallow its clicks.
- **Action failures.** A stale element id or a failed click comes back as
  `FAILED: ...` text for the model to react to, instead of crashing the run.
- **MAX_STEPS.** A hard cap, always.

**Verification.** A `done` is never taken on trust. A second model call gets
the goal, the claim, the quoted evidence and the real final page, and decides
whether the page actually proves it. If not, the rejection and a hint go back
to the model and the run continues. A verifier that errors returns *reject*, so
a broken check can never become a false success.

**Honest stopping** is in the system prompt and is treated as overriding task
completion: a locked account, a CAPTCHA, a paywall or a missing credential ends
in `blocked` or `ask_user`. The agent is told in as many words that a truthful
`blocked` is a success and a fabricated `done` is a total failure.

### Perception: numbered DOM elements, and a text-only model

The model is `openai/gpt-oss-120b` on Groq, which **cannot see images**. That
shaped the design, and it turned out to be a reasonable trade rather than a
pure loss.

Instead of pixels, each step builds a compact text view of the page:

```
URL: https://www.saucedemo.com/inventory.html
TITLE: Swag Labs
SCROLL: y=0 of 1110 (viewport 900)

INTERACTIVE ELEMENTS:
  [1] button "Open Menu" type=button
  [2] button "Cart, empty"
  [3] select "Sort products" value="az" options=[Name (A to Z), ...]
  [5] button "Add to cart" in "Sauce Labs Backpack"
  [7] button "Add to cart" in "Sauce Labs Bike Light"
```

- Elements are found by role and tag, not by site knowledge, and tagged in the
  DOM with `data-wta-id`. The model picks a **number**; it never writes a
  selector, so it cannot invent one that does not exist.
- The accessible name is resolved the way a screen reader would:
  `aria-label` → `aria-labelledby` → `<label>` → text → attributes.
- Repeated names are the hard case without vision. Six buttons all called "Add
  to cart" are only tellable apart by what they belong to, so each element also
  carries context from its nearest heading, shown **only** when a name repeats.
- All frames are read, so iframe content and frameset pages are not invisible.
- Transparent form controls are included: sites routinely layer a
  near-zero-opacity native `<select>` under a styled widget, and skipping those
  would make such dropdowns unusable.

Screenshots are still captured every step, with numbered boxes drawn over each
element. They are for **you** — the trace, the report and the Telegram
messages — and are never sent to the model.

Why this is a fair trade: a text view is cheap, fast and deterministic, it
survives a model with no vision, and the numbered handles remove a whole class
of failure where a model hallucinates a CSS selector. What it costs is anything
that is genuinely only visible — canvas, images, "click the red button",
layout-dependent judgement. Those are in [LIMITS.md](LIMITS.md).

### Model calls

Groq through the OpenAI client. Calls use JSON mode, with a lenient parser for
fenced or chatty replies and one corrective retry before giving up.
`reasoning_effort` is `low` — these are short, mechanical decisions. 429s and
transient 5xx are retried with exponential backoff that honours `Retry-After`,
capped, and bounded in attempts. If the model rejects an optional parameter,
it is dropped once and the call retried rather than failing the run.

## Layout

```
agent/browser.py      Playwright driver: observe() and the actions
agent/agent.py        the loop, recovery, verification, the trace
agent/prompts.py      all prompt text
agent/bot.py          Telegram front end
run_cli.py            command line front end
run_targets.py        batch runner over tasks.json -> reports/
tasks.json            the target tasks (the only file that names sites)
make_zip.py           submission zip, refuses to ship anything key-shaped
slides/make_deck.py   builds the deck from reports/SUMMARY.md
tests/                offline test suites
runs/                 per-run traces and screenshots (gitignored)
reports/              per-task results from run_targets.py
```
