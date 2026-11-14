"""Playwright driver: the agent's eyes and hands.

No LLM code lives here. The only contract with the agent layer is:
  - observe()  -> Observation (numbered interactive elements + screenshot)
  - the action methods (click/type/select/press/goto/wait/scroll)

Elements are numbered fresh on every observe() and tagged in the DOM with
``data-wta-id`` so an action can find the same node again without the agent
ever seeing a CSS selector. Nothing here knows about any specific site.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

ATTR = "data-wta-id"
MAX_ELEMENTS = 150
TEXT_BUDGET = 2500
DEFAULT_TIMEOUT_MS = 15_000
NETWORK_IDLE_MS = 2_500     # short: many pages never truly go idle
DOM_QUIET_MS = 500          # no DOM mutations for this long = settled
DOM_QUIET_CAP_MS = 4_000    # a page that animates forever must not hang us


class ActionError(RuntimeError):
    """An action could not be carried out (stale element, bad id, timeout...)."""


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------

@dataclass
class Element:
    id: int
    tag: str
    role: str
    name: str
    value: str = ""
    placeholder: str = ""
    type: str = ""
    href: str = ""
    options: list[str] = field(default_factory=list)
    checked: Optional[bool] = None
    disabled: bool = False
    in_viewport: bool = True
    frame: int = 0
    context: str = ""

    def describe(self, with_context: bool = False) -> str:
        bits = [f"[{self.id}]", self.role or self.tag]
        if self.name:
            bits.append('"%s"' % self.name)
        if with_context and self.context:
            bits.append('in "%s"' % self.context)
        if self.type and self.type not in ("text", "submit"):
            bits.append(f"type={self.type}")
        if self.placeholder and self.placeholder != self.name:
            bits.append('placeholder="%s"' % self.placeholder)
        if self.value:
            bits.append('value="%s"' % self.value)
        if self.options:
            shown = ", ".join(self.options[:8])
            if len(self.options) > 8:
                shown += f", +{len(self.options) - 8} more"
            bits.append(f"options=[{shown}]")
        if self.checked is not None:
            bits.append("checked" if self.checked else "unchecked")
        if self.href:
            bits.append(f"-> {self.href}")
        if self.disabled:
            bits.append("(disabled)")
        if not self.in_viewport:
            bits.append("(off-screen)")
        if self.frame:
            bits.append(f"(iframe {self.frame})")
        return " ".join(bits)


@dataclass
class Observation:
    url: str
    title: str
    elements: list[Element]
    text: str
    screenshot: bytes
    screenshot_path: Optional[str] = None
    dialogs: list[str] = field(default_factory=list)
    scroll: dict[str, Any] = field(default_factory=dict)
    tabs: int = 1
    truncated: int = 0
    busy: bool = False

    @property
    def fingerprint(self) -> str:
        """Stable digest of the page state, for no-change / loop detection."""
        sig = "|".join(
            [self.url, self.title]
            + [f"{e.tag}:{e.role}:{e.name}:{e.value}:{e.checked}" for e in self.elements]
            + [self.text[:1200]]
        )
        return hashlib.sha256(sig.encode("utf-8", "ignore")).hexdigest()[:16]

    def to_prompt(self) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.tabs > 1:
            lines.append(f"TABS OPEN: {self.tabs} (acting on the newest)")
        if self.busy:
            lines.append(
                "PAGE STATE: something on this page is still loading. If the content "
                "you need is not here yet, use wait (2-5s) rather than another action."
            )
        if self.dialogs:
            lines.append("BROWSER DIALOGS since last step: " + " | ".join(self.dialogs))
        if self.scroll:
            lines.append(
                "SCROLL: y=%s of %s (viewport %s)"
                % (self.scroll.get("y"), self.scroll.get("height"), self.scroll.get("viewport"))
            )
        lines.append("")
        lines.append("INTERACTIVE ELEMENTS:")
        if self.elements:
            # Several controls can share a name ("Add to cart" once per product).
            # Those, and only those, are shown with the row they belong to.
            seen: dict[str, int] = {}
            for e in self.elements:
                seen[e.name] = seen.get(e.name, 0) + 1
            lines += [f"  {e.describe(with_context=seen.get(e.name, 0) > 1)}" for e in self.elements]
        else:
            lines.append("  (none found)")
        if self.truncated:
            lines.append(f"  ... {self.truncated} further elements omitted; scroll to reveal")
        lines.append("")
        lines.append("PAGE TEXT:")
        lines.append(self.text or "(empty)")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# JS collectors (run once per frame)
# --------------------------------------------------------------------------

_COLLECT_JS = r"""
(startId) => {
  const SEL = [
    'a[href]','button','input','select','textarea','summary',
    '[role=button]','[role=link]','[role=checkbox]','[role=radio]','[role=tab]',
    '[role=menuitem]','[role=menuitemcheckbox]','[role=option]','[role=switch]',
    '[role=combobox]','[role=textbox]','[role=searchbox]','[role=spinbutton]','[role=slider]',
    '[onclick]','[contenteditable=""]','[contenteditable="true"]','label','[tabindex]'
  ].join(',');
  const ALWAYS = new Set(['INPUT','SELECT','TEXTAREA','BUTTON','A']);
  const vw = window.innerWidth, vh = window.innerHeight;

  document.querySelectorAll('[data-wta-id]').forEach(e => e.removeAttribute('data-wta-id'));

  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 140);
  const visible = (el, r) => {
    if (!r || r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    // Transparent form controls are real and clickable: sites routinely layer a
    // near-zero-opacity native select or file input over a styled widget. Only
    // treat low opacity as "hidden" for generic nodes.
    if (!ALWAYS.has(el.tagName) && parseFloat(st.opacity || '1') < 0.05) return false;
    return true;
  };
  const nameOf = el => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const t = labelled.split(/\s+/).map(i => {
        const n = document.getElementById(i);
        return n ? clean(n.innerText || n.textContent) : '';
      }).filter(Boolean).join(' ');
      if (t) return clean(t);
    }
    if (el.labels && el.labels.length) {
      const t = clean(el.labels[0].innerText || el.labels[0].textContent);
      if (t) return t;
    }
    const own = clean(el.innerText || el.textContent);
    if (own) return own;
    for (const a of ['title','alt','placeholder','name','value','id']) {
      const v = clean(el.getAttribute && el.getAttribute(a));
      if (v) return v;
    }
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img) return clean(img.getAttribute('alt'));
    return '';
  };
  const roleOf = el => {
    const r = el.getAttribute('role');
    if (r) return clean(r);
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return t;
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      return 'input';
    }
    return tag;
  };

  // What row/card/section is this control part of? Repeated controls ("Add to
  // cart", "Edit", "Buy") are only tellable apart by the thing they belong to.
  const contextOf = (el, own) => {
    let p = el.parentElement, hops = 0;
    while (p && hops++ < 5) {
      if (p.tagName === 'BODY' || p.tagName === 'MAIN') break;
      const h = p.querySelector(
        'h1,h2,h3,h4,h5,h6,[class*=title],[class*=name],[class*=heading],th,a,strong,b'
      );
      if (h && h !== el && !h.contains(el)) {
        const t = clean(h.innerText || h.textContent);
        if (t && t.length > 1 && t !== own) return t.slice(0, 70);
      }
      p = p.parentElement;
    }
    return '';
  };

  const chosen = new Set();
  const seenLinks = new Set();
  const out = [];
  let id = startId;

  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= 400) break;
    const r = el.getBoundingClientRect();
    if (!visible(el, r)) continue;

    // Drop a wrapper's duplicate: skip nested nodes whose interactive ancestor
    // is already listed, unless the node is a real form control of its own.
    if (!ALWAYS.has(el.tagName)) {
      let p = el.parentElement, nested = false;
      while (p) { if (chosen.has(p)) { nested = true; break; } p = p.parentElement; }
      if (nested) continue;
    }

    const name = nameOf(el);
    const tag = el.tagName.toLowerCase();
    if (!name && !['input','select','textarea'].includes(tag)) continue;

    const item = {
      id: 0,
      tag: tag,
      role: roleOf(el),
      name: name,
      value: '',
      placeholder: clean(el.getAttribute && el.getAttribute('placeholder')),
      type: clean(el.getAttribute && el.getAttribute('type')),
      href: '',
      options: [],
      checked: null,
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      in_viewport: r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw,
      context: contextOf(el, name)
    };

    if (tag === 'input' || tag === 'textarea') {
      const t = (el.type || '').toLowerCase();
      item.value = t === 'password'
        ? (el.value ? '*'.repeat(el.value.length) : '')
        : clean(el.value);
      if (t === 'checkbox' || t === 'radio') item.checked = !!el.checked;
    } else if (tag === 'select') {
      item.options = Array.from(el.options).slice(0, 40)
        .map(o => clean(o.textContent) || clean(o.value));
      item.value = clean(el.value);
    } else if (tag === 'a') {
      const h = el.getAttribute('href') || '';
      if (h && h.indexOf('javascript:') !== 0) {
        try { item.href = new URL(h, location.href).href.slice(0, 120); }
        catch (e) { item.href = h.slice(0, 120); }
      }
    } else if (el.getAttribute && el.getAttribute('aria-checked')) {
      item.checked = el.getAttribute('aria-checked') === 'true';
    }

    // One target per destination: an image and a caption linking to the same
    // place are two nodes but one choice, so list only the first.
    if (item.href) {
      const key = item.name + '||' + item.href;
      if (seenLinks.has(key)) continue;
      seenLinks.add(key);
    }

    item.id = id++;
    chosen.add(el);
    el.setAttribute('data-wta-id', String(item.id));
    out.push(item);
  }
  return {
    elements: out,
    scroll: {
      y: Math.round(window.scrollY),
      height: Math.round(document.body ? document.body.scrollHeight : 0),
      viewport: vh
    }
  };
}
"""

_TEXT_JS = r"""
() => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','HEAD','SVG']);
  const parts = [];
  let total = 0;
  const walk = node => {
    if (total > 8000) return;
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        const t = child.textContent.replace(/\s+/g, ' ').trim();
        if (t) {
          parts.push(t);
          total += t.length + 3;
        }
      } else if (child.nodeType === 1 && !skip.has(child.tagName)) {
        // Never descend into a hidden subtree. Checking only a text node's
        // own parent is not enough: a hidden container's text would leak in
        // and describe a page the user cannot see.
        const st = getComputedStyle(child);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        walk(child);
      }
    }
  };
  if (document.body) walk(document.body);
  // A lone symbol in its own node ("$" before "49.99") must not be separated
  // from what it belongs to, or prices and the like become unreadable.
  const merged = [];
  for (const p of parts) {
    const prev = merged[merged.length - 1];
    if (prev !== undefined && /^[^\w\s]{1,2}$/.test(prev)) merged[merged.length - 1] = prev + p;
    else merged.push(p);
  }
  return merged.join(' | ');
}
"""

_OVERLAY_JS = r"""
() => {
  const old = document.getElementById('__wta_overlay');
  if (old) old.remove();
  const box = document.createElement('div');
  box.id = '__wta_overlay';
  box.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;';
  const palette = ['#e6194b','#3cb44b','#4363d8','#f58231','#911eb4','#008080','#9a6324'];
  let i = 0;
  for (const el of document.querySelectorAll('[data-wta-id]')) {
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight || r.width < 2 || r.height < 2) continue;
    const c = palette[i++ % palette.length];
    const f = document.createElement('div');
    f.style.cssText = 'position:fixed;left:' + r.left + 'px;top:' + r.top + 'px;width:'
      + r.width + 'px;height:' + r.height + 'px;border:2px solid ' + c + ';box-sizing:border-box;';
    const lab = document.createElement('div');
    lab.textContent = el.getAttribute('data-wta-id');
    const below = r.top < 16;
    lab.style.cssText = 'position:fixed;left:' + Math.max(0, r.left) + 'px;top:'
      + (below ? r.bottom : r.top - 15) + 'px;background:' + c
      + ';color:#fff;font:bold 11px/13px monospace;padding:0 3px;border-radius:2px;';
    box.appendChild(f);
    box.appendChild(lab);
  }
  document.documentElement.appendChild(box);
}
"""

_OVERLAY_OFF_JS = "() => { const o = document.getElementById('__wta_overlay'); if (o) o.remove(); }"

# Resolve once the DOM has stopped changing for `quiet` ms, so content that
# arrives a moment after a click is in the next observation. `cap` stops a page
# that animates forever from hanging the loop.
_DOM_QUIET_JS = r"""
([quiet, cap]) => new Promise(resolve => {
  let timer = null;
  const started = Date.now();
  const observer = new MutationObserver(() => {
    if (Date.now() - started > cap) return finish('cap');
    clearTimeout(timer);
    timer = setTimeout(() => finish('quiet'), quiet);
  });
  function finish(why) {
    clearTimeout(timer);
    try { observer.disconnect(); } catch (e) {}
    resolve(why);
  }
  try {
    observer.observe(document, {
      subtree: true, childList: true, attributes: true, characterData: true
    });
  } catch (e) { return resolve('no-dom'); }
  timer = setTimeout(() => finish('quiet'), quiet);
  setTimeout(() => finish('cap'), cap);
});
"""

# Is the page telling us it is still working? Kept to markers that mean the
# same thing on any site: ARIA state, <progress>, and the usual class names.
_BUSY_JS = r"""
() => {
  const sel = '[aria-busy="true"],[role="progressbar"],progress,'
    + '[class*="loading" i],[class*="spinner" i],[class*="progress" i],[id*="loading" i]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    return true;
  }
  return false;
}
"""


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

class BrowserDriver:
    """Owns a headless Chromium and exposes observe() + the agent's actions."""

    def __init__(
        self,
        headless: bool = True,
        run_dir: Optional[str | Path] = None,
        viewport: tuple[int, int] = (1280, 900),
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.headless = headless
        self.run_dir = Path(run_dir) if run_dir else None
        self.viewport = viewport
        self.timeout_ms = timeout_ms

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._frames: dict[int, Frame] = {}
        self._dialogs: list[str] = []
        self._shot_n = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._ctx = await self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
            locale="en-US",
            ignore_https_errors=True,
        )
        self._ctx.set_default_timeout(self.timeout_ms)
        self._ctx.on("page", self._on_new_page)
        self._page = await self._ctx.new_page()
        self._wire(self._page)
        if self.run_dir:
            self.run_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        for obj in (self._ctx, self._browser):
            if obj is not None:
                try:
                    await obj.close()
                except PlaywrightError:
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except PlaywrightError:
                pass
        self._pw = self._browser = self._ctx = self._page = None

    async def __aenter__(self) -> "BrowserDriver":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _wire(self, page: Page) -> None:
        # Never let a native dialog block the loop; record it so the agent sees it.
        page.on("dialog", lambda d: asyncio.ensure_future(self._on_dialog(d)))

    async def _on_dialog(self, dialog: Any) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message[:200]}")
        try:
            await dialog.accept()
        except PlaywrightError:
            pass

    def _on_new_page(self, page: Page) -> None:
        # A click opened a tab/window: follow it, that is where the task continues.
        self._wire(page)
        self._page = page

    @property
    def page(self) -> Page:
        if self._page is None or self._page.is_closed():
            alive = [p for p in (self._ctx.pages if self._ctx else []) if not p.is_closed()]
            if not alive:
                raise ActionError("no open page; the browser was closed")
            self._page = alive[-1]
        return self._page

    # -- observe -----------------------------------------------------------

    async def observe(self, label: str = "") -> Observation:
        page = self.page
        await self._settle()

        self._frames = {}
        elements: list[Element] = []
        scroll: dict[str, Any] = {}
        next_id = 1

        frames = [page.main_frame] + [
            f for f in page.frames if f is not page.main_frame and not f.is_detached()
        ]
        frames = frames[:8]
        for fi, frame in enumerate(frames):
            try:
                data = await frame.evaluate(_COLLECT_JS, next_id)
            except PlaywrightError:
                continue  # cross-origin, or navigating away under us
            if fi == 0:
                scroll = data.get("scroll", {}) or {}
            raw = data.get("elements", []) or []
            if not raw:
                continue
            self._frames[fi] = frame
            for item in raw:
                elements.append(Element(frame=fi, **item))
            next_id = raw[-1]["id"] + 1

        truncated = 0
        if len(elements) > MAX_ELEMENTS:
            # Keep what the user can actually see first, then the rest in order.
            on = [e for e in elements if e.in_viewport]
            off = [e for e in elements if not e.in_viewport]
            kept = (on + off)[:MAX_ELEMENTS]
            truncated = len(elements) - len(kept)
            elements = sorted(kept, key=lambda e: e.id)

        # Read every frame, not just the main one: on frameset pages and sites
        # that put the real content in an iframe, the main document has no text.
        chunks: list[str] = []
        for fi, frame in enumerate(frames):
            try:
                chunk = await frame.evaluate(_TEXT_JS)
            except PlaywrightError:
                continue
            chunk = re.sub(r"\s+", " ", chunk or "").strip()
            if chunk:
                chunks.append(chunk if fi == 0 else f"[iframe {fi}] {chunk}")
        text = "\n".join(chunks)
        if len(text) > TEXT_BUDGET:
            text = text[:TEXT_BUDGET] + " ...[truncated]"

        try:
            busy = bool(await page.evaluate(_BUSY_JS))
        except PlaywrightError:
            busy = False

        shot, path = await self._screenshot(label)
        dialogs, self._dialogs = self._dialogs, []

        return Observation(
            url=page.url,
            title=await self._safe_title(),
            elements=elements,
            text=text,
            screenshot=shot,
            screenshot_path=path,
            dialogs=dialogs,
            scroll=scroll,
            tabs=len([p for p in (self._ctx.pages if self._ctx else []) if not p.is_closed()]),
            truncated=truncated,
            busy=busy,
        )

    async def _safe_title(self) -> str:
        try:
            return (await self.page.title())[:200]
        except PlaywrightError:
            return ""

    async def _screenshot(self, label: str = "") -> tuple[bytes, Optional[str]]:
        """Viewport screenshot with the element numbers drawn on it."""
        for frame in self._frames.values():
            try:
                await frame.evaluate(_OVERLAY_JS)
            except PlaywrightError:
                pass
        try:
            shot = await self.page.screenshot(type="png", timeout=self.timeout_ms)
        except PlaywrightError as exc:
            raise ActionError(f"screenshot failed: {_short(exc)}") from exc
        finally:
            for frame in self._frames.values():
                try:
                    await frame.evaluate(_OVERLAY_OFF_JS)
                except PlaywrightError:
                    pass

        path = None
        if self.run_dir:
            self._shot_n += 1
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]
            name = f"{self._shot_n:02d}" + (f"-{slug}" if slug else "") + ".png"
            dest = self.run_dir / name
            dest.write_bytes(shot)
            path = str(dest)
        return shot, path

    # -- actions -----------------------------------------------------------

    async def _target(self, element_id: int):
        if not self._frames:
            raise ActionError("call observe() before acting")
        for frame in self._frames.values():
            if frame.is_detached():
                continue
            loc = frame.locator('[%s="%s"]' % (ATTR, element_id)).first
            try:
                if await loc.count():
                    return loc
            except PlaywrightError:
                continue
        raise ActionError(
            f"element {element_id} is no longer on the page (it changed) - observe again"
        )

    async def click(self, element_id: int) -> str:
        loc = await self._target(element_id)
        desc = await self._label(loc)
        await self._reveal(loc)
        try:
            await loc.click(timeout=self.timeout_ms)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            try:  # covered by an overlay or odd CSS: go straight at the node
                await loc.click(timeout=4000, force=True)
            except PlaywrightError:
                try:
                    await loc.evaluate("el => el.click()")
                except PlaywrightError:
                    raise ActionError(
                        f"could not click [{element_id}] {desc}: {_short(exc)}"
                    ) from exc
        await self._settle()
        return f"clicked [{element_id}] {desc}"

    async def type(self, element_id: int, text: str, clear: bool = True) -> str:
        loc = await self._target(element_id)
        desc = await self._label(loc)
        await self._reveal(loc)
        try:
            tag = (await loc.evaluate("el => el.tagName.toLowerCase()")) or ""
            if tag in ("input", "textarea"):
                if clear:
                    await loc.fill("", timeout=self.timeout_ms)
                await loc.type(text, delay=25, timeout=self.timeout_ms)
            else:  # contenteditable or a custom widget
                await loc.click(timeout=self.timeout_ms)
                if clear:
                    await self.page.keyboard.press("Control+A")
                await self.page.keyboard.type(text, delay=25)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise ActionError(
                f"could not type into [{element_id}] {desc}: {_short(exc)}"
            ) from exc
        await self._settle()
        shown = text if len(text) < 60 else text[:57] + "..."
        return 'typed "%s" into [%s] %s' % (shown, element_id, desc)

    async def select(self, element_id: int, value: str) -> str:
        loc = await self._target(element_id)
        desc = await self._label(loc)
        attempts: list[dict[str, Any]] = [{"label": value}, {"value": value}]
        idx = _as_index(value)
        if idx is not None:
            attempts.append({"index": idx})
        for kwargs in attempts:
            try:
                if await loc.select_option(timeout=self.timeout_ms, **kwargs):
                    await self._settle()
                    return 'selected "%s" in [%s] %s' % (value, element_id, desc)
            except PlaywrightError:
                continue
        opts = await self._options(loc)
        raise ActionError(
            'could not select "%s" in [%s] %s; options are: %s' % (value, element_id, desc, opts)
        )

    async def press(self, key: str) -> str:
        try:
            await self.page.keyboard.press(key)
        except PlaywrightError as exc:
            raise ActionError(f"could not press {key}: {_short(exc)}") from exc
        await self._settle()
        return f"pressed {key}"

    async def goto(self, url: str) -> str:
        # "example.com" needs a scheme; "data:"/"about:" style URIs have one
        # already even though they carry no "//".
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) and not re.match(
            r"^(data|about|blob|file|view-source):", url, re.I
        ):
            url = "https://" + url
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            return f"navigation to {url} timed out; showing whatever loaded"
        except PlaywrightError as exc:
            raise ActionError(f"could not open {url}: {_short(exc)}") from exc
        await self._settle()
        return f"opened {url}"

    async def wait(self, seconds: float) -> str:
        seconds = max(0.0, min(float(seconds), 20.0))
        await asyncio.sleep(seconds)
        return f"waited {seconds:g}s"

    async def scroll(self, direction: str = "down", amount: Optional[int] = None) -> str:
        d = (direction or "down").lower()
        step = amount if amount is not None else int(self.viewport[1] * 0.8)
        js = {
            "down": f"window.scrollBy(0, {step})",
            "up": f"window.scrollBy(0, -{step})",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
            "right": f"window.scrollBy({step}, 0)",
            "left": f"window.scrollBy(-{step}, 0)",
        }.get(d, f"window.scrollBy(0, {step})")
        try:
            await self.page.evaluate(js)
        except PlaywrightError as exc:
            raise ActionError(f"could not scroll: {_short(exc)}") from exc
        await asyncio.sleep(0.35)
        return f"scrolled {d}"

    async def back(self) -> str:
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightError as exc:
            raise ActionError(f"could not go back: {_short(exc)}") from exc
        await self._settle()
        return "went back"

    # -- helpers -----------------------------------------------------------

    async def _reveal(self, loc: Any) -> None:
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
        except PlaywrightError:
            pass

    async def _settle(self) -> None:
        """Wait for the page to stop moving, without ever hanging the loop.

        Network idle alone is not enough: content is often written to the DOM
        just after the last request finishes. So we also wait for the DOM
        itself to go quiet, which is what makes delayed content show up in the
        next observation rather than the one after.
        """
        try:
            page = self.page
        except ActionError:
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except PlaywrightError:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
        except PlaywrightError:
            pass
        try:
            await page.evaluate(_DOM_QUIET_JS, [DOM_QUIET_MS, DOM_QUIET_CAP_MS])
        except PlaywrightError:
            # Navigating away mid-wait is normal; the next observe() re-reads.
            await asyncio.sleep(DOM_QUIET_MS / 1000)

    async def _label(self, loc: Any) -> str:
        txt = ""
        try:
            txt = (await loc.get_attribute("aria-label")) or ""
            if not txt:
                txt = (await loc.inner_text(timeout=1500)) or ""
            if not txt:
                txt = (
                    (await loc.get_attribute("placeholder"))
                    or (await loc.get_attribute("name"))
                    or ""
                )
        except PlaywrightError:
            pass
        return re.sub(r"\s+", " ", txt).strip()[:60] or "element"

    async def _options(self, loc: Any) -> str:
        try:
            opts = await loc.evaluate(
                "el => el.options ? Array.from(el.options).map(o => o.textContent.trim()) : []"
            )
            return ", ".join(opts[:15]) or "(none)"
        except PlaywrightError:
            return "(unknown)"


def _as_index(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _short(exc: BaseException) -> str:
    return re.sub(r"\s+", " ", str(exc)).strip()[:180]


def new_run_dir(root: str | Path = "runs") -> Path:
    """runs/<timestamp>/ for this run's trace and screenshots."""
    d = Path(root) / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d
