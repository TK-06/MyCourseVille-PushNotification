"""Minimal MyCourseVille client: log in once, then replay the session headlessly.

Derived from the `mcv` Claude Code skill's mcv.py, trimmed to just what the watcher
needs — login, session validation, and reading the dashboard and course pages. The
skill's download/submit/TA features are not included.

    python mcvclient.py login     # opens a real browser; you clear MFA yourself
    python mcvclient.py courses   # list courses using the saved session

Everything that matters lives in the DATA DIRECTORY, never beside this file:

    config.json        your CU username and password, in plaintext
    auth_state.json    a live logged-in session (as good as the password)
    snapshots/         crawl history

Resolution order for that directory:
    1. $MCV_DATA_DIR
    2. ~/.mcvpushnoti          if it exists
    3. ~/.claude/skills/mcv    if it holds an auth_state.json (the skill's own location)
    4. ~/.mcvpushnoti          created on demand

Keep it off any synced drive. A OneDrive/Dropbox folder uploads your university
password to someone else's computer.
"""

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# Windows consoles default to cp1252; force UTF-8 so arrows and check marks don't crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _find_data_dir() -> Path:
    if os.environ.get("MCV_DATA_DIR"):
        return Path(os.environ["MCV_DATA_DIR"]).expanduser()
    home = Path.home() / ".mcvpushnoti"
    if home.exists():
        return home
    skill = Path.home() / ".claude" / "skills" / "mcv"
    if (skill / "auth_state.json").exists():
        return skill
    return home


DATA_DIR = _find_data_dir()
AUTH_FILE = DATA_DIR / "auth_state.json"
CONFIG_FILE = DATA_DIR / "config.json"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LATEST_FILE = SNAPSHOTS_DIR / "latest.json"

MCV_HOME = "https://www.mycourseville.com/?type=course&role=all"
CHULA_LOGIN_URL = (
    "https://www.mycourseville.com/api/oauth/authorize"
    "?response_type=code&client_id=mycourseville.com"
    "&redirect_uri=https://www.mycourseville.com&login_page=itchula"
)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(
            f"Missing {CONFIG_FILE}.\n"
            'Create it with: {"username": "<CU id>", "password": "<password>"}'
        )
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def ensure_session(page) -> bool:
    """True iff the dashboard loaded with at least one real course tile.

    MCV renders its expired-session page at the same `?type=course&role=all` URL with no
    redirect, so checking the URL is a false positive. Count numeric-id course anchors
    instead: the login form has zero, the dashboard has at least one.
    """
    page.goto(MCV_HOME, wait_until="networkidle")
    if "oauth" in page.url or "login" in page.url.lower():
        return False
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href*="/course/"]'))
                .filter(a => /\\/course\\/\\d+/.test(a.href)).length"""
    ) > 0


def scrape_courses(page) -> list[dict]:
    """Course tiles from the dashboard."""
    page.goto(MCV_HOME, wait_until="networkidle")
    return page.evaluate(
        """() => {
            const seen = new Set();
            const out = [];
            // Real courses have numeric ids: /course/12345 — nav items use words.
            document.querySelectorAll('a[href*="/course/"]').forEach(a => {
                const href = a.href;
                if (!/\\/course\\/\\d+/.test(href)) return;
                if (seen.has(href)) return;
                seen.add(href);
                const text = (a.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text) out.push({ text: text.slice(0, 200), href });
            });
            return out;
        }"""
    )


def scrape_course_detail(page, href: str) -> dict:
    """Announcements and materials visible on a single course page."""
    try:
        page.goto(href, wait_until="networkidle", timeout=30_000)
    except PWTimeout:
        return {"href": href, "error": "load timeout"}
    return page.evaluate(
        """() => {
            const text = (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim();
            const links = Array.from(document.querySelectorAll('a')).map(a => ({
                text: text(a).slice(0, 160),
                href: a.href
            })).filter(l => l.text);
            return { title: document.title, link_count: links.length, links: links.slice(0, 200) };
        }"""
    )


def login() -> None:
    """Open a visible browser, drive the Chula IT login, save the session."""
    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("-> navigating to Chula IT login")
        page.goto(CHULA_LOGIN_URL, wait_until="domcontentloaded")

        try:
            # Some sessions land on a 4-option login-method chooser instead of the
            # credential form. Click the CU-account option to reach the real form.
            if page.locator('input[type="password"]').count() == 0:
                chooser = page.get_by_text("log in with", exact=False).first
                if chooser.count() > 0:
                    print("-> chooser page detected, clicking CU account login")
                    chooser.click()
                    page.wait_for_load_state("networkidle", timeout=15_000)

            page.wait_for_selector(
                'input[type="text"], input[name="username"], input[id*="user" i]', timeout=8000
            )
            user_input = page.locator(
                'input[name="username"], input[id*="user" i], input[type="text"]'
            ).first
            pass_input = page.locator('input[type="password"]').first
            user_input.fill(cfg["username"])
            pass_input.fill(cfg["password"])
            print("-> credentials filled; submit in the browser if it doesn't auto-submit")
            # The real Chula IT button is Thai-labelled with no type="submit", so it needs
            # its id selector — the generic ones below silently miss it.
            for sel in ['#cv-login-cvecologinbutton', 'button[type="submit"]',
                        'input[type="submit"]', 'button:has-text("Sign")',
                        'button:has-text("Log")']:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    btn.click()
                    break
            else:
                pass_input.press("Enter")
        except PWTimeout:
            print("-> couldn't auto-locate login fields; please log in manually")

        print("\nWaiting for login to complete. Finish any MFA in the browser. Up to 5 min.")

        # Don't trust the URL: the Chula IT login page never leaves mycourseville.com, so a
        # URL wait passes instantly whether or not login happened. Poll the real session
        # check instead — but in a SEPARATE headless browser. Opening a second page in the
        # visible context spawns a tab Chrome auto-focuses, yanking you off the login form.
        checker = p.chromium.launch(headless=True)
        deadline = time.time() + 300
        authenticated = False
        ticks = 0
        while time.time() < deadline:
            try:
                check_ctx = checker.new_context(storage_state=ctx.storage_state())
                if ensure_session(check_ctx.new_page()):
                    authenticated = True
                    check_ctx.close()
                    break
                check_ctx.close()
            except PWTimeout:
                pass
            time.sleep(5)
            ticks += 1
            if ticks % 6 == 0:
                print(f"  ...still waiting ({ticks * 5}s)")
        checker.close()

        if authenticated:
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(AUTH_FILE))
            print(f"\nOK - saved session to {AUTH_FILE}")
        else:
            print("FAILED - timed out waiting for login. Nothing saved.")
        browser.close()


def courses() -> None:
    """List courses using the saved session."""
    if not AUTH_FILE.exists():
        sys.exit(f"No {AUTH_FILE}. Run: python mcvclient.py login")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(storage_state=str(AUTH_FILE)).new_page()
            if not ensure_session(page):
                sys.exit("Session expired - run: python mcvclient.py login")
            for c in scrape_courses(page):
                print(f"  {c['text'][:70]}")
                print(f"    {c['href']}")
        finally:
            browser.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "courses"
    if cmd == "login":
        login()
    elif cmd == "courses":
        courses()
    elif cmd == "where":
        print(f"data dir : {DATA_DIR}")
        print(f"config   : {'present' if CONFIG_FILE.exists() else 'MISSING'}")
        print(f"session  : {'present' if AUTH_FILE.exists() else 'MISSING'}")
    else:
        sys.exit(f"Unknown command: {cmd}. Try: login | courses | where")
