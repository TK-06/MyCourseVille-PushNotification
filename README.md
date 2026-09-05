# mcvPushNoti

Push notifications for [MyCourseVille](https://www.mycourseville.com), which doesn't have any.

Polls MCV every 30 minutes, works out what actually changed since last time, and sends a
Telegram message only when something did. Ask it `/due` from your phone any time. Costs nothing to run — no LLM in the loop, no server.

---

## Why this exists

MCV is Chula's LMS and it will not tell you when an assignment appears. You find out by
remembering to look. That's a bad way to not miss a deadline.

There is no usable API. MCV runs an OAuth server, but client IDs aren't issued to students
and the one public namespace is read-only. So this drives a real browser headlessly using a
saved login session, and diffs the pages.

## How it works

```
Task Scheduler (every 30 min, 09:00-23:00)
        |
        v
   watch.py  --->  headless Chromium, replays saved cookies
        |          crawls dashboard, every course page,
        |          and every course's Assignments tab
        v
   build the board: every assignment still open, soonest first
        |          then diff vs. snapshots/latest.json to decide whether to send
        |
   board reads the same? ---> exit 0, silently (this is the usual case)
        |
   new assignment, moved deadline, a deadline crossing into
   72h or 24h, or new course material?
        |
        +---> Telegram message ---> your phone
              (the whole open-assignment board, with what changed on top)


watch.py listen  (separate process, starts at logon)
        |
        v
   long-polls Telegram  <--- you type /due on your phone
        |
        v
   runs the same code, replies in the same chat
```

The expensive-looking part is the browser, and it's still just Python. **No Claude, no tokens,
no API bill.** Doing this with a scheduled AI agent instead would burn roughly a million
tokens a day to answer "did a string change?", which is why it isn't built that way.

---

## Where the files live, and why

The code is self-contained and holds **no secrets**, so this folder is safe to sync or
publish. Everything sensitive lives in a separate **data directory**.

| Location | Holds | Safe to publish? |
|---|---|---|
| this repo | `watch.py`, `mcvclient.py`, tests, `courses.json`, `watch.log` | Yes |
| the data dir | `config.json` (**your CU password**), `auth_state.json` (**a live session**), `notify_config.json` (**bot token**), `snapshots/` | **No** |

The data directory is resolved in this order:

1. `$MCV_DATA_DIR`
2. `~/.mcvpushnoti` if it exists
3. `~/.claude/skills/mcv` if it holds an `auth_state.json` — the `mcv` Claude Code skill's
   own folder, so an existing skill install keeps working with no re-login
4. `~/.mcvpushnoti`, created on demand

Check which one is in play with `python mcvclient.py where`.

**Never put those three files in this folder.** `config.json` is your university password in
plaintext and `auth_state.json` is a live logged-in session, which is just as good as the
password. Keep the data dir off OneDrive/Dropbox — syncing it uploads your CU password to
someone else's computer. `.gitignore` blocks all three names here as a backstop.

## One-time setup

### 0. Prerequisites

Python 3.10+ and Playwright's Chromium:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Nothing else is needed — `mcvclient.py` is vendored, so this repo does not depend on the
`mcv` Claude Code skill. (If you already have that skill installed, its data directory is
picked up automatically and you can skip the login step.)

### 1. Log in to MCV

Put your credentials in `config.json` **in the data directory** (see above):

```json
{ "username": "<your CU id>", "password": "<your password>" }
```

Then log in. Sessions last roughly 2-3 weeks. This opens a real browser window because you
have to clear MFA yourself:

```powershell
.\.venv\Scripts\python.exe mcvclient.py login
```

### 2. Make a Telegram bot

1. Message **@BotFather** on Telegram, send `/newbot`, pick a name and a username ending in `bot`.
2. It replies with a token like `8123456789:AAH...`. That token is a credential — treat it like a password.
3. Create the config **in the data directory**, not here. Run
   `python mcvclient.py where` to see the path, then copy the template there:

```powershell
$data = (& .\.venv\Scripts\python.exe mcvclient.py where | Select-String "data dir").Line.Split(":",2)[1].Trim()
copy ".
otify_config.example.json" "$data
otify_config.json"
notepad "$data
otify_config.json"
```

Paste the token into `telegram_bot_token`.

### 3. Find your chat id

Open Telegram, find your new bot, press **Start** (or send it anything). Then:

```powershell
.\watch.bat chatid
```

It prints the chat ids it can see. Put the right number into `telegram_chat_id`, then:

```powershell
.\watch.bat test
```

A message should land on your phone.

### 4. Write the baseline

The first successful run has no previous board to compare against, so it sends you the
current open-assignment list and stores it as the baseline:

```powershell
.\watch.bat run --force
```

### 5. Register the scheduled task

Registered as **"MCV Watcher"** — every 30 minutes, 09:00–23:00, via `pythonw.exe` so no
console window flashes. To recreate it from scratch, in PowerShell:

```powershell
$py     = "$env:USERPROFILE\.claude\skills\mcv\.venv\Scripts\pythonw.exe"
$script = "$env:USERPROFILE\OneDrive\Documents\computer_programming\Projects\mcvPushNoti\watch.py"

$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]"09:00")
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At ([datetime]"09:00") `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 14)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "MCV Watcher" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Polls MyCourseVille every 30 min and pushes changes to Telegram." -Force
```

PowerShell's cmdlets are used rather than raw `schtasks` because the nested-quote escaping
in `schtasks /TR` is easy to get wrong, and because `-StartWhenAvailable` (run a missed
slot once the laptop wakes) has no clean `schtasks` equivalent. `-ExecutionTimeLimit`
kills a hung Chromium after 15 minutes instead of leaving it resident.

Managing it afterwards:

```powershell
Get-ScheduledTask     -TaskName "MCV Watcher"
Get-ScheduledTaskInfo -TaskName "MCV Watcher"   # NextRunTime, LastTaskResult (0 = ok)
Start-ScheduledTask   -TaskName "MCV Watcher"   # run now
Unregister-ScheduledTask -TaskName "MCV Watcher" -Confirm:$false
```

### 6. Register the Telegram listener

Lets you type `/due` on your phone instead of opening the laptop. Starts at logon and
restarts itself if it dies:

```powershell
$me = "$env:USERDOMAIN\$env:USERNAME"
$lAction  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`" listen"
$lTrigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$lSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 99 -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "MCV Listener" -Action $lAction -Trigger $lTrigger `
    -Settings $lSettings -User $me -Description "Telegram command listener for the MCV watcher." -Force
```

`-User` is not optional here: an `-AtLogOn` trigger without it registers for *any* user and
Windows refuses that without elevation (`Access is denied`). `-ExecutionTimeLimit 0` means
no limit, since this one is meant to run forever.

---

## Talking to it from your phone

The listener registers its own command menu, so Telegram autocompletes these:

| Command | What you get |
|---|---|
| `/due` | Every assignment still open, soonest first |
| `/check` | Scrape now, report either way |
| `/status` | Is the watcher healthy? Baseline age, active hours, whether a crawl is running |
| `/help` | The list above |

A `/due` takes about a minute, so the bot replies "⏳ Checking…" first and sends the result
when the crawl finishes.

**Only your chat id gets answers.** Anyone who guesses the bot's username can message it;
messages from any other chat are logged and dropped. The bot token is the real secret — if
it leaks, someone can read messages sent to the bot, so `/revoke` it in BotFather.

Why polling and not a webhook: a webhook needs a public HTTPS endpoint, which a laptop
behind university wifi doesn't have. Long-polling costs one idle HTTPS connection.

**This still needs the laptop awake.** The scraper needs your MCV cookies and Chromium, both
of which live here — so no remote trigger, Telegram or otherwise, works while the lid is
shut. That's what improvement 6 is about.

---

## Naming your courses

Messages identify a course by its code — `2190222` — which nobody remembers. `courses.json`
maps codes to whatever you actually call the class:

```json
{
  "2190222": "Data Structures",
  "2190472": "Netcentric"
}
```

**It maintains itself.** Every crawl appends any course code it hasn't seen before with an
empty name, so enrolling in something new adds it automatically and the next alert tells you
to name it. You only ever edit the values.

To see the full titles alongside the codes, or to seed the file up front:

```powershell
.\watch.bat courses
```

Anything left empty falls back to the bare code, so a half-filled file is fine.

The append is strictly additive: an existing name is never overwritten, an unchanged file is
never rewritten, and a file that doesn't parse as a JSON object is left **completely alone**
rather than regenerated — a stray comma while hand-editing shouldn't cost you every name you
typed. You'll see `courses.json unreadable - leaving it alone` in `watch.log` if that happens.

It is keyed on the course code, not MyCourseVille's internal course id, because the internal
id changes every term while the code doesn't. Your file keeps working next semester.

Edits are picked up without restarting the listener.

---

## What the messages look like

**Unprompted, when something changed:**

```
📚 MyCourseVille update

📝 New assignments
  • HW5 — due Mon 07 Sep 23:59 (in 8d 2h)
    2190222

⏰ Deadline changed
  • HW4
    2184301 · Tue 08 Sep 13:00 → Mon 07 Sep 13:00 (in 7d 2h)

📌 2190472 Netcentric Architecture
    + Lecture 5 slides
```

**`/due`:**

```
📋 Open assignments — 4

🔴 Exercise I : Chapter 2 Application Layer
    2190472 · due Fri 28 Aug 23:59 · in 41m

🟡 Lab - Week4 Question1
    2190222 · due Sun 30 Aug 23:59 · in 2d 0h

18 already closed · checked Fri 28 Aug 23:17
```

Titles are tappable links straight to the worksheet.

**When the session dies** (at most once every 6 hours):

```
⚠️ MyCourseVille session expired

The watcher can't log in, so you are not being
notified of new assignments until this is fixed.
```

---

## Running it day to day

**You don't.** That's the whole point. Windows fires it every 30 minutes and it stays silent
unless something happened. When something does, your phone buzzes.

Two things worth knowing:

- Every run appends one line to `watch.log`, so you can confirm it's alive rather than
  quietly broken.
- When the MCV session expires, it pushes *"session expired — you are not being notified"*
  to Telegram, at most once every 6 hours. Then you re-run step 1. This is the failure mode
  most likely to bite you, so it announces itself instead of failing silently.

### Commands

| Command | What it does |
|---|---|
| `watch.bat` | The scheduled run. Silent unless something changed. |
| `watch.bat --force` | Same, ignoring the active-hours window. |
| `watch.bat due` | **Every assignment still open**, soonest first, with time remaining. 🔴 under 24h, 🟡 under 72h, 🟢 beyond. Sends to Telegram and prints to the terminal. Leaves the baseline alone. |
| `watch.bat check` | **Test run.** Scrapes right now and messages you *either way* — the real alert if something changed, otherwise a "nothing new" note listing the courses it's watching. Deliberately does not touch the baseline, so it can't swallow a change the next real run should report. |
| `watch.bat status` | Where things are, what's configured, when it last fired. No network. |
| `watch.bat test` | Send a test Telegram message. |
| `watch.bat courses` | Print course codes and seed `courses.json` with short names. |
| `watch.bat chatid` | Look up your Telegram chat id. |
| `watch.bat listen` | Run the Telegram command listener in the foreground (the scheduled task does this for you). |
| `python test_diff.py` | Offline checks on the diff and board logic. No network, no MCV, no Telegram. |

### Config (`notify_config.json`, in the data directory)

| Key | Meaning |
|---|---|
| `telegram_bot_token` | From @BotFather. |
| `telegram_chat_id` | From `watch.bat chatid`. |
| `active_hours` | `[9, 23]` = 09:00-23:59, end inclusive. A backstop for catch-up runs after sleep — the scheduler is what actually sets the cadence. |
| `skip_on_battery` | `true` skips runs on battery. Off by default. |

---

## Concerns

Real ones, roughly worst first.

**The session dies every few weeks and only you can fix it.** MFA means no unattended
re-login. The watcher tells you when it happens, but if you ignore the message you are
silently uncovered. This is the single biggest reliability hole.

**"Session expired" can still be a lie.** MCV serves its logged-out page at the same URL, so
the only way to tell is by counting course links — and a page that failed to load produces
zero links too. Flaky campus wifi therefore looks identical to a logout. The check now
retries three times, five seconds apart, before believing it, which absorbs a normal blip.
A sustained outage will still report as expired. Before re-running `login`, send `/check`:
if it comes back with your courses, the session was fine all along.

**It only runs when your laptop is awake and you're logged in.** `-StartWhenAvailable` means
a slot missed while asleep fires once on wake, so you get the backlog — but on wake, not at
the time it mattered. Anything posted at 02:00 still reaches you at 09:00.

**Course-page changes are still link-level only.** Assignments are tracked properly (by id,
with due dates), but everything else — announcements, materials — is compared as link text.
Editing an announcement's *body* without changing its title stays invisible.

**Submission status is not tracked.** The watcher knows an assignment exists and when it's
due; it does not know whether *you* have handed it in. `due` lists everything still open,
submitted or not. Reading real status means visiting each worksheet page — roughly 20 extra
page loads per run — so it's deliberately left out for now.

**Scraping is fragile by nature.** The selectors in `mcvclient.py` and `watch.py` match
MCV's current HTML. A
redesign breaks it — probably loudly (zero courses found, read as an expired session), but
possibly quietly.

**Filtering is a denylist, so expect the occasional false ping.** Anything not recognised as
nav chrome gets through. That's deliberate: a spurious notification costs two seconds, a
missed deadline doesn't. If MCV starts rotating a URL parameter this doesn't know about,
you'll get a repeated phantom alert — add the parameter to `VOLATILE_PARAMS` in `watch.py`.

**RAM.** Each run launches headless Chromium and crawls every course page plus its
Assignments tab: a few hundred MB for roughly two minutes, now twice an hour. A lock file
(`scrape.lock`) stops the listener and the scheduled run from crawling at once; a lock older
than 15 minutes is treated as a crashed run and taken over. Fine when idle. Less fine mid-game on a 16 GB laptop — that's what
`skip_on_battery` is for, and it's off by default.

**Credentials.** `config.json` holds your CU password in plaintext and `auth_state.json` is
a live session. Both sit in the data directory, unencrypted, protected only by your Windows
account. Anyone with your unlocked laptop has your university account. Keeping them out of
OneDrive limits the blast radius but doesn't remove it.

**The listener only runs while you're logged in.** It starts at logon and restarts itself if
it crashes, but a locked or sleeping laptop answers no commands. `watch.log` records every
start, so a listener that keeps dying is visible there.

**Terms of service.** This logs in as you, with your own credentials, at roughly human
frequency, and reads only pages you're entitled to read. That's defensible for personal use.
Don't hammer it, don't share `auth_state.json`, don't run it for other people.

**No due-date parsing yet.** `fetch_assignments.py` returns raw text, so nothing here knows
what "due 5 Sep 23:59" means as a date. No calendar integration until that changes.

---

## Improvements, in the order worth doing them

Done: due-date parsing, assignment tracking by id, moved-deadline detection, `due` listing.

1. **Deadline reminders.** The obvious next step now that due dates are parsed: ping at
   T-24h and T-2h. More useful than "a new assignment exists", which you'd often have
   noticed anyway. Needs a little state so it fires once per assignment per threshold.
2. **Submission status.** Visit each open assignment's worksheet page and read whether
   something is attached, so reminders can skip what you've already handed in. The reason
   this isn't done: ~20 extra page loads per run. Only worth it for *open* assignments.
3. **Calendar.** Generate `.ics` from the parsed deadlines. Either import manually, or host
   the file and subscribe Google Calendar to the URL so it stays live.
4. **Dead-man's switch.** If no successful run in ~3 hours during active hours, alert. Right
   now a crashed task looks exactly like a quiet week.
5. **Announcement bodies.** Only link text is compared, so an edited announcement is
   invisible. Fetch and diff the body.
6. **Move off the laptop.** A Raspberry Pi or free-tier VM fixes the awake-only and
   overnight-gap problems at once. The blocker is credential storage — you'd be putting a CU
   password on a box you have to secure properly. Worth doing only with real secret handling.
7. **Per-course mute.** One course that posts constantly will train you to ignore the
   notifications, which defeats the point.
8. **Auto-relogin.** If MFA can be skipped on a trusted device, attempt a headless re-login
   before giving up. Would close the biggest reliability hole — if it's possible at all.
   Verify before building.

---

## Layout

```
mcvPushNoti/
├── watch.py                     the watcher and the Telegram listener
├── mcvclient.py                 login + scraping (vendored; no skill dependency)
├── watch.bat                    manual runner (finds a venv for you)
├── test_diff.py                 offline checks, no network (94 assertions)
├── courses.json                 course code -> the name you actually use
├── notify_config.example.json   template — the real one goes in the data dir
├── requirements.txt
├── .gitignore
├── README.md
├── watch.log                    created on first run
├── notify_state.json            last-notified time, Telegram update offset
└── scrape.lock                  present only while a crawl is running
```

