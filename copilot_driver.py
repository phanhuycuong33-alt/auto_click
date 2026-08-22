#!/usr/bin/env python3
"""
copilot_driver.py — Copilot acts as your "eyes + brain" to drive the browser.

Loop (up to --steps rounds):
  1. screenshot the page (or use --image for round 1)
  2. attach it to Copilot + ask "what should I do next?" (strict format)
  3. read Copilot's reply TEXT (no reply.png needed)
  4. parse the one-line command (click/fill/type/wait/scroll/goto/done)
  5. execute it in Firefox with Playwright
  6. repeat until "done" or steps exhausted

USAGE:
  .venv/bin/python3 copilot_driver.py "i want to make money with this web by doing a survey, what do i do next?"
  .venv/bin/python3 copilot_driver.py --image survey_web.png "task text here"
  .venv/bin/python3 copilot_driver.py --steps 5 "task text"

COMMAND FORMAT COPILOT RETURNS (one line, nothing else):
  click 'button or link text'
  fill 'field' with 'value'
  type 'text to type'
  wait '3'
  scroll 'down'   (or 'up')
  goto 'https://url'
  done
"""

import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

BASE = Path(__file__).resolve().parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)
PROFILE = BASE / "pw-profile"

URL = "https://copilot.microsoft.com/"
DEFAULT_STEPS = 5

ATTACH_OPENERS = ["Add context", "Attach", "Attach files", "Attach media"]

# The instruction sent to Copilot each round — it MUST output one parseable line.
INSTRUCTION = """TASK: {task}

I am automating this browser with Python + Playwright. I just attached a screenshot of the current page.

Reply with EXACTLY ONE line, nothing else, using ONLY one of these formats:

click 'button or link text'
fill 'field' with 'value'
type 'text to type'
wait '3'
scroll 'down'   (or 'up')
goto 'https://url'
done

Rules:
- Choose the single most useful next action for the task.
- For click, use the exact visible text of the button or link.
- For fill, use the field's label or placeholder text.
- If you need to see the result of your action before choosing more, output just that one action.
- When the task is fully done, output exactly: done"""


# ---------------------------------------------------------------- helpers
def visible(loc, timeout=2000):
    try:
        return loc.is_visible(timeout=timeout)
    except Exception:
        return False


def find_textbox(page):
    selectors = ('[role="textbox"]', "textarea", '[contenteditable="true"]',
                 '[aria-label*="Ask"]', '[aria-label*="prompt"]', '[aria-label*="message"]')
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                return loc.last
        except Exception:
            continue
    return None


def open_attach_menu(page):
    opener = None
    for name in ATTACH_OPENERS:
        loc = page.get_by_role("button", name=name, exact=False).first
        if visible(loc):
            opener = loc
            break
    if opener is None:
        for sel in ['button[aria-label*="Add context"]', 'button[aria-label*="Attach"]']:
            loc = page.locator(sel).first
            if visible(loc, 1500):
                opener = loc
                break
    if opener is None:
        return False
    opener.click()
    page.wait_for_timeout(800)
    return True


def attach_image(page, image_path):
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    if not open_attach_menu(page):
        print("[!] could not open the attach menu")
        return False
    page.set_input_files("input[type=file]", str(image_path), timeout=10000)
    page.wait_for_timeout(1500)
    return True


def send_message(page, text):
    textbox = find_textbox(page)
    if textbox is None:
        print("[!] message input not found")
        return False
    textbox.click()
    try:
        textbox.fill(text)
    except Exception:
        textbox.click()
        page.keyboard.type(text, delay=15)
    sent = False
    for name in ["Send message", "Send", "Send prompt"]:
        btn = page.get_by_role("button", name=name, exact=False).first
        if visible(btn, 1500):
            btn.click()
            sent = True
            break
    if not sent:
        page.keyboard.press("Enter")
    return True


def extract_last_assistant_reply(page, timeout=90):
    """Wait for the newest Copilot message to finish streaming, return its text."""
    selectors = ('[data-content="assistant-message"]',
                 '[data-testid="assistant-message"]',
                 '.assistant-message')
    last_text, stable_since = "", time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = ""
        for sel in selectors:
            try:
                loc = page.locator(sel)
                n = loc.count()
                if n:
                    text = loc.nth(n - 1).inner_text()
                    break
            except Exception:
                continue
        if text and text != last_text:
            last_text, stable_since = text, time.time()
        elif text and text == last_text and time.time() - stable_since > 3:
            return text
        page.wait_for_timeout(1000)
    return last_text


def quoted(s):
    m = re.search(r"""['"]([^'"]*)['"]""", s)
    return m.group(1) if m else s


def parse_command(reply):
    for line in reversed(reply.strip().splitlines()):
        line = line.strip().lstrip("*-`# ")
        if re.match(r"^(click|fill|type|wait|scroll|goto|done)\b", line, re.I):
            return line
    return None


def find_input(page, hint):
    sels = [f'input[placeholder*="{hint}"]', f'textarea[placeholder*="{hint}"]',
            f'[aria-label*="{hint}"]', "input", "textarea"]
    for sel in sels:
        try:
            loc = page.locator(sel).first
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def execute_command(page, line):
    """Run one Copilot command line. Returns False when the task is done."""
    m = re.match(r"^(\w+)\b(.*)$", line.strip(), re.S)
    cmd, rest = m.group(1).lower(), m.group(2).strip()

    if cmd == "click":
        text = quoted(rest)
        try:
            page.get_by_role("button", name=text, exact=False).first.click(timeout=8000)
        except Exception:
            page.get_by_text(text, exact=False).first.click(timeout=8000)
        print(f"[exec] click '{text}'")
    elif cmd == "fill":
        fm = re.match(r"""['"]([^'"]*)['"]\s+with\s+['"]([^'"]*)['"]""", rest, re.I)
        if not fm:
            print(f"[!] bad fill command: {line}")
            return True
        field, value = fm.group(1), fm.group(2)
        loc = find_input(page, field)
        if loc is None:
            print(f"[!] no input found for '{field}'")
            return True
        try:
            loc.fill(value)
        except Exception:
            loc.click()
            page.keyboard.type(value, delay=15)
        print(f"[exec] fill '{field}' with '{value}'")
    elif cmd == "type":
        page.keyboard.type(quoted(rest), delay=20)
        print(f"[exec] type '{quoted(rest)}'")
    elif cmd == "wait":
        m2 = re.search(r"(\d+(?:\.\d+)?)", rest)
        secs = float(m2.group(1)) if m2 else 2.0
        print(f"[exec] wait {secs}s")
        page.wait_for_timeout(int(secs * 1000))
    elif cmd == "scroll":
        page.mouse.wheel(0, 800 if "up" not in rest else -800)
        print(f"[exec] scroll {rest}")
    elif cmd == "goto":
        page.goto(quoted(rest))
        page.wait_for_load_state("domcontentloaded")
        print(f"[exec] goto {quoted(rest)}")
    elif cmd == "done":
        print("[exec] done — task complete")
        return False
    else:
        print(f"[!] unknown command: {line}")
    return True


def load_firefox_cookies():
    candidates = []
    for base in (Path.home() / ".mozilla" / "firefox",
                 Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"):
        if base.is_dir():
            candidates += sorted(base.glob("*/cookies.sqlite"))
    if not candidates:
        return None
    db = max(candidates, key=lambda p: p.stat().st_mtime)
    tmp = Path(tempfile.mkdtemp()) / "cookies.sqlite"
    shutil.copy2(db, tmp)
    for suffix in ("-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp) + suffix))
    con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
    rows = con.execute(
        "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
        "FROM moz_cookies WHERE host LIKE '%microsoft%' OR host LIKE '%live.com'"
    ).fetchall()
    con.close()
    cookies = []
    for name, value, host, path, expiry, isSecure, isHttpOnly in rows:
        if not value:
            continue
        cookies.append({"name": name, "value": value,
                        "domain": host if host.startswith(".") else "." + host,
                        "path": path or "/",
                        "expires": int(expiry) if expiry and expiry > 0 else -1,
                        "secure": bool(isSecure), "httpOnly": bool(isHttpOnly)})
    return cookies or None


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Drive the browser via Copilot's vision + commands")
    ap.add_argument("task", help="the task/goal for Copilot")
    ap.add_argument("--image", default=None, help="image for round 1 (default: fresh screenshot)")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS, help="max loop rounds")
    ap.add_argument("--no-cookies", action="store_true", help="skip real-Firefox cookie import")
    args = ap.parse_args()

    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=False,
            viewport={"width": 1400, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)

        if not args.no_cookies:
            cookies = load_firefox_cookies()
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                    print(f"[i] imported {len(cookies)} session cookies")
                except Exception as e:
                    print(f"[!] cookie import failed: {e}")
        page.goto(URL)
        page.wait_for_load_state("domcontentloaded")

        for name in ["Sign in", "Sign in to continue"]:
            if visible(page.get_by_role("button", name=name, exact=False).first, 2500):
                print("[!] sign-in page detected — sign in on your normal Firefox and re-run")
                ctx.close()
                return
        print("[i] signed-in session OK")

        for step in range(1, args.steps + 1):
            print(f"\n=== round {step}/{args.steps} ===")
            if step == 1 and args.image:
                shot = Path(args.image).resolve()
            else:
                shot = SHOTS / f"round_{step}.png"
                page.screenshot(path=str(shot))
            print(f"[i] screenshot: {shot}")

            if not attach_image(page, shot):
                ctx.close()
                return
            prompt = INSTRUCTION.format(task=args.task)
            if not send_message(page, prompt):
                ctx.close()
                return
            print("[i] waiting for Copilot's next action...")

            reply = extract_last_assistant_reply(page, timeout=90)
            print(f"[copilot] {reply!r}")

            cmd = parse_command(reply)
            if cmd is None:
                print("[!] no recognizable command in reply — stopping")
                break
            print(f"[cmd] {cmd}")
            if not execute_command(page, cmd):
                break
            page.wait_for_timeout(1500)

        print("\n[done] loop finished — check the browser window / screenshots/")


if __name__ == "__main__":
    main()
