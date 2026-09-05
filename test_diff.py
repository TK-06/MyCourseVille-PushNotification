"""Offline checks for the diff/noise-filter logic. No network, no MCV, no Telegram.

    %USERPROFILE%\.claude\skills\mcv\.venv\Scripts\python.exe test_diff.py

These are the assertions that stop the watcher from either spamming you every
hour or going quiet when something real lands.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch

C1 = "https://www.mycourseville.com/?q=courseville/course/84996"
C2 = "https://www.mycourseville.com/?q=courseville/course/77719"


def course(href, text):
    return {"href": href, "text": text}


def detail(links):
    return {"title": "t", "link_count": len(links), "links": links}


base_links = [
    {"text": "Home", "href": "https://www.mycourseville.com/"},
    {"text": "Logout", "href": "javascript:void(0)"},
    {"text": "Lect 1 Intro", "href": C1 + "/material/1"},
]

prev = {
    "courses": [course(C1, "2110215 PROG METH"), course(C2, "2110101 COMP PROG")],
    "details": {C1: detail(base_links), C2: detail(base_links)},
}

curr = {
    "courses": [
        course(C1, "2110215 PROG METH"),
        course(C2, "2110101 COMP PROG"),
        course("https://www.mycourseville.com/?q=courseville/course/90001", "2110999 NEW COURSE"),
    ],
    "details": {
        C1: detail(base_links + [
            {"text": "HW3 due 5 Sep 23:59", "href": C1 + "/assignment/3"},
            # same link, cache-buster appended -> must NOT count as new
            {"text": "Lect 1 Intro", "href": C1 + "/material/1&t=1724800000"},
            # nav chrome appearing later -> must NOT count as new
            {"text": "Calendar", "href": "https://www.mycourseville.com/?q=courseville/calendar"},
        ]),
        C2: detail(base_links),
    },
}

failures = []


def check(label, cond, detail_msg=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(f"{label} {detail_msg}")


print("real change is detected:")
d = watch.diff(prev, curr)
check("a genuinely new course is reported", d["new_courses"] == ["2110999 NEW COURSE"], d["new_courses"])
check("nothing is falsely reported as removed", d["gone_courses"] == [], d["gone_courses"])
check("the new assignment is the only per-course item",
      list(d["per_course"].values()) == [["HW3 due 5 Sep 23:59"]], d["per_course"])
check("has_changes() agrees", watch.has_changes(d))

print("\nnoise does not fire:")
d2 = watch.diff(prev, prev)
check("identical snapshots produce no alert", not watch.has_changes(d2))
check("cache-buster param is stripped",
      watch.normalize(C1 + "/material/1&t=1") == watch.normalize(C1 + "/material/1&t=2"))
check("nav text is filtered", watch.is_noise({"text": "Calendar", "href": C1}))
check("empty link text is filtered", watch.is_noise({"text": "", "href": C1}))
check("javascript: href is filtered", watch.is_noise({"text": "Click", "href": "javascript:void(0)"}))
check("a real assignment link is NOT filtered",
      not watch.is_noise({"text": "HW3 due", "href": C1 + "/assignment/3"}))

print("\nfirst-sight courses don't spam:")
d3 = watch.diff({"courses": [], "details": {}}, curr)
check("a course with no baseline reports zero links", d3["per_course"] == {}, d3["per_course"])

print("\nmessage rendering:")
msg = watch.format_message(d)
check("stays under the Telegram 4096 limit", len(msg) < watch.TELEGRAM_LIMIT, len(msg))
check("HTML-escapes course names", "&amp;" in watch.esc("A & B"))

print("\n--- rendered message ---")
print(msg)

print("\nassignment URL building:")
# Regression: dashboard hrefs carry &from=home. Appending "/assignment" to the href
# buried the path inside the `from` param and silently served the course home page,
# so every assignment was invisible.
check("&from=home href still yields the assignment page",
      watch.assignment_url("https://www.mycourseville.com/?q=courseville/course/84992&from=home")
      == "https://www.mycourseville.com/?q=courseville/course/84992/assignment",
      watch.assignment_url("https://www.mycourseville.com/?q=courseville/course/84992&from=home"))
check("plain course href works too",
      watch.assignment_url("https://www.mycourseville.com/?q=courseville/course/84992")
      == "https://www.mycourseville.com/?q=courseville/course/84992/assignment")
check("href with no course id returns None",
      watch.assignment_url("https://www.mycourseville.com/") is None)

print("\ndue-date parsing:")
check("full date and time", watch.parse_due("Due on 01 September 2026 at 23:59") == "2026-09-01T23:59")
check("date with no time defaults to end of day",
      watch.parse_due("Due on 8 March 2026") == "2026-03-08T23:59")
check("unparseable returns None", watch.parse_due("whenever you feel like it") is None)
check("bad month returns None", watch.parse_due("Due on 01 Smarch 2026 at 10:00") is None)
check("impossible date returns None", watch.parse_due("Due on 31 February 2026 at 10:00") is None)

print("\ntime-remaining formatting:")
from datetime import datetime as _dt, timedelta as _td
_now = _dt(2026, 8, 28, 12, 0)
check("days ahead", watch.fmt_delta(_now + _td(days=2, hours=3), _now) == "in 2d 3h")
check("hours ahead", watch.fmt_delta(_now + _td(hours=5, minutes=30), _now) == "in 5h 30m")
check("minutes ahead", watch.fmt_delta(_now + _td(minutes=41), _now) == "in 41m")
check("past reads as overdue", "overdue" in watch.fmt_delta(_now - _td(days=1), _now))

print("\nnew assignments fire, and don't spam on first sight:")
A = lambda i, t, due: {"id": i, "title": t, "href": f"h{i}", "due_iso": due, "due_raw": "", "out_raw": "", "note": ""}
p2 = {"courses": [course(C1, "2190222 DSA")], "details": {}, "assignments": {C1: [A("1", "HW1", "2026-09-01T23:59")]}}
c2 = {"courses": [course(C1, "2190222 DSA")], "details": {},
      "assignments": {C1: [A("1", "HW1", "2026-09-01T23:59"), A("2", "HW2", "2026-09-05T23:59")]}}
d4 = watch.diff(p2, c2)
check("a newly posted assignment is reported",
      [a["title"] for a in d4["new_assignments"]] == ["HW2"], d4["new_assignments"])
check("has_changes() fires on assignments alone", watch.has_changes(d4))
check("the alert names the due date", "05 Sep" in watch.format_message(d4))

d5 = watch.diff({"courses": [course(C1, "x")], "details": {}}, c2)
check("a course with no assignment baseline reports none",
      d5["new_assignments"] == [], d5["new_assignments"])
d6 = watch.diff(c2, c2)
check("unchanged assignments produce no alert", not watch.has_changes(d6))

print("\nmoved deadlines are caught:")
c3 = {"courses": [course(C1, "2190222 DSA")], "details": {},
      "assignments": {C1: [A("1", "HW1", "2026-08-30T23:59")]}}   # was 2026-09-01
d7 = watch.diff(p2, c3)
check("a deadline pulled earlier is reported",
      [a["title"] for a in d7["changed_assignments"]] == ["HW1"], d7["changed_assignments"])
check("the old deadline is kept for the message",
      d7["changed_assignments"][0]["was_due_iso"] == "2026-09-01T23:59")
check("has_changes() fires on a moved deadline alone", watch.has_changes(d7))
check("the alert shows old and new", "01 Sep" in watch.format_message(d7)
      and "30 Aug" in watch.format_message(d7))
check("a moved deadline is not also counted as new", d7["new_assignments"] == [])

print("\ntelegram command parsing:")
check("plain command", watch.parse_command("/due") == "due")
check("command with @botname", watch.parse_command("/due@MyCourseBot") == "due")
check("command with trailing args", watch.parse_command("/check now please") == "check")
check("uppercase is normalised", watch.parse_command("/DUE") == "due")
check("leading whitespace tolerated", watch.parse_command("  /status ") == "status")
check("plain chatter is not a command", watch.parse_command("hello there") == "")
check("empty text is not a command", watch.parse_command("") == "")

print("\ncommand authorization:")
_cfg = {"telegram_chat_id": "2022106262"}
check("the configured chat is allowed", watch.is_authorized(2022106262, _cfg))
check("string and int chat ids compare equal", watch.is_authorized("2022106262", _cfg))
check("a stranger's chat is rejected", not watch.is_authorized(999999, _cfg))
check("None chat id is rejected", not watch.is_authorized(None, _cfg))

print("\nscrape lock:")
watch.release_lock()
check("lock is free to start", watch.acquire_lock())
check("a second acquire is refused while held", not watch.acquire_lock())
watch.release_lock()
check("released lock can be re-taken", watch.acquire_lock())
watch.release_lock()
check("lock file is gone after release", not watch.LOCK_FILE.exists())

print("\nsession check tolerates a wifi blip:")
import mcvclient as _mcv
_orig = _mcv.ensure_session
try:
    calls = {"n": 0}
    def _flaky(page):
        calls["n"] += 1
        return calls["n"] >= 3          # fails twice, then the wifi comes back
    _mcv.ensure_session = _flaky
    check("a transient failure is retried, not reported as a logout",
          watch.session_ok(None, retries=3, wait=0))
    check("it stopped as soon as it succeeded", calls["n"] == 3, calls["n"])

    calls["n"] = 0
    _mcv.ensure_session = lambda page: False
    check("a genuine logout is still reported after all retries",
          not watch.session_ok(None, retries=3, wait=0))

    def _boom(page):
        raise RuntimeError("net down")
    _mcv.ensure_session = _boom
    check("an exception during the check doesn't crash the run",
          not watch.session_ok(None, retries=2, wait=0))
finally:
    _mcv.ensure_session = _orig

print("\ncourse short names:")
import tempfile, pathlib, json as _json, os as _os
_real_file, _real_mtime = watch.COURSES_FILE, watch._course_names_mtime
_tmp = pathlib.Path(tempfile.gettempdir()) / "mcv_courses_test.json"


def _use(mapping):
    """Point course_label at a throwaway file and bust the mtime cache."""
    if mapping is None:
        if _tmp.exists():
            _tmp.unlink()
    else:
        _tmp.write_text(mapping if isinstance(mapping, str) else _json.dumps(mapping), encoding="utf-8")
    watch.COURSES_FILE = _tmp
    watch._course_names_mtime = -1.0


try:
    TITLE = "2190222.i (2026/1) Fundamental Data Structure and Algorithm"
    _use({"2190222": "Data Structures"})
    check("a mapped code renders as its short name", watch.course_label(TITLE) == "Data Structures")
    check("course_code itself is unchanged", watch.course_code(TITLE) == "2190222")

    _use({"2190999": "Something Else"})
    check("an unmapped code falls back to the code", watch.course_label(TITLE) == "2190222")

    _use({"2190222": ""})
    check("an empty name falls back to the code", watch.course_label(TITLE) == "2190222")

    _use(None)
    check("a missing courses.json falls back to the code", watch.course_label(TITLE) == "2190222")

    _use("{ this is not json")
    check("a malformed courses.json doesn't crash", watch.course_label(TITLE) == "2190222")

    # The listener is long-lived, so an edit must take effect without a restart.
    _use({"2190222": "DSA"})
    check("first read picks up the name", watch.course_label(TITLE) == "DSA")
    _os.utime(_tmp, (0, 0))  # force a different mtime
    _tmp.write_text(_json.dumps({"2190222": "Algorithms"}), encoding="utf-8")
    check("an edit is picked up without restarting", watch.course_label(TITLE) == "Algorithms")
finally:
    watch.COURSES_FILE, watch._course_names_mtime = _real_file, _real_mtime
    if _tmp.exists():
        _tmp.unlink()

print("\ncourses.json auto-append is append-only:")
_real_file2, _real_mtime2 = watch.COURSES_FILE, watch._course_names_mtime
_tmp2 = pathlib.Path(tempfile.gettempdir()) / "mcv_sync_test.json"


def _snap(*titles):
    return {"courses": [{"href": f"h{i}", "text": t} for i, t in enumerate(titles)]}


try:
    watch.COURSES_FILE = _tmp2

    _tmp2.write_text(_json.dumps({"2190222": "DSA"}), encoding="utf-8")
    added = watch.sync_course_names(_snap("2190222.i (2026/1) DSA", "2110999.i (2026/1) New One"))
    saved = _json.loads(_tmp2.read_text(encoding="utf-8"))
    check("a newly enrolled course is appended", added == ["2110999"], added)
    check("the new code lands with an empty name", saved.get("2110999") == "")
    check("an existing name is NOT overwritten", saved.get("2190222") == "DSA", saved)

    # Re-running must be a no-op, including not touching mtime (the label cache keys on it).
    before = _tmp2.stat().st_mtime_ns
    added2 = watch.sync_course_names(_snap("2190222.i (2026/1) DSA", "2110999.i (2026/1) New One"))
    check("re-running adds nothing", added2 == [], added2)
    check("an unchanged file is not rewritten", _tmp2.stat().st_mtime_ns == before)

    # The important one: a file you hand-edited into invalid JSON must survive untouched.
    _tmp2.write_text('{"2190222": "DSA",  <- oops', encoding="utf-8")
    raw_before = _tmp2.read_text(encoding="utf-8")
    added3 = watch.sync_course_names(_snap("2110999.i (2026/1) New One"))
    check("a malformed file is left completely alone", added3 == [] and
          _tmp2.read_text(encoding="utf-8") == raw_before)

    # A JSON list is valid JSON but the wrong shape; must not be clobbered either.
    _tmp2.write_text('["not", "a", "map"]', encoding="utf-8")
    raw_before = _tmp2.read_text(encoding="utf-8")
    added4 = watch.sync_course_names(_snap("2110999.i (2026/1) New One"))
    check("a non-object file is left alone", added4 == [] and
          _tmp2.read_text(encoding="utf-8") == raw_before)

    # Missing file: create it from scratch.
    _tmp2.unlink()
    added5 = watch.sync_course_names(_snap("2190222.i (2026/1) DSA"))
    check("a missing file is created", added5 == ["2190222"] and _tmp2.exists(), added5)
finally:
    watch.COURSES_FILE, watch._course_names_mtime = _real_file2, _real_mtime2
    if _tmp2.exists():
        _tmp2.unlink()

print("\nthe live board -- what makes it re-send, and what stays quiet:")
NOW = _dt(2026, 8, 29, 12, 0)
B = lambda i, t, due: {"id": i, "title": t, "href": f"h{i}", "due_iso": due,
                       "due_raw": "", "out_raw": "", "note": ""}


def snap_board(*rows):
    return {"courses": [course(C1, "2190222 DSA")], "details": {}, "assignments": {C1: list(rows)}}


ROWS_B1 = (B("1", "HW1", "2026-09-05T23:59"),
           B("2", "HW2", "2026-09-01T23:59"),
           B("3", "OLD", "2026-08-01T23:59"))
b1 = watch.open_assignments(snap_board(*ROWS_B1), NOW)

check("closed assignments are off the board",
      [r["id"] for r in b1["open"]] == ["2", "1"], [r["id"] for r in b1["open"]])
check("closed ones are counted, not listed", b1["closed"] == 1, b1["closed"])

b_un = watch.open_assignments(snap_board(B("1", "HW1", "2026-09-05T23:59"),
                                         B("9", "NoDate", None)), NOW)
check("undated assignments are kept aside, not dropped",
      [r["id"] for r in b_un["undated"]] == ["9"], b_un["undated"])
check("an undated assignment is not treated as open",
      [r["id"] for r in b_un["open"]] == ["1"], b_un["open"])

# The signature IS the send / stay-quiet decision, so it gets the most attention.
check("an unchanged board produces an unchanged signature",
      watch.board_signature(b1) ==
      watch.board_signature(watch.open_assignments(snap_board(*ROWS_B1), NOW)))

b_new = watch.open_assignments(snap_board(B("1", "HW1", "2026-09-05T23:59"),
                                          B("2", "HW2", "2026-09-01T23:59"),
                                          B("4", "HW4", "2026-09-09T23:59")), NOW)
check("a new assignment changes the signature",
      watch.board_signature(b_new) != watch.board_signature(b1))

b_moved = watch.open_assignments(snap_board(B("1", "HW1", "2026-09-06T23:59"),
                                            B("2", "HW2", "2026-09-01T23:59"),
                                            B("3", "OLD", "2026-08-01T23:59")), NOW)
check("a moved deadline changes the signature, though the id set is identical",
      watch.board_signature(b_moved) != watch.board_signature(b1))

# The trigger that fires with nothing changing on MCV's side: time crossing a band.
SOLO = (B("1", "HW1", "2026-08-31T12:00"),)          # 48h out from NOW -> yellow
early = watch.open_assignments(snap_board(*SOLO), NOW)
later = watch.open_assignments(snap_board(*SOLO), _dt(2026, 8, 30, 20, 0))   # 16h -> red
mid = watch.open_assignments(snap_board(*SOLO), _dt(2026, 8, 29, 14, 0))     # 46h -> still yellow

check("crossing into the 24h band changes the signature",
      watch.board_signature(early) != watch.board_signature(later))
check("...and only the band changed, not the underlying row",
      [s.rsplit("|", 1)[0] for s in watch.board_signature(early)] ==
      [s.rsplit("|", 1)[0] for s in watch.board_signature(later)])
check("drifting within a band stays quiet",
      watch.board_signature(early) == watch.board_signature(mid))

gone = watch.open_assignments(snap_board(*SOLO), _dt(2026, 9, 1, 12, 0))
check("an assignment closing changes the signature",
      watch.board_signature(gone) != watch.board_signature(early))
check("a board with nothing open is empty but still counts the closed one",
      gone["open"] == [] and gone["closed"] == 1)

# Rendering.
msg = watch.format_board(b1)
check("the board lists every open assignment", "HW1" in msg and "HW2" in msg)
check("the board omits closed ones", "OLD" not in msg)
check("the board leads with the open count", "2" in msg.split("\n")[0])
check("the board dates each row", "05 Sep" in msg and "01 Sep" in msg)
check("the board sorts soonest first", msg.index("HW2") < msg.index("HW1"))
check("an empty board says so", "Nothing due" in watch.format_board(gone))
check("undated assignments are surfaced in the render",
      "no readable due date" in watch.format_board(b_un))

# The what-changed headline that leads the scheduled message.
head = watch.board_headline(b_new, watch.board_signature(b1), {
    "new_assignments": [{"id": "4", "title": "HW4", "course": "2190222 DSA",
                         "due_iso": "2026-09-09T23:59", "href": "h4"}],
    "changed_assignments": [],
})
check("the headline names a new assignment", any("HW4" in l for l in head), head)

band_head = watch.board_headline(later, watch.board_signature(early), None)
check("the headline calls out a band crossing", any("HW1" in l for l in band_head), band_head)
check("a quiet drift produces no headline",
      watch.board_headline(mid, watch.board_signature(early), None) == [])
check("with no previous signature there is no band headline",
      watch.board_headline(later, None, None) == [])

# Page-link changes still get through, demoted under the board.
foot = watch.format_page_changes(d)
check("page changes still render as a footer", any("NEW COURSE" in l for l in foot), foot)

if failures:
    print(f"\n{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nAll checks passed.")
