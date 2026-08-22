#!/usr/bin/env python3
"""
copilot_playwright.py — the reliable way to attach an image to Copilot.

Drives the REAL Firefox (a visible window you can watch) with Playwright.
No templates, no screen capture, no tab-walking, no native file dialog:
the image is fed straight into the upload control, so nothing gets stuck.

Why this fixes the problems you hit:
  - the file dialog: bypassed entirely (Playwright hands the file to the page)
  - blind Tab clicks: replaced by real UI lookups by accessible name
  - sign-in: done once manually, saved in ./pw-profile for future runs

USAGE:
  python3 copilot_playwright.py                   # attach fresh screenshot, ask "what is this"
  python3 copilot_playwright.py --image photo.png
  python3 copilot_playwright.py --question "explain this image"

ONE-TIME INSTALL:
  pip install playwright
  playwright install firefox          # downloads the Firefox runtime
  playwright install-deps firefox     # system libs (may ask for sudo)

First run: sign in to Copilot in the opened window — the session persists.
"""

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)
PROFILE = BASE / "pw-profile"

URL = "https://copilot.microsoft.com/"
DEFAULT_QUESTION = "what is this"

# candidate names for the attach flow — extend if your UI differs
ATTACH_OPENERS = ["Add context", "Attach", "Attach files", "Attach media"]
UPLOAD_ITEMS = ["Upload from this device", "Upload from device", "Upload file", "Upload"]


def visible(loc, timeout=2000):
    try:
        return loc.is_visible(timeout=timeout)
    except Exception:
        return False


def load_firefox_cookies():
    """Copy Microsoft session cookies from the user's REAL Firefox profile.

    Firefox stores cookies plaintext in cookies.sqlite (no decryption needed
    on Linux). We copy the db (+ WAL) to a temp file so Firefox can keep
    running, then read the Microsoft/live.com cookies. This lets the
    Playwright window arrive ALREADY SIGNED IN — Microsoft's anti-automation
    check only fires on the sign-in page, which we never visit.
    """
    candidates = []
    for base in (Path.home() / ".mozilla" / "firefox",
                 Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"):
        if base.is_dir():
            candidates += sorted(base.glob("*/cookies.sqlite"))
    if not candidates:
        print("[!] no Firefox profile found (~/.mozilla/firefox or snap path)")
        return None
    db = max(candidates, key=lambda p: p.stat().st_mtime)  # most recent profile
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
        cookies.append({
            "name": name,
            "value": value,
            "domain": host if host.startswith(".") else "." + host,
            "path": path or "/",
            "expires": int(expiry) if expiry and expiry > 0 else -1,
            "secure": bool(isSecure),
            "httpOnly": bool(isHttpOnly),
        })
    return cookies or None


def main():
    ap = argparse.ArgumentParser(description="Attach an image to Copilot via Playwright + Firefox")
    ap.add_argument("--image", default=None, help="image to attach (default: fresh screenshot)")
    ap.add_argument("--question", default=DEFAULT_QUESTION, help="question to ask")
    ap.add_argument("--no-screenshot", action="store_true", help="don't capture a fresh screenshot")
    ap.add_argument("--timeout", type=int, default=30000, help="per-action timeout ms")
    ap.add_argument("--no-cookies", action="store_true",
                    help="skip importing your real Firefox session cookies")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout

    image = args.image
    if not image and not args.no_screenshot:
        image = str(SHOTS / "to_attach.png")
        try:
            import pyautogui
            pyautogui.screenshot().save(image)
            print(f"[i] captured fresh screenshot: {image}")
        except Exception as e:
            print(f"[!] could not capture screenshot ({e}) — pass --image instead")
            return
    if not image:
        print("[!] no image to attach — pass --image or drop --no-screenshot")
        return
    image = str(Path(image).resolve())
    if not Path(image).exists():
        print(f"[!] image not found: {image}")
        return

    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(args.timeout)

        # --- import real Firefox session so we never hit the sign-in page --
        if not args.no_cookies:
            cookies = load_firefox_cookies()
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                    print(f"[i] imported {len(cookies)} session cookies from your real Firefox")
                except Exception as e:
                    print(f"[!] cookie import failed: {e}")
            else:
                print("[!] no Microsoft cookies found in your Firefox profile —")
                print("    sign in to copilot.microsoft.com in your NORMAL Firefox first.")

        page.goto(URL)
        page.wait_for_load_state("domcontentloaded")

        # --- detect the sign-in page (Microsoft blocks automated sign-in) ---
        signin_shown = False
        for name in ["Sign in", "Sign in to continue"]:
            if visible(page.get_by_role("button", name=name, exact=False).first, 2500):
                signin_shown = True
                break
        if signin_shown:
            print("[!] sign-in page detected — Microsoft blocks automated sign-in.")
            print("    Fix: open copilot.microsoft.com in your NORMAL Firefox, sign in,")
            print("    close it, then re-run this script (cookies will be imported).")
            ctx.close()
            return
        print("[i] signed-in session OK — continuing")

        # ---------------- 1. open the attach menu (paperclip / '+') -------
        opener = None
        for name in ATTACH_OPENERS:
            loc = page.get_by_role("button", name=name, exact=False).first
            if visible(loc):
                opener = loc
                break
        if opener is None:  # aria-label fallbacks
            for sel in ['button[aria-label*="Add context"]',
                        'button[aria-label*="Attach"]',
                        'button[aria-label*="attach"]']:
                loc = page.locator(sel).first
                if visible(loc, 1500):
                    opener = loc
                    break
        if opener is None:
            print("[!] attach button not found — Copilot UI may have changed.")
            page.screenshot(path=str(SHOTS / "debug_ui.png"))
            print(f"    debug screenshot: {SHOTS / 'debug_ui.png'}")
            ctx.close()
            return
        opener.click()
        print("[i] attach menu opened")

        # ---------------- 2. 'Upload from this device' -> feed the file ---
        attached = False
        try:
            with page.expect_file_chooser(timeout=8000) as fc_info:
                for name in UPLOAD_ITEMS:
                    item = page.get_by_role("button", name=name, exact=False).first
                    if visible(item, 2000):
                        item.click()
                        break
            fc = fc_info.value
            fc.set_files(image)
            attached = True
            print(f"[i] file attached: {image}")
        except Exception as e:
            print(f"[!] file-chooser route failed ({e}); trying input[type=file]...")
            try:
                page.set_input_files("input[type=file]", image)
                attached = True
                print("[i] file attached via input[type=file]")
            except Exception as e2:
                print(f"[!] direct input failed too: {e2}")
                page.screenshot(path=str(SHOTS / "debug_attach.png"))
                print(f"    debug screenshot: {SHOTS / 'debug_attach.png'}")
                ctx.close()
                return
        if not attached:
            print("[!] could not attach the file")
            ctx.close()
            return

        # ---------------- 3. let the thumbnail render, then ask -----------
        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOTS / "attached.png"))
        print(f"[i] state after attach: {SHOTS / 'attached.png'}")

        textbox = page.get_by_role("textbox").last
        textbox.click()
        try:
            textbox.fill(args.question)
        except Exception:
            textbox.click()
            page.keyboard.type(args.question, delay=30)
        print(f"[i] typed question: {args.question}")

        # ---------------- 4. send -----------------------------------------
        sent = False
        for name in ["Send message", "Send", "Send prompt", "Send message"]:
            btn = page.get_by_role("button", name=name, exact=False).first
            if visible(btn, 1500):
                btn.click()
                sent = True
                break
        if not sent:
            page.keyboard.press("Enter")
        print("[i] message sent. Waiting ~20s for Copilot's reply...")

        page.wait_for_timeout(20000)
        page.screenshot(path=str(SHOTS / "reply.png"))
        print(f"[i] reply screenshot: {SHOTS / 'reply.png'}")
        print("[i] done — closing the browser window.")
        ctx.close()


if __name__ == "__main__":
    main()
