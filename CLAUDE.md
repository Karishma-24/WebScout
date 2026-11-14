# Web Task Agent

Autonomous browser agent driven from Telegram. Goal in plain English + site -> agent launches its own headless Playwright browser, runs a perceive -> plan -> act -> verify loop, reports back with screenshots.

## Rules
- NEVER hardcode selectors or step sequences for any specific site. Must generalise to unseen sites.
- Keep layers separate: `agent/browser.py` (Playwright only, no LLM), `agent/agent.py` (loop + LLM), `agent/bot.py` (Telegram).
- Actions the LLM can emit (JSON, one per step): click(id), type(id,text), select(id,value), press(key), goto(url), wait(s), scroll, back, ask_user(question), done(result, evidence), blocked(reason).
- `done` requires evidence from the final page state; verify it, never assume success.
- Recovery: detect no-change after action, repeated actions, popups/cookie banners; hard cap MAX_STEPS.
- Locked accounts / CAPTCHA / missing login -> blocked or ask_user, never retry forever or invent results.
- Secrets only in .env (never commit, never in zip). Keep .env.example updated.
- Save per-run trace + screenshots in runs/<timestamp>/.

## Model
- Groq via the OpenAI client: `base_url=https://api.groq.com/openai/v1`, `GROQ_API_KEY` and `GROQ_MODEL` from .env.
- `openai/gpt-oss-120b` is TEXT-ONLY. Never send images to the model. Screenshots are captured and saved for the run report and the Telegram reply only; the model sees `Observation.to_prompt()` (URL, title, numbered elements, page text).
- Calls use JSON mode (`response_format={"type":"json_object"}`) plus a lenient parser for fenced/chatty replies, and one corrective retry before failing.
- `reasoning_effort=low` (override with REASONING_EFFORT).
- Retry 429s and transient 5xx with exponential backoff, honouring Retry-After; cap the delay. Unsupported optional params (JSON mode, reasoning effort) are dropped once and the call retried.

## Layout
agent/browser.py, agent/agent.py, agent/bot.py, agent/prompts.py, run_cli.py (CLI entry), tasks.json (test tasks only), runs/

## Run
python run_cli.py "task" https://site
python -m agent.bot
