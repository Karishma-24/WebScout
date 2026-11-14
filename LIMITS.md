# Limits

An honest account of what this agent does well, where it breaks, and what I
would fix next. Written from what I actually observed building and running it,
not from what the design hopes.

## Where it is reliable

- **Ordinary forms and flows.** Login, search, dropdowns, carts, multi-page
  checkouts. Anything driven by real HTML controls with readable labels.
- **Sites it has never seen.** The element list is built from roles and
  accessible names, so a new site is not a special case. It handled Wikipedia
  and Hacker News with no changes.
- **Stopping honestly.** A locked account, a wrong password or an empty result
  comes back as `blocked` with the banner quoted, not as an invented success.
  This is the behaviour I was most careful about and tested most directly.
- **Not claiming success it cannot show.** Every `done` is checked by a second
  model call against the real final page. Unproven claims are rejected and the
  run continues.
- **Recovering from its own mistakes.** Stale element ids, failed clicks and
  actions that changed nothing are fed back as text and worked around rather
  than crashing the run.
- **Repeated controls.** Six buttons named "Add to cart" are disambiguated by
  the product each belongs to. Verified by having it add two specific items out
  of six and getting both right.

## Where it is not

**No vision.** The model is `openai/gpt-oss-120b`, which cannot see images.
Screenshots are saved for the report but never sent. So:

- Anything rendered in `<canvas>`, in an image, or in WebGL is invisible to it.
  Charts, maps, drawing tools and image-based puzzles are out of reach.
- Instructions that depend on appearance — "click the red button", "pick the
  one at the top right", "the item that looks discounted" — cannot be followed
  except by accident.
- Where a layout conveys meaning that the DOM order does not, it will guess.
  The HN check showed this: context for a repeated link resolved to the row's
  username rather than the story title. Still disambiguating, but not the label
  a person would use.

**CAPTCHAs and bot checks.** It stops and reports `blocked`, by design. It
makes no attempt to solve or evade them, and I would not add one.

**Heavy iframe and shadow DOM pages.** Same-origin iframes are read and acted
in, and that is tested end to end against a TinyMCE editor inside an iframe.
But cross-origin frames cannot be inspected at all, and **closed shadow roots
are invisible** — a site built on those will look mostly empty to the agent.
Frames beyond the first eight are ignored.

**Rate limits.** A long run is many sequential model calls. 429s are retried
with backoff that honours `Retry-After`, but a tight quota turns into slow runs
and eventually a failed one. `run_targets.py` pauses between tasks for this
reason. This is the most likely cause of a flaky batch run.

**Long multi-step forms.** `MAX_STEPS` defaults to 25. A checkout with several
pages and many fields can run out of budget partway, which is reported as
`FAILED (ran out of steps)` rather than something misleading — but it is still
a failure. Each field costs a step, so the cap is the real constraint on how
long a flow can be.

**Page text truncation.** The text view is capped at 2500 characters and the
element list at 150 entries. On a very long page, a fact that sits below the
cut is invisible until the agent scrolls, and it may conclude too early that
something is not there.

**Non-determinism.** It is a language model choosing one action at a time.
The same task can take a different route on different runs, and a task that
passes once is not guaranteed to pass every time. The traces in `reports/`
are the record of specific runs, not a promise.

**Single session, no auth reuse.** Every run starts from a cold browser with
no cookies or saved logins. Anything requiring an existing session, or 2FA,
ends in `ask_user` or `blocked`.

## What I would harden next

In the order I would actually do it:

1. **A vision model as an optional second opinion.** Keep the text view as the
   primary channel — it is cheap and deterministic — and send the already-drawn
   numbered screenshot only when the text view is ambiguous or an action has
   stalled. The screenshots and the numbering exist already, so this is mostly
   a routing decision.
2. **Better context for repeated controls.** Prefer a row's heading or first
   cell over whatever `querySelector` happens to find first, which would fix
   the Hacker News case.
3. **A step budget that reflects the work.** Let a long form earn more steps
   instead of a flat cap, while keeping a hard ceiling.
4. **Caching and batching to cut the rate-limit exposure**, plus resuming a
   batch run from where it stopped rather than restarting it.
5. **Shadow DOM traversal**, including open roots explicitly, which is a
   contained change to the element collector.
6. **A real test matrix.** The offline suites cover the loop, the model calls,
   the bot and the packaging, and the driver is tested against one live site.
   What is missing is a broad set of recorded pages to catch perception
   regressions without hitting the network.
7. **Per-run cost and token accounting** in the trace, which right now records
   steps and actions but not what they cost.
