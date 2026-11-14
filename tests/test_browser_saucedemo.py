"""Smoke test for agent/browser.py against saucedemo.com.

No LLM involved: this drives the actions by hand to prove observe() finds the
right elements and every action works. Element ids are looked up from the
observation by their visible name, never hardcoded.

    python tests/test_browser_saucedemo.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.browser import BrowserDriver, Observation, new_run_dir  # noqa: E402

SITE = "https://www.saucedemo.com"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def find(obs: Observation, *, name: str = "", role: str = "", placeholder: str = "") -> int:
    """Lowest-numbered element matching the given visible attributes."""
    for e in obs.elements:
        if name and name.lower() not in e.name.lower():
            continue
        if role and e.role != role:
            continue
        if placeholder and placeholder.lower() not in e.placeholder.lower():
            continue
        return e.id
    raise AssertionError(
        f"no element matching name={name!r} role={role!r} placeholder={placeholder!r}\n"
        + "\n".join(f"  {e.describe()}" for e in obs.elements)
    )


async def main() -> int:
    run_dir = new_run_dir("runs")
    print(f"run dir: {run_dir}\n")

    async with BrowserDriver(headless=True, run_dir=run_dir) as drv:
        # --- goto + observe ------------------------------------------------
        print(await drv.goto(SITE))
        obs = await drv.observe("login")
        print(f"\n{obs.to_prompt()[:1400]}\n...\n")

        check("goto reached the site", "saucedemo.com" in obs.url, obs.url)
        check("observe found elements", len(obs.elements) >= 3, f"{len(obs.elements)} found")
        check("screenshot captured", len(obs.screenshot) > 5000, f"{len(obs.screenshot)} bytes")
        check(
            "screenshot saved to run dir",
            bool(obs.screenshot_path) and Path(obs.screenshot_path).exists(),
            str(obs.screenshot_path),
        )
        check("page text extracted", "Swag Labs" in obs.text or len(obs.text) > 50)
        check("fingerprint is stable", obs.fingerprint == (await drv.observe()).fingerprint)

        user_id = find(obs, placeholder="Username")
        pass_id = find(obs, placeholder="Password")
        login_id = find(obs, name="Login", role="button")
        check("username/password/login all numbered", True, f"{user_id}/{pass_id}/{login_id}")

        # --- type + click (a real login) -----------------------------------
        print(await drv.type(user_id, "standard_user"))
        print(await drv.type(pass_id, "secret_sauce"))
        obs = await drv.observe("filled")
        typed = [e for e in obs.elements if e.id == user_id and e.value == "standard_user"]
        check("type() landed in the field and observe() reads it back", bool(typed))
        pw = [e for e in obs.elements if e.id == pass_id]
        check("password value is masked in the observation", bool(pw) and "secret" not in pw[0].value)

        print(await drv.click(find(obs, name="Login", role="button")))
        obs = await drv.observe("inventory")
        check("click() logged in", "inventory" in obs.url, obs.url)
        check("inventory items visible", "Sauce Labs Backpack" in obs.text)

        before = obs.fingerprint

        # --- select --------------------------------------------------------
        sort_id = find(obs, role="select")
        opts = [e for e in obs.elements if e.id == sort_id][0].options
        check("select options are reported", len(opts) >= 2, str(opts))
        print(await drv.select(sort_id, "Price (high to low)"))
        obs = await drv.observe("sorted")
        prices = [float(m) for m in re.findall(r"\$\s*([0-9]+\.[0-9]{2})", obs.text)]
        check("select() changed the page", obs.fingerprint != before, f"prices {prices}")
        check("sorted high to low", bool(prices) and prices == sorted(prices, reverse=True),
              str(prices))

        # --- scroll --------------------------------------------------------
        top_y = obs.scroll.get("y")
        print(await drv.scroll("bottom"))
        obs = await drv.observe("scrolled")
        check("scroll() moved the viewport", obs.scroll.get("y", 0) > (top_y or 0),
              f"{top_y} -> {obs.scroll.get('y')}")

        # --- click into a detail page, then back ---------------------------
        print(await drv.scroll("top"))
        obs = await drv.observe()
        print(await drv.click(find(obs, name="View details for Sauce Labs Backpack")))
        obs = await drv.observe("detail")
        check("navigated to the item page", "inventory-item" in obs.url, obs.url)
        print(await drv.back())
        obs = await drv.observe("back")
        check("back() returned to the list", "inventory.html" in obs.url, obs.url)

        # --- press ---------------------------------------------------------
        print(await drv.press("Escape"))
        check("press() ran without error", True)

        # --- wait ----------------------------------------------------------
        print(await drv.wait(0.5))

        # --- stale element is reported, not silently wrong -----------------
        stale = max(e.id for e in obs.elements) + 500
        try:
            await drv.click(stale)
            check("unknown element id raises ActionError", False, "no error raised")
        except Exception as exc:  # ActionError
            check("unknown element id raises ActionError", "no longer on the page" in str(exc),
                  type(exc).__name__)

        # --- menu in a drawer: proves off-screen/hidden handling -----------
        obs = await drv.observe("menu-closed")
        print(await drv.click(find(obs, name="Open Menu")))
        obs = await drv.observe("menu-open")
        check("drawer contents appear after click", "Logout" in obs.text)
        print(await drv.click(find(obs, name="Logout")))
        obs = await drv.observe("logged-out")
        check("logout returned to the login page",
              find(obs, name="Login", role="button") > 0 and obs.url.rstrip("/").endswith(".com"),
              obs.url)

    shots = sorted(p.name for p in run_dir.glob("*.png"))
    print(f"\n{len(shots)} screenshots in {run_dir}: {', '.join(shots)}")
    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
