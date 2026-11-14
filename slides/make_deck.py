"""Build the 10-slide deck.

    python slides/make_deck.py                 # -> slides/web-task-agent.pptx
    python slides/make_deck.py --out other.pptx

The results slide is read from reports/SUMMARY.md and a few screenshots are
pulled from reports/. If those do not exist yet the deck still builds, with the
results slide saying so: run run_targets.py first for the real numbers.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

W, H = Inches(13.333), Inches(7.5)          # 16:9
INK = RGBColor(0x1A, 0x1A, 0x1A)
MUTED = RGBColor(0x6B, 0x6B, 0x6B)
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
BAD = RGBColor(0xB3, 0x1F, 0x1F)
GOOD = RGBColor(0x1E, 0x6B, 0x33)


def _say(text: str = "") -> None:
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")


# --------------------------------------------------------------------------
# reading the results
# --------------------------------------------------------------------------

def read_summary() -> tuple[list[list[str]], str]:
    """The results table from reports/SUMMARY.md, as rows of cells."""
    path = REPORTS / "SUMMARY.md"
    if not path.exists():
        return [], ""
    text = path.read_text(encoding="utf-8")
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):      # the ---- separator
            continue
        rows.append(cells)
    headline = ""
    m = re.search(r"^(\d+ of \d+ runs matched[^.]*\.)", text, re.M)
    if m:
        headline = m.group(1)
    return rows, headline


def find_screenshots(limit: int = 3) -> list[Path]:
    """A few screenshots from the reports, newest task folders first."""
    if not REPORTS.is_dir():
        return []
    shots: list[Path] = []
    for folder in sorted(p for p in REPORTS.iterdir() if p.is_dir()):
        pngs = sorted(folder.glob("*.png"))
        if pngs:
            shots.append(pngs[-1])        # the final state of that task
        if len(shots) >= limit:
            break
    return shots[:limit]


# --------------------------------------------------------------------------
# slide helpers
# --------------------------------------------------------------------------

def blank(prs: Presentation):
    return prs.slides.add_slide(prs.slide_layouts[6])


def textbox(slide, left, top, width, height, text, size=18, bold=False,
            color=INK, space_after=10):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.space_after = Pt(space_after)
        for run in p.runs:
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = color
    return box


def heading(slide, title: str, kicker: str = "") -> None:
    textbox(slide, Inches(0.8), Inches(0.5), Inches(11.7), Inches(1.0),
            title, size=34, bold=True, color=ACCENT)
    if kicker:
        textbox(slide, Inches(0.8), Inches(1.35), Inches(11.7), Inches(0.6),
                kicker, size=16, color=MUTED)


def bullets(slide, lines: list[str], top=Inches(2.2), size=20, left=Inches(1.0)) -> None:
    textbox(slide, left, top, Inches(11.3), Inches(4.6),
            "\n".join(f"-  {line}" for line in lines), size=size, space_after=16)


def mono(slide, text: str, top, size=14, left=Inches(1.0), width=Inches(11.3)):
    box = textbox(slide, left, top, width, Inches(3.4), text, size=size)
    for p in box.text_frame.paragraphs:
        for run in p.runs:
            run.font.name = "Consolas"
        p.space_after = Pt(2)
    return box


def picture(slide, path: Path, left, top, height) -> None:
    try:
        slide.shapes.add_picture(str(path), left, top, height=height)
    except Exception as exc:                 # a bad PNG must not kill the deck
        _say(f"  note: could not place {path.name}: {exc}")


# --------------------------------------------------------------------------
# the slides
# --------------------------------------------------------------------------

def build(out: Path) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H

    # 1 - title / problem
    s = blank(prs)
    textbox(s, Inches(0.9), Inches(2.3), Inches(11.5), Inches(1.2),
            "Web Task Agent", size=50, bold=True, color=ACCENT)
    textbox(s, Inches(0.9), Inches(3.4), Inches(11.5), Inches(1.6),
            "A goal in plain English and a URL.\n"
            "The agent drives a real browser and reports what it actually found.",
            size=22, color=INK)
    textbox(s, Inches(0.9), Inches(5.2), Inches(11.5), Inches(1.0),
            "No hardcoded selectors. No scripted steps. Must work on sites it has never seen.",
            size=16, color=MUTED)

    # 2 - the problem
    s = blank(prs)
    heading(s, "The problem", "Why this is not just scripting a browser")
    bullets(s, [
        "A real user's goal does not map to a fixed sequence of clicks.",
        "Every site names and arranges things differently.",
        "The agent cannot be told the selectors: it has to work them out.",
        "Hardest part is not acting. It is knowing when it has succeeded,",
        "and being willing to say that it has not.",
    ])

    # 3 - architecture
    s = blank(prs)
    heading(s, "Architecture", "Three layers that do not reach into each other")
    mono(s, "agent/browser.py     Playwright only. No LLM, no prompts, no goal.\n"
            "                     observe() -> numbered elements + text + screenshot\n"
            "                     click / type / select / press / goto / wait / scroll\n"
            "                            |\n"
            "agent/agent.py       The loop. Model calls, recovery, verification, trace.\n"
            "                            |\n"
            "agent/bot.py         Telegram. Uses on_step and ask_user callbacks only.\n"
            "agent/prompts.py     All prompt text.\n\n"
            "run_cli.py  run_targets.py        same TaskAgent, different callbacks",
         top=Inches(2.1), size=15)
    textbox(s, Inches(1.0), Inches(6.1), Inches(11.3), Inches(0.8),
            "The browser layer knows nothing about goals. The bot changed nothing in the loop.",
            size=15, color=MUTED)

    # 4 - the loop
    s = blank(prs)
    heading(s, "The loop", "observe -> plan -> act -> verify")
    mono(s, "every step:\n"
            "  observe()        numbered elements, page text, fingerprint\n"
            "  plan             goal + page + last 4 steps -> ONE json action\n"
            "  act              click / type / select / press / goto / wait / scroll\n"
            "  check            did the page actually change?\n\n"
            "the model emits exactly one action per step:\n"
            '  {\"action\": \"click\", \"id\": 12}\n'
            '  {\"action\": \"type\", \"id\": 3, \"text\": \"ada\"}\n'
            '  {\"action\": \"done\", \"result\": \"...\", \"evidence\": \"...\"}\n'
            '  {\"action\": \"blocked\", \"reason\": \"...\"}',
         top=Inches(2.1), size=15)

    # 5 - perception
    s = blank(prs)
    heading(s, "Perception", "The model picks a number, never a selector")
    mono(s, 'INTERACTIVE ELEMENTS:\n'
            '  [1] button "Open Menu"\n'
            '  [3] select "Sort products" options=[Name (A to Z), Price (low to high), ...]\n'
            '  [5] button "Add to cart" in "Sauce Labs Backpack"\n'
            '  [7] button "Add to cart" in "Sauce Labs Bike Light"',
         top=Inches(2.1), size=15)
    bullets(s, [
        "Found by role and accessible name, the way a screen reader reads a page.",
        "Tagged in the DOM, so a number cannot be hallucinated into a bad selector.",
        "Repeated names carry the row they belong to: the hard case without vision.",
        "All frames read; transparent native controls included.",
    ], top=Inches(4.0), size=17)

    # 6 - recovery
    s = blank(prs)
    heading(s, "Recovery", "Most of the work is handling things going wrong")
    bullets(s, [
        "Nothing happened: page fingerprint unchanged -> tell the model, do not repeat.",
        "Going in circles: the same action three times -> stop, do not burn the budget.",
        "Overlays: cookie and consent banners flagged before they swallow clicks.",
        "Action failed: stale id or failed click comes back as text to react to.",
        "MAX_STEPS: a hard cap, always.",
    ])

    # 7 - honest stopping
    s = blank(prs)
    heading(s, "Honest stopping", "The part that decides whether any of it is trustworthy")
    bullets(s, [
        "done is never taken on trust: a second call checks the claim against the page.",
        "Evidence must be quoted from the final page, not asserted.",
        "A verifier that errors returns reject, so a broken check cannot pass as success.",
        "Locked account, CAPTCHA, paywall, missing credential -> blocked or ask_user.",
    ], top=Inches(2.2))
    textbox(s, Inches(1.0), Inches(5.3), Inches(11.3), Inches(1.0),
            "A truthful blocked is a success. A fabricated done is a total failure.",
            size=22, bold=True, color=ACCENT)

    # 8 - model choice
    s = blank(prs)
    heading(s, "Model choice", "Groq openai/gpt-oss-120b, text-only")
    bullets(s, [
        "The model cannot see images, so the page is given to it as structured text.",
        "Screenshots are still captured every step, with numbered boxes drawn on them,",
        "    for the trace, the report and Telegram. They are for people, not the model.",
        "JSON mode, a lenient parser for fenced replies, and one corrective retry.",
        "Low reasoning effort: these are short, mechanical decisions.",
        "429s and 5xx retried with backoff that honours Retry-After.",
    ], size=18)

    # 9 - results
    s = blank(prs)
    rows, headline = read_summary()
    heading(s, "Results", headline or "Target tasks from tasks.json")
    if rows:
        want = ["Task", "Status", "Expected", "Steps", "Match"]
        head = rows[0]
        keep = [head.index(c) for c in want if c in head]
        table_rows = rows[: 1 + 8]
        shape = s.shapes.add_table(
            len(table_rows), len(keep), Inches(0.7), Inches(2.1),
            Inches(11.9), Inches(0.4) * len(table_rows)
        ).table
        for r, row in enumerate(table_rows):
            for c, idx in enumerate(keep):
                cell = shape.cell(r, c)
                cell.text = (row[idx] if idx < len(row) else "").replace("`", "").replace("**", "")
                for p in cell.text_frame.paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(13)
                        run.font.bold = r == 0
                        if r and "Match" in want and c == len(keep) - 1:
                            run.font.color.rgb = GOOD if cell.text == "yes" else BAD
    else:
        textbox(s, Inches(1.0), Inches(2.6), Inches(11.3), Inches(1.5),
                "No results yet.\n\nRun  python run_targets.py  to produce reports/SUMMARY.md,\n"
                "then rebuild this deck.", size=20, color=MUTED)

    shots = find_screenshots(3)
    if shots:
        left = Inches(0.7)
        for shot in shots:
            picture(s, shot, left, Inches(5.1), Inches(2.0))
            left += Inches(4.1)

    # 10 - limits and next
    s = blank(prs)
    heading(s, "Limits, and what I would do next", "The honest version is in LIMITS.md")
    textbox(s, Inches(1.0), Inches(2.1), Inches(5.4), Inches(4.2),
            "Where it breaks\n\n"
            "-  No vision: canvas, images, \"the red button\"\n"
            "-  CAPTCHAs: blocked by design, not solved\n"
            "-  Cross-origin frames, closed shadow DOM\n"
            "-  Rate limits on long runs\n"
            "-  Long forms can exhaust MAX_STEPS\n"
            "-  Non-deterministic: a pass is not a promise",
            size=17)
    textbox(s, Inches(6.9), Inches(2.1), Inches(5.4), Inches(4.2),
            "What I would harden\n\n"
            "-  Vision as a second opinion when text is ambiguous\n"
            "-  Better row context for repeated controls\n"
            "-  Step budget that scales with the work\n"
            "-  Resume a batch run; cache to cut rate limits\n"
            "-  Shadow DOM traversal\n"
            "-  Recorded pages to catch perception regressions",
            size=17)

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the slide deck.")
    ap.add_argument("--out", default=str(Path(__file__).parent / "web-task-agent.pptx"))
    args = ap.parse_args()

    out = Path(args.out)
    rows, _ = read_summary()
    if not rows:
        _say("note: reports/SUMMARY.md not found; the results slide will say so.")
    shots = find_screenshots(3)
    build(out)
    _say(f"wrote {out}  (10 slides, {len(rows[1:]) if rows else 0} result rows, "
         f"{len(shots)} screenshots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
