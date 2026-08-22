#!/usr/bin/env python3
"""
ai_driver.py — multi-AI vision driver: Copilot -> ChatGPT -> DeepSeek fallback.

Two tabs:
  - TAB A (task): the web page you are automating (opens --url). Screenshots
    are taken here and commands (click/fill/...) are executed here.
  - TAB B (ai): the chat tab. Each round the current screenshot is attached
    and ONE provider is asked; if its reply is empty/unparseable, the next
    provider is tried automatically.

Provider order (configurable): copilot -> chatgpt -> deepseek
  - deepseek needs its "vision" tab switched on before attaching an image
    (handled automatically).

USAGE:
  .venv/bin/python3 ai_driver.py --url "https://survey-site.com" \
      "i want to make money with this web by doing a survey, what do i do next?"
  .venv/bin/python3 ai_driver.py --url "..." --image first_shot.png "task"
  .venv/bin/python3 ai_driver.py --url "..." --providers chatgpt,deepseek "task"
  .venv/bin/python3 ai_driver.py --url "..." --steps 10 "task"

Cookies for all three providers are imported from your real Firefox (be
signed in there: copilot.microsoft.com, chatgpt.com, chat.deepseek.com).
"""

import argparse
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
SHOTS = BASE / "screenshots"
SHOTS.mkdir(exist_ok=True)
PROFILE = BASE / "pw-profile"

DEFAULT_STEPS = 5
DEFAULT_PROVIDERS = ["copilot", "chatgpt", "deepseek"]

INSTRUCTION = """You are controlling my browser via Playwright. I just attached a screenshot of the current page.

Reply with ONLY ONE line, one of these formats:
click 'button text'
fill 'field' with 'value'
type 'text'
wait '3'
scroll 'down' or 'up'
goto 'https://url'
done

Choose the single next best action for: {task}
When the task is fully done, reply exactly: done"""

ATTACH_OPENERS = ["Add context", "Attach", "Attach files", "Attach media"]

REPLY_SELECTORS = {
    "copilot": ('[data-content="assistant-message"]',),
    "chatgpt": ('[data-message-author-role="assistant"]',),
    "deepseek": ('.ds-markdown',),
}
SIGNIN_MARKERS = ["Log in", "Sign in", "登录"]


# ---------------------------------------------------------------- generic helpers
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


def quoted(s):
    m = re.search(r"""['"]([^'"]*)['"]""", s)
    return m.group(1) if m else s


def parse_command(reply, exclude=""):
    for line in reversed(reply.strip().splitlines()):
        line = line.strip().lstrip("*-`# ")
        if not line:
            continue
        if exclude and line in exclude:
            continue
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
    """Microsoft + OpenAI + DeepSeek cookies from the user's real Firefox."""
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
        "FROM moz_cookies WHERE host LIKE '%microsoft%' OR host LIKE '%live.com' "
        "OR host LIKE '%openai%' OR host LIKE '%deepseek%'"
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


# ---------------------------------------------------------------- provider actions
def extract_reply(page, provider, timeout=120):
    selectors = REPLY_SELECTORS.get(provider, ())
    last_text, stable_since, started = "", time.time(), False
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
        if not text:  # generic catch-all
            for sel in ('[data-content]', '[data-message-author-role="assistant"]', '.ds-markdown'):
                try:
                    loc = page.locator(sel)
                    if loc.count():
                        text = loc.nth(loc.count() - 1).inner_text()
                        break
                except Exception:
                    continue
        if text != last_text:
            if text:
                started = True
            last_text, stable_since = text, time.time()
        elif started and text and time.time() - stable_since > 4:
            return text
        page.wait_for_timeout(1000)
    return last_text


def check_signin(page, provider):
    for name in SIGNIN_MARKERS:
        if visible(page.get_by_role("button", name=name, exact=False).first, 2000):
            print(f"[!] {provider}: sign-in page detected — sign in on your normal Firefox and re-run")
            return True
    return False


def send_message(page, provider, text):
    textbox = None
    if provider == "chatgpt":
        try:
            loc = page.locator("#prompt-textarea")
            if loc.count():
                textbox = loc.first
        except Exception:
            pass
    if textbox is None:
        textbox = find_textbox(page)
    if textbox is None:
        print(f"[!] {provider}: message input not found")
        return False
    textbox.click()
    try:
        textbox.fill(text)
    except Exception:
        textbox.click()
        page.keyboard.type(text, delay=15)

    sent = False
    if provider == "chatgpt":
        for sel in ('[data-testid="send-button"]', 'button[aria-label="Send prompt"]'):
            try:
                loc = page.locator(sel).first
                if visible(loc, 1500):
                    loc.click()
                    sent = True
                    break
            except Exception:
                continue
    if not sent:
        for name in ["Send message", "Send", "Send prompt"]:
            btn = page.get_by_role("button", name=name, exact=False).first
            if visible(btn, 1500):
                btn.click()
                sent = True
                break
    if not sent:
        page.keyboard.press("Enter")
    return True


def attach_image(page, provider, image):
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    if provider == "copilot":
        opener = None
        for name in ATTACH_OPENERS:
            loc = page.get_by_role("button", name=name, exact=False).first
            if visible(loc):
                opener = loc
                break
        if opener is None:
            for sel in ('button[aria-label*="Add context"]', 'button[aria-label*="Attach"]'):
                loc = page.locator(sel).first
                if visible(loc, 1500):
                    opener = loc
                    break
        if opener is None:
            print("[!] copilot: attach button not found")
            return False
        opener.click()
        page.wait_for_timeout(800)
    elif provider == "chatgpt":
        clicked = False
        for sel in ('button[aria-label="Attach files"]', 'button[aria-label*="Attach"]',
                    'input[type="file"]'):
            loc = page.locator(sel).first
            try:
                if loc.count() and sel.startswith("input"):
                    pass  # handled by set_input_files below
                elif visible(loc, 1500):
                    loc.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            print("[!] chatgpt: attach button not found (paperclip)")
            return False
        page.wait_for_timeout(800)
    elif provider == "deepseek":
        # deepseek: switch the vision tab on BEFORE attaching
        for name in ["Vision", "vision", "图片", "视觉"]:
            try:
                loc = page.get_by_text(name, exact=False).first
                if visible(loc, 1200):
                    loc.click()
                    print(f"[i] deepseek: switched vision tab ('{name}')")
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
        clicked = False
        for sel in ('button[aria-label*="Upload"]', 'button[aria-label*="attach"]',
                    'button[aria-label*="Attach"]', 'input[type="file"]'):
            loc = page.locator(sel).first
            try:
                if visible(loc, 1500):
                    loc.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            print("[!] deepseek: upload/attach button not found")
            return False
        page.wait_for_timeout(800)

    try:
        page.set_input_files("input[type=file]", str(image), timeout=10000)
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        print(f"[!] {provider}: set_input_files failed: {e}")
        return False


def ask_provider(page, provider, image, task, prompt):
    """One round on one provider. Returns reply text or None."""
    urls = {"copilot": "https://copilot.microsoft.com/",
            "chatgpt": "https://chatgpt.com/",
            "deepseek": "https://chat.deepseek.com/"}
    print(f"\n[ai] asking {provider} ...")
    try:
        page.goto(urls[provider])
        page.wait_for_load_state("domcontentloaded")
    except Exception as e:
        print(f"[!] {provider}: navigation failed: {e}")
        return None
    page.wait_for_timeout(1500)
    if check_signin(page, provider):
        return None
    if not attach_image(page, provider, image):
        return None
    if not send_message(page, provider, prompt):
        return None
    print(f"[i] waiting for {provider} reply...")
    return extract_reply(page, provider)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Drive the browser via Copilot/ChatGPT/DeepSeek vision")
    ap.add_argument("task", help="the task/goal for the AI (plain words)")
    ap.add_argument("--url", default=None, help="task page to open in the automation tab")
    ap.add_argument("--image", default=None, help="image for round 1 (default: screenshot of task tab)")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS, help="max loop rounds")
    ap.add_argument("--providers", default=",".join(DEFAULT_PROVIDERS),
                    help="fallback order, comma separated")
    ap.add_argument("--no-cookies", action="store_true", help="skip real-Firefox cookie import")
    args = ap.parse_args()

    if re.match(r"^\s*(task\s*:|i am automating this browser)", args.task, re.I):
        print("[!] you pasted the instruction template as the task!")
        print("    pass your REAL goal, e.g.:")
        print('    .venv/bin/python3 ai_driver.py --url "https://survey-site.com" "i want to make money with this web by doing a survey"')
        return

    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    prompt = INSTRUCTION.format(task=args.task)

    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=False,
            viewport={"width": 1400, "height": 900})

        if not args.no_cookies:
            cookies = load_firefox_cookies()
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                    print(f"[i] imported {len(cookies)} session cookies (microsoft/openai/deepseek)")
                except Exception as e:
                    print(f"[!] cookie import failed: {e}")
            else:
                print("[!] no cookies found — sign in to the providers in your normal Firefox first")

        ai_page = ctx.pages[0] if ctx.pages else ctx.new_page()
        task_page = ctx.new_page()
        if args.url:
            task_page.goto(args.url)
            task_page.wait_for_load_state("domcontentloaded")
            print(f"[i] task page opened: {args.url}")
        else:
            print("[i] no --url given — commands that need the task page may fail;")
            print("    use --url to open the page you want automated")

        for step in range(1, args.steps + 1):
            print(f"\n=== round {step}/{args.steps} ===")
            if step == 1 and args.image:
                shot = Path(args.image).resolve()
            else:
                shot = SHOTS / f"round_{step}.png"
                task_page.bring_to_front()
                task_page.screenshot(path=str(shot))
            print(f"[i] screenshot: {shot}")

            done_round = False
            for provider in providers:
                reply = ask_provider(ai_page, provider, shot, args.task, prompt)
                if reply and reply.strip():
                    print(f"[{provider}] {reply!r}")
                    cmd = parse_command(reply, exclude=prompt)
                    if cmd:
                        print(f"[cmd] {cmd}")
                        if not execute_command(task_page, cmd):
                            print("\n[done] task complete")
                            ctx.close()
                            return
                        done_round = True
                        break
                    else:
                        print(f"[!] {provider} replied but no command found — trying next provider")
                else:
                    print(f"[!] {provider} returned nothing — trying next provider")
            if not done_round:
                shot_dbg = SHOTS / f"debug_round_{step}.png"
                ai_page.screenshot(path=str(shot_dbg))
                print(f"[!] all providers failed this round — debug: {shot_dbg}")
                break
            task_page.wait_for_timeout(1500)

        print("\n[done] loop finished — check the browser / screenshots/")


if __name__ == "__main__":
    main()
