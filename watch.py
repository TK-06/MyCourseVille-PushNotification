"""MyCourseVille watcher — headless poll, diff, push to Telegram.

Usage:
    python watch.py                 # the scheduled run: snapshot, then push the open-assignment
                                    #   board if it reads differently than last time
    python watch.py --force         # same, but ignore the active-hours window
    python watch.py check           # scrape now and message either way; leaves the baseline alone
    python watch.py due             # list every assignment still open, soonest first
    python watch.py courses         # show course codes and seed courses.json short names
    python watch.py chatid          # resolve your Telegram chat id (message the bot first)
    python watch.py test            # send a test message, touch nothing else
    python watch.py status          # print config/state without contacting MCV or Telegram
    python watch.py listen          # long-poll Telegram for /due, /check, /status (runs forever)

This file keeps NO secrets beside itself, so the project folder is safe to sync or
publish. Credentials, the live session and snapshots live in mcvclient.DATA_DIR.
See README.md.

Exits 0 silently when there is nothing to say — that is the common case.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent


import mcvclient as mcv
from playwright.sync_api import sync_playwright

# Credentials, session and snapshots all live in mcv.DATA_DIR, never beside this file.
DATA_DIR = mcv.DATA_DIR

# Holds the Telegram bot token, so it lives in the data dir with the other secrets.
NOTIFY_CONFIG = DATA_DIR / "notify_config.json"
# Harmless bookkeeping can live with the code.
STATE_FILE = PROJECT_ROOT / "notify_state.json"
LOG_FILE = PROJECT_ROOT / "watch.log"

TELEGRAM_LIMIT = 4096
# Don't re-nag about a dead session every single run.
SESSION_ALERT_COOLDOWN = timedelta(hours=6)

LOCK_FILE = PROJECT_ROOT / "scrape.lock"
# A full crawl is ~2 minutes. Anything older than this is a crashed run, not a live one.
LOCK_STALE = timedelta(minutes=15)


class Busy(RuntimeError):
    """Another scrape holds the lock."""


def acquire_lock() -> bool:
    """One crawl at a time.

    The listener and the scheduled task are independent processes; without this
    they can launch two headless Chromiums at once, which is a lot of memory to
    ask of a 16 GB laptop that might also be gaming.
    """
    now = datetime.now()
    if LOCK_FILE.exists():
        try:
            held = datetime.fromisoformat(LOCK_FILE.read_text(encoding="utf-8").strip())
            if now - held < LOCK_STALE:
                return False
        except (ValueError, OSError):
            pass  # unreadable or corrupt: treat as stale and take it
    try:
        LOCK_FILE.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        return True  # can't write a lock; don't let that block the actual work
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass

# Course pages are mostly chrome. These are the links that are always there and
# never mean "something happened". Everything not matched here is worth telling
# you about — a false ping is cheap, a missed deadline is not.
NAV_TEXT = {
    "home", "dashboard", "log out", "logout", "sign out", "profile", "settings",
    "notification", "notifications", "calendar", "help", "support", "english",
    "ไทย", "thai", "my courses", "courses", "course admin", "back", "menu",
    "search", "about", "contact", "privacy", "terms", "edit", "more",
}
NAV_HREF = re.compile(
    r"""(?ix)
    ^(javascript:|mailto:|\#)            # dead links
    | /api/oauth/                        # login plumbing
    | /\?q=courseville/(notification|profile|calendar)\b
    | ^https?://(www\.)?mycourseville\.com/?$   # bare home
    """
)
# Query params that change per page load and would otherwise fake a diff every run.
VOLATILE_PARAMS = {"t", "ts", "_", "cachebust", "rand", "token", "csrf", "sid"}


# --------------------------------------------------------------------------- config


def load_notify_config() -> dict:
    if not NOTIFY_CONFIG.exists():
        sys.exit(
            f"Missing {NOTIFY_CONFIG}.\n"
            f"Copy notify_config.example.json there and fill in your bot token."
        )
    cfg = json.loads(NOTIFY_CONFIG.read_text(encoding="utf-8"))
    token = (cfg.get("telegram_bot_token") or "").strip()
    if not token:
        sys.exit(f"telegram_bot_token is empty in {NOTIFY_CONFIG}.")
    # A BotFather token is "<bot_id>:<35 chars>". Clipping a character while copying
    # is easy and otherwise surfaces as a bare 401 much later.
    if not re.fullmatch(r"\d{8,12}:[A-Za-z0-9_-]{35}", token):
        tail = token.split(":", 1)[-1] if ":" in token else "(no colon)"
        sys.exit(
            f"telegram_bot_token in {NOTIFY_CONFIG} isn't shaped like a BotFather token.\n"
            f"Expected <digits>:<35 chars>; the part after the colon is {len(tail)} chars.\n"
            "Re-copy it from BotFather — send /token there and tap the token to copy it whole."
        )
    cfg["telegram_bot_token"] = token
    return cfg


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- telegram


def telegram_call(token: str, method: str, params: dict | None = None, timeout: int = 30) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data or None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 401:
            raise RuntimeError(
                "Telegram rejected the bot token (401 Unauthorized).\n"
                "The token is wrong, mistyped, or was revoked. Send /token to BotFather\n"
                f"and paste the fresh one into {NOTIFY_CONFIG}."
            ) from e
        raise RuntimeError(f"Telegram {method} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram {method} unreachable: {e.reason}") from e
    except (TimeoutError, ConnectionError, json.JSONDecodeError) as e:
        # Read-phase timeouts (socket.timeout IS TimeoutError on 3.13, not a URLError),
        # dropped connections, and captive-portal HTML served as a 200 all land here
        # rather than in the handlers above. The long-poll listener treats RuntimeError
        # as "blip, retry" -- make sure these reach it as one instead of killing it.
        raise RuntimeError(f"Telegram {method} failed ({type(e).__name__}): {e}") from e


def send(cfg: dict, text: str) -> None:
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 40].rstrip() + "\n\n… (truncated)"
    res = telegram_call(
        cfg["telegram_bot_token"],
        "sendMessage",
        {
            "chat_id": cfg["telegram_chat_id"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    if not res.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {res}")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- diffing


def normalize(href: str) -> str:
    """Strip fragments and per-load params so the same link compares equal run to run."""
    try:
        parts = urllib.parse.urlsplit(href)
    except ValueError:
        return href
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in VOLATILE_PARAMS
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), "")
    )


def is_noise(link: dict) -> bool:
    text = (link.get("text") or "").strip()
    href = link.get("href") or ""
    if not text:
        return True
    if text.strip().lower() in NAV_TEXT:
        return True
    if NAV_HREF.search(href):
        return True
    return False


def link_keys(detail: dict) -> set[tuple[str, str]]:
    return {
        (normalize(l["href"]), (l.get("text") or "").strip())
        for l in detail.get("links", [])
        if not is_noise(l)
    }


def diff(prev: dict, curr: dict) -> dict:
    """Structured diff. Returns {'new_courses': [...], 'gone_courses': [...], 'per_course': {name: [labels]}}."""
    prev_courses = {c["href"]: c for c in prev.get("courses", [])}
    curr_courses = {c["href"]: c for c in curr.get("courses", [])}

    out = {
        "new_courses": [curr_courses[h]["text"] for h in curr_courses.keys() - prev_courses.keys()],
        "gone_courses": [prev_courses[h]["text"] for h in prev_courses.keys() - curr_courses.keys()],
        "per_course": {},
        "new_assignments": [],
        "changed_assignments": [],
    }

    # Assignments are the thing this whole project exists for, so they get their own
    # comparison rather than being lumped in with page links.
    prev_assign = prev.get("assignments", {})
    curr_assign = curr.get("assignments", {})
    for href, rows in curr_assign.items():
        # No baseline for this course (first crawl, or a snapshot from before
        # assignments were tracked) — reporting all of them would be noise.
        if href not in prev_assign:
            continue
        cname = curr_courses.get(href, {}).get("text", href)
        prev_by_id = {r["id"]: r for r in prev_assign[href]}
        for r in rows:
            was = prev_by_id.get(r["id"])
            if was is None:
                out["new_assignments"].append({**r, "course": cname})
            elif r.get("due_iso") != was.get("due_iso"):
                # A deadline that moves is the one change most likely to hurt.
                out["changed_assignments"].append(
                    {**r, "course": cname, "was_due_iso": was.get("due_iso")}
                )
    out["new_assignments"].sort(key=lambda r: r.get("due_iso") or "9999")
    out["changed_assignments"].sort(key=lambda r: r.get("due_iso") or "9999")

    prev_details = prev.get("details", {})
    curr_details = curr.get("details", {})
    for href, curr_d in curr_details.items():
        prev_d = prev_details.get(href)
        # A course we've never crawled has no baseline — everything would look "new".
        if not prev_d or "links" not in prev_d or "links" not in curr_d:
            continue
        added = link_keys(curr_d) - link_keys(prev_d)
        if not added:
            continue
        name = curr_courses.get(href, {}).get("text", href)
        out["per_course"][name] = sorted({text for _, text in added})
    return out


def has_changes(d: dict) -> bool:
    return bool(d["new_courses"] or d["gone_courses"] or d["per_course"]
                or d.get("new_assignments") or d.get("changed_assignments"))


def format_message(d: dict, title: str = "📚 <b>MyCourseVille update</b>") -> str:
    lines = [title]
    now = datetime.now()
    if d.get("new_assignments"):
        lines.append("\n📝 <b>New assignments</b>")
        for a in d["new_assignments"]:
            when = ""
            if a.get("due_iso"):
                due = datetime.fromisoformat(a["due_iso"])
                when = f" — due {due:%a %d %b %H:%M} ({fmt_delta(due, now)})"
            lines.append(f"  • <a href=\"{esc(a['href'])}\">{esc(a['title'][:80])}</a>{esc(when)}")
            lines.append(f"    <i>{esc(course_label(a['course']))}</i>")
    if d.get("changed_assignments"):
        lines.append("\n⏰ <b>Deadline changed</b>")
        for a in d["changed_assignments"]:
            old = "none"
            if a.get("was_due_iso"):
                old = f"{datetime.fromisoformat(a['was_due_iso']):%a %d %b %H:%M}"
            new = "none"
            if a.get("due_iso"):
                due = datetime.fromisoformat(a["due_iso"])
                new = f"{due:%a %d %b %H:%M} ({fmt_delta(due, now)})"
            lines.append(f"  • <a href=\"{esc(a['href'])}\">{esc(a['title'][:80])}</a>")
            lines.append(f"    <i>{esc(course_label(a['course']))}</i> · {esc(old)} → <b>{esc(new)}</b>")
    if d["new_courses"]:
        lines.append("\n🆕 <b>New courses</b>")
        lines += [f"  • {esc(c[:90])}" for c in sorted(d["new_courses"])]
        lines.append("  <i>added to courses.json - give them short names</i>")
    if d["gone_courses"]:
        lines.append("\n➖ <b>Courses gone</b>")
        lines += [f"  • {esc(c[:90])}" for c in sorted(d["gone_courses"])]
    for course, items in sorted(d["per_course"].items()):
        lines.append(f"\n📌 <b>{esc(course[:70])}</b>")
        for it in items[:12]:
            lines.append(f"  + {esc(it[:110])}")
        if len(items) > 12:
            lines.append(f"  … and {len(items) - 12} more")
    lines.append(f"\n<i>{datetime.now().strftime('%a %d %b, %H:%M')}</i>")
    lines.append("https://www.mycourseville.com/?type=course&role=all")
    return "\n".join(lines)


# --------------------------------------------------------------------------- assignments

# The assignment table renders its dates twice: once as a stacked Aug/27/2026 badge,
# once as prose. The prose form is the one worth parsing.
DUE_RE = re.compile(r"Due on (\d{1,2}) (\w+) (\d{4})(?: at (\d{1,2}):(\d{2}))?", re.I)
OUT_RE = re.compile(r"Out on (\d{1,2}) (\w+) (\d{4})", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], start=1)}


def parse_due(text: str) -> str | None:
    """'Due on 01 September 2026 at 23:59' -> ISO string. None if absent/unparseable."""
    m = DUE_RE.search(text or "")
    if not m:
        return None
    day, month_name, year, hh, mm = m.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), int(hh or 23), int(mm or 59)).isoformat(timespec="minutes")
    except ValueError:
        return None


def course_code(course_text: str) -> str:
    """'2190222.i (2026/1) Fundamental Data...' -> '2190222'."""
    return (course_text or "").split(".")[0].split()[0][:12] or "?"


COURSES_FILE = PROJECT_ROOT / "courses.json"
_course_names: dict[str, str] = {}
_course_names_mtime: float = -1.0


def load_course_names() -> dict[str, str]:
    """Course code -> the name you actually call it.

    Keyed on the course code (2190222), not MyCourseVille's internal id, because the
    internal id changes every term while the code doesn't — so the file you maintain
    keeps working next semester. Re-read when the file changes, since the listener is
    long-lived and you shouldn't have to restart it after an edit.
    """
    global _course_names, _course_names_mtime
    try:
        mtime = COURSES_FILE.stat().st_mtime
    except OSError:
        _course_names, _course_names_mtime = {}, -1.0
        return _course_names
    if mtime != _course_names_mtime:
        try:
            raw = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
            _course_names = {str(k): str(v) for k, v in raw.items() if v}
        except (json.JSONDecodeError, AttributeError, OSError) as e:
            log(f"courses.json unreadable ({type(e).__name__}) - falling back to course codes")
            _course_names = {}
        _course_names_mtime = mtime
    return _course_names


def sync_course_names(snap: dict) -> list[str]:
    """Append any newly-seen course code to courses.json, blank, on every crawl.

    Append-only by construction: existing names are never touched, and a file that
    won't parse is left completely alone rather than rewritten — overwriting it would
    silently destroy every name you'd typed. Writes only when there is genuinely
    something new, because load_course_names() caches on mtime and a pointless
    rewrite would bust that cache every half hour.

    Returns the codes added.
    """
    codes = [course_code(c["text"]) for c in snap.get("courses", [])]
    existing: dict = {}
    if COURSES_FILE.exists():
        try:
            existing = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log(f"courses.json unreadable ({type(e).__name__}) - leaving it alone")
            return []
        if not isinstance(existing, dict):
            log("courses.json is not a JSON object - leaving it alone")
            return []

    added = [c for c in codes if c and c != "?" and c not in existing]
    if not added:
        return []
    for c in added:
        existing[c] = ""
    try:
        COURSES_FILE.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as e:
        log(f"couldn't write courses.json: {e}")
        return []
    log(f"courses.json: added {', '.join(added)} (unnamed)")
    return added


def course_label(course_text: str) -> str:
    """The short name if courses.json has one, otherwise the bare course code."""
    code = course_code(course_text)
    return load_course_names().get(code, code)


COURSE_ID_RE = re.compile(r"/course/(\d+)")


def assignment_url(course_href: str) -> str | None:
    """Build the Assignments URL from the course id.

    Dashboard links look like `?q=courseville/course/84992&from=home`, so appending
    "/assignment" to the href would bury the path inside the `from` param and silently
    serve the course home page instead.
    """
    m = COURSE_ID_RE.search(course_href or "")
    if not m:
        return None
    return f"https://www.mycourseville.com/?q=courseville/course/{m.group(1)}/assignment"


def scrape_assignments(page, course_href: str) -> list[dict]:
    """Read the course's Assignments tab. This page is where assignments actually live —
    the course home page only links to it, which is why they were invisible before."""
    url = assignment_url(course_href)
    if not url:
        log(f"assignments: can't derive course id from {course_href}")
        return []
    try:
        page.goto(url, wait_until="networkidle", timeout=45_000)
    except Exception as e:
        log(f"assignments: load failed for {url}: {type(e).__name__}")
        return []
    # The table is populated by AJAX after networkidle fires.
    page.wait_for_timeout(3000)
    try:
        rows = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('table tr').forEach(tr => {
                    const a = tr.querySelector('a[href*="worksheet"]');
                    if (!a) return;
                    const tds = tr.querySelectorAll('td');
                    const txt = (el) => el ? (el.innerText || '').replace(/\\u00a0/g, ' ').trim() : '';
                    const titleCell = txt(tds[1]).split('\\n').map(s => s.trim()).filter(Boolean);
                    const m = a.href.match(/worksheet\\/\\d+\\/(\\d+)/);
                    out.push({
                        id: m ? m[1] : a.href,
                        title: titleCell[0] || '(untitled)',
                        note: titleCell.slice(1).join(' ').slice(0, 120),
                        out_raw: txt(tds[2]).replace(/\\s+/g, ' '),
                        due_raw: txt(tds[3]).replace(/\\s+/g, ' '),
                        href: a.href,
                    });
                });
                return out;
            }"""
        )
    except Exception as e:
        log(f"assignments: parse failed for {url}: {type(e).__name__}: {e}")
        return []
    for r in rows:
        r["due_iso"] = parse_due(r.get("due_raw", ""))
    return rows


def fmt_delta(target: datetime, now: datetime) -> str:
    """'in 2d 3h' / 'in 45m' / '3d overdue'."""
    secs = (target - now).total_seconds()
    overdue = secs < 0
    secs = abs(secs)
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        s = f"{d}d {h}h"
    elif h:
        s = f"{h}h {m}m"
    else:
        s = f"{m}m"
    return f"{s} overdue" if overdue else f"in {s}"


# --------------------------------------------------------------------------- the board

# The scheduled run's message is this board — every assignment still open, soonest
# first — not a diff. The diff decides *whether* to send; the board is what you read.
#
# Bands are the urgency dots promoted to a decision. Because the band is part of the
# signature, a deadline sliding from 72h to 24h re-sends the board on its own, with
# nothing having changed on MCV's side. That is the whole point: the thing most likely
# to hurt you is a deadline you already knew about and stopped thinking about.
URGENCY_BANDS = ((24, "red", "🔴"), (72, "yellow", "🟡"))
URGENCY_DEFAULT = ("green", "🟢")
BOARD_LIMIT = 25


def urgency(due: datetime, now: datetime) -> tuple[str, str]:
    """(band, dot) for a due date. The band drives re-sends; the dot is just display."""
    hrs = (due - now).total_seconds() / 3600
    for limit, band, dot in URGENCY_BANDS:
        if hrs < limit:
            return band, dot
    return URGENCY_DEFAULT


def open_assignments(snap: dict, now: datetime | None = None) -> dict:
    """Split a snapshot's assignments into open / closed / undated.

    Shared by `due` and the scheduled run so the two can never drift into showing
    different pictures of the same snapshot.
    """
    now = now or datetime.now()
    names = {c["href"]: c["text"] for c in snap.get("courses", [])}
    dated, undated = [], []
    for href, items in (snap.get("assignments") or {}).items():
        for r in items:
            rec = {**r, "course": names.get(href, href)}
            (dated if r.get("due_iso") else undated).append(rec)
    open_rows = sorted(
        (r for r in dated if datetime.fromisoformat(r["due_iso"]) >= now),
        key=lambda r: r["due_iso"],
    )
    return {"open": open_rows, "closed": len(dated) - len(open_rows),
            "undated": undated, "now": now}


def board_signature(board: dict) -> list[str]:
    """A stable description of what the board says, for comparing against last time.

    Sorted rather than in display order — reordering alone is not news. Undated rows
    are included by id so one appearing still counts as something to tell you about.
    """
    sig = [f"{r.get('id')}|{r['due_iso']}|{urgency(datetime.fromisoformat(r['due_iso']), board['now'])[0]}"
           for r in board["open"]]
    sig += [f"{r.get('id')}|undated" for r in board["undated"]]
    return sorted(sig)


def board_headline(board: dict, prev_sig: list[str] | None, d: dict | None) -> list[str]:
    """The 'what changed' lines that sit above the board.

    Band crossings are recovered from the previous signature rather than from extra
    state — the band is already encoded in it, so there is nothing else to store.
    """
    lines = []
    for a in (d or {}).get("new_assignments", []):
        lines.append(f"🆕 <b>New:</b> {esc(a['title'][:60])} <i>({esc(course_label(a['course']))})</i>")
    for a in (d or {}).get("changed_assignments", []):
        when = "no due date"
        if a.get("due_iso"):
            when = f"{datetime.fromisoformat(a['due_iso']):%a %d %b %H:%M}"
        lines.append(f"⏰ <b>Deadline moved:</b> {esc(a['title'][:60])} → {esc(when)}")
    if prev_sig:
        was = {}
        for s in prev_sig:
            parts = s.split("|")
            if len(parts) == 3:
                was[parts[0]] = parts[2]
        for r in board["open"]:
            due = datetime.fromisoformat(r["due_iso"])
            band, dot = urgency(due, board["now"])
            if band in ("red", "yellow") and was.get(str(r.get("id"))) not in (None, band):
                lines.append(f"{dot} <b>Now {esc(fmt_delta(due, board['now']))}:</b> "
                             f"{esc(r['title'][:60])}")
    return lines


def format_board(board: dict, headline: list[str] | None = None,
                 extra: list[str] | None = None) -> str:
    """Render the board. `headline` leads it, `extra` (page-link changes) trails it."""
    now, open_rows = board["now"], board["open"]
    lines = [f"📋 <b>Open assignments — {len(open_rows)}</b>"]
    if headline:
        lines.append("")
        lines += headline
    if not open_rows:
        lines.append("\nNothing due. Every assignment found has passed its deadline.")
    for r in open_rows[:BOARD_LIMIT]:
        due = datetime.fromisoformat(r["due_iso"])
        lines.append(
            f"\n{urgency(due, now)[1]} <a href=\"{esc(r['href'])}\">{esc(r['title'][:80])}</a>"
            f"\n    {esc(course_label(r['course']))} · due {due:%a %d %b %H:%M}"
            f" · <b>{esc(fmt_delta(due, now))}</b>"
        )
    if len(open_rows) > BOARD_LIMIT:
        lines.append(f"\n… and {len(open_rows) - BOARD_LIMIT} more")
    if board["undated"]:
        lines.append(f"\n\n❔ {len(board['undated'])} assignment(s) with no readable due date")
    if extra:
        lines.append("")
        lines += extra
    lines.append(f"\n<i>{board['closed']} already closed · checked {now:%a %d %b %H:%M}</i>")
    return "\n".join(lines)


def format_page_changes(d: dict) -> list[str]:
    """Course-page link changes, demoted to a footer under the board.

    Still reported — the project's bias is that a spurious ping costs two seconds and
    a missed deadline doesn't — but no longer the headline act.
    """
    lines = []
    if d.get("new_courses"):
        lines.append("🆕 <b>New courses</b>")
        lines += [f"  • {esc(c[:90])}" for c in sorted(d["new_courses"])]
        lines.append("  <i>added to courses.json - give them short names</i>")
    if d.get("gone_courses"):
        lines.append("➖ <b>Courses gone</b>")
        lines += [f"  • {esc(c[:90])}" for c in sorted(d["gone_courses"])]
    for course, items in sorted(d.get("per_course", {}).items()):
        lines.append(f"📌 <b>{esc(course[:70])}</b> — {len(items)} new link(s)")
        lines += [f"  + {esc(it[:110])}" for it in items[:6]]
        if len(items) > 6:
            lines.append(f"  … and {len(items) - 6} more")
    return lines


# --------------------------------------------------------------------------- scraping


SESSION_RETRIES = 3
SESSION_RETRY_WAIT = 5  # seconds


def session_ok(page, retries: int = SESSION_RETRIES, wait: float = SESSION_RETRY_WAIT) -> bool:
    """Is the saved session usable?

    MCV serves its logged-out page at the same URL, so mcv.ensure_session decides by
    counting course links. A page that failed to load for ANY reason — flaky campus wifi
    being the usual one — also yields zero links, and so looks exactly like a logout.
    Retry before believing it: otherwise one dropped connection fires a false
    "session expired" alert, and a false alarm teaches you to ignore the real one.
    """
    for attempt in range(1, retries + 1):
        try:
            if mcv.ensure_session(page):
                return True
            log(f"session check {attempt}/{retries}: no course links")
        except Exception as e:
            log(f"session check {attempt}/{retries} errored: {type(e).__name__}")
        if attempt < retries:
            time.sleep(wait)
    return False


def take_snapshot() -> dict | None:
    """Headless crawl. Returns the snapshot dict, or None if the session is dead."""
    if not mcv.AUTH_FILE.exists():
        raise RuntimeError(f"No auth_state.json in {DATA_DIR} - run: python mcvclient.py login")
    if not acquire_lock():
        raise Busy("another crawl is already running")

    mcv.SNAPSHOTS_DIR.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(storage_state=str(mcv.AUTH_FILE))
                page = ctx.new_page()
                if not session_ok(page):
                    return None

                courses = mcv.scrape_courses(page)
                details, assignments = {}, {}
                for c in courses:
                    details[c["href"]] = mcv.scrape_course_detail(page, c["href"])
                    assignments[c["href"]] = scrape_assignments(page, c["href"])
                snap = {
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "courses": courses,
                    "details": details,
                    "assignments": assignments,
                }
                # Every crawl keeps courses.json in step with your real enrolment.
                sync_course_names(snap)
                return snap
            finally:
                browser.close()
    finally:
        release_lock()


def persist(snap: dict) -> dict | None:
    """Write the new snapshot, return the previous one (None on first ever run)."""
    prev = None
    if mcv.LATEST_FILE.exists():
        try:
            prev = json.loads(mcv.LATEST_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = None
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    blob = json.dumps(snap, indent=2, ensure_ascii=False)
    (mcv.SNAPSHOTS_DIR / f"{ts}.json").write_text(blob, encoding="utf-8")
    mcv.LATEST_FILE.write_text(blob, encoding="utf-8")
    return prev


# --------------------------------------------------------------------------- gates


def within_active_hours(cfg: dict) -> bool:
    """`end` is inclusive, so [9, 23] means 09:00 through 23:59 — the 23:00 run counts.

    This is a backstop, not the schedule: Task Scheduler decides when runs fire. It
    only matters for catch-up runs Windows fires after a sleep/wake at 03:00.
    """
    start, end = cfg.get("active_hours", [9, 23])
    return start <= datetime.now().hour <= end


def on_battery() -> bool:
    """True only if we're certain we're on battery. Any doubt returns False."""
    import ctypes

    class SPS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("SystemStatusFlag", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    status = SPS()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.pointer(status)):
            return False
    except Exception:
        return False
    return status.ACLineStatus == 0


# --------------------------------------------------------------------------- commands


def cmd_run(force: bool = False) -> int:
    cfg = load_notify_config()
    if not force:
        if not within_active_hours(cfg):
            return 0
        if cfg.get("skip_on_battery") and on_battery():
            log("skipped: on battery")
            return 0

    state = load_state()

    try:
        snap = take_snapshot()
    except Exception as e:
        log(f"ERROR during scrape: {e}")
        return 1

    if snap is None:
        last = state.get("session_alert_at")
        due = True
        if last:
            due = datetime.fromisoformat(last) + SESSION_ALERT_COOLDOWN < datetime.now()
        if due:
            send(
                cfg,
                "⚠️ <b>MyCourseVille session expired</b>\n\n"
                "The watcher can't log in, so you are <b>not</b> being notified of "
                "new assignments until this is fixed.\n\n"
                "But send /check first — a wifi drop looks the same as a logout.\n\n"
                "If it really is expired, on your laptop:\n"
                "<code>python mcvclient.py login</code>",
            )
            state["session_alert_at"] = datetime.now().isoformat(timespec="seconds")
            save_state(state)
        log("session expired")
        return 2

    # A good scrape clears any standing session warning.
    state.pop("session_alert_at", None)

    prev = persist(snap)
    if prev is None:
        log(f"baseline snapshot written ({len(snap['courses'])} courses)")

    board = open_assignments(snap)
    sig = board_signature(board)
    prev_sig = state.get("board_sig")
    d = diff(prev, snap) if prev is not None else None

    # Either the assignments or the course pages can be the reason to send, but the
    # board leads the message either way. Editing a pinned board in place was the
    # other option and was dropped: Telegram edits are silent, so it would never
    # once reach the phone.
    page_changes = bool(d and (d["new_courses"] or d["gone_courses"] or d["per_course"]))
    if prev_sig is not None and sig == prev_sig and not page_changes:
        log(f"no change ({len(board['open'])} open, {len(snap['courses'])} courses)")
        save_state(state)
        return 0

    send(cfg, format_board(
        board,
        headline=board_headline(board, prev_sig, d),
        extra=format_page_changes(d) if page_changes else None,
    ))
    extra_note = ""
    if page_changes:
        extra_note = f", {sum(len(v) for v in d['per_course'].values())} page change(s)"
    log(f"notified: board of {len(board['open'])} open{extra_note}")
    state["board_sig"] = sig
    state["last_notified_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return 0


def cmd_check() -> int:
    """Scrape right now and message you either way — the 'is this thing on?' command.

    Deliberately does NOT write the baseline. A test run that consumed the diff
    would leave the next scheduled run with nothing to report.
    """
    cfg = load_notify_config()
    snap = take_snapshot()

    if snap is None:
        send(
            cfg,
            "⚠️ <b>Manual check</b>\n\nMCV session is expired — run "
            "<code>python mcvclient.py login</code> on the laptop.",
        )
        log("check: session expired")
        return 2

    prev = None
    if mcv.LATEST_FILE.exists():
        try:
            prev = json.loads(mcv.LATEST_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = None

    if prev is None:
        send(
            cfg,
            f"🔎 <b>Manual check</b>\n\nNo baseline stored yet. Found "
            f"{len(snap['courses'])} courses — run the watcher once to set a baseline.",
        )
        log("check: no baseline")
        return 0

    d = diff(prev, snap)
    if has_changes(d):
        send(cfg, format_message(d, "🔎 <b>Manual check — changes found</b>"))
        log("check: changes found (baseline left alone)")
        return 0

    lines = [
        "🔎 <b>Manual check — nothing new.</b>",
        f"\nWatching {len(snap['courses'])} courses:",
    ]
    lines += [f"  • {esc(c['text'][:70])}" for c in snap["courses"]]
    lines.append(f"\nBaseline taken {esc(prev.get('fetched_at', '?'))}")
    lines.append(f"<i>{datetime.now().strftime('%a %d %b, %H:%M')}</i>")
    send(cfg, "\n".join(lines))
    log("check: no changes")
    return 0


def cmd_due() -> int:
    """Report every assignment still open, soonest first. Leaves the baseline alone."""
    cfg = load_notify_config()
    snap = take_snapshot()
    if snap is None:
        send(cfg, "⚠️ <b>MCV session expired</b> — run <code>python mcvclient.py login</code>.")
        log("due: session expired")
        return 2

    board = open_assignments(snap)
    msg = format_board(board)
    send(cfg, msg)
    # Console view, without the HTML tags.
    print(re.sub(r"<[^>]+>", "", msg))
    log(f"due: {len(board['open'])} open, {board['closed']} closed")
    return 0


def cmd_courses() -> int:
    """Show course code -> title and seed courses.json, never clobbering names you set."""
    if mcv.LATEST_FILE.exists():
        snap = json.loads(mcv.LATEST_FILE.read_text(encoding="utf-8"))
    else:
        snap = take_snapshot()
        if snap is None:
            sys.exit("Session expired - run: python mcvclient.py login")

    merged = {}
    if COURSES_FILE.exists():
        try:
            merged = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"warning: {COURSES_FILE.name} isn't valid JSON — rewriting it")
            merged = {}

    print(f"{'code':<10} {'short name':<26} full title")
    print("-" * 96)
    for c in snap.get("courses", []):
        code = course_code(c["text"])
        merged.setdefault(code, "")  # an existing name is never overwritten
        print(f"{code:<10} {(merged[code] or '(unset)'):<26} {c['text'][:58]}")

    COURSES_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {COURSES_FILE}")
    print("Fill in short names. Anything left empty falls back to the course code.")
    return 0


def cmd_chatid() -> int:
    cfg = load_notify_config()
    res = telegram_call(cfg["telegram_bot_token"], "getUpdates")
    if not res.get("ok"):
        sys.exit(f"getUpdates failed: {res}")
    seen = {}
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
            seen[chat["id"]] = f"{name} ({chat.get('type')})"
    if not seen:
        print(
            "No messages yet. Open Telegram, find your bot, press Start / send it any\n"
            "message, then run this again."
        )
        return 1
    print("Chat ids found — put the right one in notify_config.json as telegram_chat_id:\n")
    for cid, who in seen.items():
        print(f"  {cid}   {who}")
    return 0


def cmd_test() -> int:
    cfg = load_notify_config()
    if not cfg.get("telegram_chat_id"):
        sys.exit("telegram_chat_id is empty — run: watch.bat chatid")
    send(
        cfg,
        "✅ <b>MCV watcher is wired up.</b>\n"
        "This is what a MyCourseVille alert will look like.\n"
        f"<i>{datetime.now().strftime('%a %d %b, %H:%M')}</i>",
    )
    print("Sent. Check Telegram.")
    return 0


# --------------------------------------------------------------------------- listener

HELP_TEXT = (
    "🤖 <b>MCV watcher</b>\n\n"
    "/due — every assignment still open, soonest first\n"
    "/check — scrape now and report either way\n"
    "/status — is the watcher healthy?\n"
    "/help — this message\n\n"
    "<i>You don't need to ask. The board arrives on its own whenever it changes "
    "- a new assignment, a moved deadline, or one closing in - checked every 30 minutes, 09:00–23:00.</i>"
)

BOT_COMMANDS = [
    {"command": "due", "description": "Assignments still open, soonest first"},
    {"command": "check", "description": "Scrape now and report either way"},
    {"command": "status", "description": "Is the watcher healthy?"},
    {"command": "help", "description": "Show the commands"},
]


def parse_command(text: str) -> str:
    """'/due@MyBot extra args' -> 'due'. Non-commands return ''."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split()[0][1:].split("@")[0].lower()


def is_authorized(chat_id, cfg: dict) -> bool:
    """Anyone who finds the bot can message it. Only the configured chat gets answers."""
    return str(chat_id) == str(cfg.get("telegram_chat_id"))


def handle_command(cfg: dict, cmd: str) -> None:
    if cmd in ("start", "help"):
        send(cfg, HELP_TEXT)
    elif cmd in ("due", "check"):
        send(cfg, "⏳ Checking MyCourseVille — takes about a minute.")
        try:
            cmd_due() if cmd == "due" else cmd_check()
        except Busy:
            send(cfg, "⏳ A check is already running. Try again in a minute.")
        except RuntimeError as e:
            send(cfg, f"⚠️ That failed:\n<code>{esc(str(e)[:400])}</code>")
    elif cmd == "status":
        send(cfg, f"<code>{esc(status_text())}</code>")
    else:
        send(cfg, f"Don't know <code>/{esc(cmd[:30])}</code>. Try /help.")


def cmd_listen() -> int:
    """Long-poll Telegram and act on commands. Runs until killed."""
    cfg = load_notify_config()
    token = cfg["telegram_bot_token"]

    me = telegram_call(token, "getMe")
    username = (me.get("result") or {}).get("username", "?")
    telegram_call(token, "setMyCommands", {"commands": json.dumps(BOT_COMMANDS)})

    state = load_state()
    offset = state.get("tg_offset")
    if offset is None:
        # Skip whatever is already queued, so a /due sent days ago doesn't fire on startup.
        res = telegram_call(token, "getUpdates", {"timeout": 0})
        results = res.get("result") or []
        offset = (results[-1]["update_id"] + 1) if results else 0
        state["tg_offset"] = offset
        save_state(state)

    log(f"listener: started as @{username}, offset={offset}")
    while True:
        try:
            res = telegram_call(
                token,
                "getUpdates",
                {"timeout": 50, "offset": offset, "allowed_updates": json.dumps(["message"])},
                timeout=70,
            )
        except Exception as e:
            # Wifi drops and Telegram blips are expected; back off and keep going.
            # Catch broadly on purpose: a crashed listener stays dead until the next
            # logon, so no transient error is worth letting through here.
            log(f"listener: poll failed ({type(e).__name__}: {str(e)[:100]}) - retrying in 15s")
            time.sleep(15)
            continue

        for upd in res.get("result") or []:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if not is_authorized(chat_id, cfg):
                log(f"listener: ignored message from unauthorized chat {chat_id}")
                continue
            cmd = parse_command(text)
            if not cmd:
                continue
            log(f"listener: /{cmd}")
            try:
                handle_command(cfg, cmd)
            except Exception as e:
                log(f"listener: /{cmd} blew up: {type(e).__name__}: {e}")

        if res.get("result"):
            state["tg_offset"] = offset
            save_state(state)


def status_text() -> str:
    state = load_state()
    cfg_present = NOTIFY_CONFIG.exists()
    lines = [
        f"project folder     : {PROJECT_ROOT}",
        f"data dir           : {DATA_DIR}",
        f"notify_config.json : {'present' if cfg_present else 'MISSING'}",
        f"auth_state.json    : {'present' if mcv.AUTH_FILE.exists() else 'MISSING - run mcvclient.py login'}",
        f"latest snapshot    : {'present' if mcv.LATEST_FILE.exists() else 'none yet'}",
    ]
    if mcv.LATEST_FILE.exists():
        try:
            snap = json.loads(mcv.LATEST_FILE.read_text(encoding="utf-8"))
            n = sum(len(v) for v in snap.get("assignments", {}).values())
            lines.append(f"baseline           : {snap.get('fetched_at','?')} "
                         f"({len(snap.get('courses', []))} courses, {n} assignments)")
        except (json.JSONDecodeError, OSError):
            lines.append("baseline           : unreadable")
    if cfg_present:
        cfg = json.loads(NOTIFY_CONFIG.read_text(encoding="utf-8"))
        start, end = cfg.get("active_hours", [9, 23])
        inside = "inside" if within_active_hours(cfg) else "outside"
        lines.append(f"active hours       : {start:02d}:00-{end:02d}:59 "
                     f"(now {datetime.now():%H:%M} -> {inside})")
        lines.append(f"token / chat id    : {'set' if cfg.get('telegram_bot_token') else 'MISSING'} / "
                     f"{'set' if cfg.get('telegram_chat_id') else 'MISSING'}")
    lines.append(f"crawl in progress  : {'yes' if LOCK_FILE.exists() else 'no'}")
    lines.append(f"last notified      : {state.get('last_notified_at', 'never')}")
    if state.get("session_alert_at"):
        lines.append(f"session warning    : sent {state['session_alert_at']}")
    return "\n".join(lines)


def cmd_status() -> int:
    print(status_text())
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    forced = "--force" in sys.argv[1:]
    cmd = args[0] if args else "run"
    handlers = {
        "run": lambda: cmd_run(force=forced),
        "check": cmd_check,
        "due": cmd_due,
        "chatid": cmd_chatid,
        "test": cmd_test,
        "status": cmd_status,
        "listen": cmd_listen,
        "courses": cmd_courses,
    }
    if cmd not in handlers:
        sys.exit(f"Unknown command: {cmd}. Try: run | due | check | courses | listen | chatid | test | status")
    try:
        sys.exit(handlers[cmd]())
    except RuntimeError as e:
        # Expected, explainable failures (bad token, Telegram down) shouldn't
        # dump a stack trace at you.
        sys.exit(f"error: {e}")
