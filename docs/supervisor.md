# Supervisor (masterplan #79, phase 2)

The supervisor keeps every project owner moving and keeps Uri's view current. Routine work is
code; judgment stays with the AI supervisor (the Super-co-pilot session). Owners: `docs/owners.md`.

## The 15-minute cycle: `bin/supervisor.py cycle`

Run by launchd `com.standup-os.supervisor` every 15 minutes. Skips 00:00-07:00 local and when paused.
One run, in order:

1. **Refresh** scoreboards (`bin/owner-scoreboard.py`, no GitHub calls of its own).
2. **Digest**: build `~/.ccc-navigator/supervisor/digest.json` and `digest.md` from code only:
   for every registered owner (`/api/tree` `owners`): project, owner session key, its live state from the
   Navigator (working / idle, last activity), `status.json` (state, summary, next, shipped, needs_uri,
   blocked, updated_at), stale/problem, scoreboard problems, the last 5 `log.md` lines, its decisions
   (`needs_uri`) with each one's status in the Navigator inbox and whether Uri answered it after the
   owner's `updated_at`; plus Uri's open decisions overall (count, oldest age). `digest.md` is the same,
   short, for the AI supervisor to read.
3. **Wake** owners by these rules (first match wins; reason is recorded):
   | Reason | When |
   |---|---|
   | `answered` | a decision in `needs_uri` was answered by Uri after the owner's last status. The wake message quotes Uri's answer verbatim (choice + note). |
   | `no-status` | the owner's last turn ended without a status (problem "no status this turn") or it has no status. |
   | `blocked-due` | `state` is `blocked` and `blocked.by` is today or past. Ask it to try again or escalate. |
   | `keep-moving` | `state` is `working` or `picking-next` and the session has been idle 30+ minutes. |
   Never wake when: the owner session is working now (activity in the last 3 minutes), `state` is
   `waiting-on-uri` with nothing newly answered, the owner was woken in the last 30 minutes, it already had
   24 wakes today, or supervision is paused (globally or for that project).
   A wake sends one message through the Navigator (`nav.py send <owner key> <text>`, same path the
   co-pilot check-in uses). The text starts with `[Supervisor wake: <reason>]`, says what to do, and ends
   with "End with owner.py status set". Each wake appends `[supervisor] woke owner: <reason>` to the
   project's `log.md`.
   Then raise **loud alerts** for the few decisions that need Uri now (section "Loud alerts" below).
4. **Register**: rewrite `~/.ccc-navigator/supervisor/register.json`, one row per project:
   `project, owner_key, state, summary, updated_at, stale, problem, last_shipped (text + when),
   days_since_shipped, decisions_open (count), oldest_decision_days, idle_checkins (consecutive cycles with
   nothing new in status), wakes_today, last_wake (reason + when), flags` (e.g. `idle-2x`, `no-status`,
   `blocked-past-due`, `decision-7d`). "Shipped" means a non-empty `shipped` list in a status write;
   track the last one per project in `~/.ccc-navigator/supervisor/state.json`.
   Append one line per cycle to `~/.ccc-navigator/supervisor/cycles.log`: time, wakes done, skips with reason.
5. **Hand judgment to the AI supervisor**: the existing co-pilot check-in (`bin/copilot-checkin.py`)
   keeps its 15-minute schedule; its prompt changes to: read `digest.md` and the register first, act on
   `flags` (two idle check-ins: re-brief the owner or start it fresh from `owner.py brief`; decisions older
   than their deadline: chase Uri once with a default), and never re-do what the cycle already did.

Env: `SUPERVISOR_DIR` (default `~/.ccc-navigator/supervisor`), `OWNER_DIR`, `CCC_NAV_URL`,
`SUPERVISOR_DRY_RUN=1` (decide and log wakes, send nothing). Everything is atomic-write; a failing owner
never stops the others; the run always exits 0 after logging the error.

## Loud alerts

The escalation ladder's loud step (#79 rule 5). An alert turns every open Navigator page red until Uri attends
to it: a red bar above everything (phone too), a two-tone chime every 20 s (Web Audio, no files), the tab title
flashing `(!) <title>` and one system notification per alert. It stops on **I'm on it** (ack), on opening the
linked decision, or on answering that decision in the inbox; on other devices it stops at their next refresh.
Store, API and CLI: `ccc-navigator/NAV_CLI.md`, "Loud alerts".

Keep it rare, or it stops meaning anything. **Only the cycle raises alerts; owners never do** (an owner turns
its question into a decision with `nav.py ask --kind ...`; the cycle decides whether it is loud). In step 3, after
the wakes, each cycle raises at most one alert per decision, for open decisions that are Uri's (see "The gate":
approvals going ahead by themselves and problems on the supervisor's desk are never loud) and that are:

| Reason | When |
|---|---|
| `P1` | the decision has priority 1. |
| `due today` / `overdue since <date>` | its deadline is today or past. |
| `blocks <project>` | it is in that owner's `needs_uri`, the owner's `state` is `blocked`, `blocked.unblocker` is `uri` and `blocked.by` is today or past. |

Every reason that applies goes into the alert body ("Why now: P1; blocks alpha. Options: A / B.") and the title
is "Decision for you: <question>". The alert links to the decision (Open shows it in Decisions) and its dedupe key
is `decision:<id>`. Not raised when: an alert with that key is open, it was acked less than 12 h ago (the
Navigator enforces both too), a paused project waits on the decision, or it is quiet hours (00-07) or the
supervisor is paused (no cycle at all). `--dry-run` decides but raises nothing. `digest.json` `alerts` and
`digest.md` list what was raised or held and why; `cycles.log` names the decisions raised. A failing raise never
stops the cycle.

## The gate

Uri's inbox holds only what is his. Every question says what kind it is when it is asked (`nav.py ask --kind`,
refused without one; old questions get a kind with `nav.py sort`). Rules, not a model, decide who holds it
(`ccc-navigator/decisions.py`; `holder` and `for_uri` on every open question in `GET /api/decisions`):

| Kind | Who holds it | What happens |
|---|---|---|
| `choice` | Uri | A real choice that is his. Waits for his answer, however long. |
| `errand` | Uri | Needs his own hands (his login, 2FA, a payment screen). Waits. |
| `approval` | nobody (`auto`) | The asker names a `default` and an `undo`. Listed under "Going ahead unless you object". At the end of its deadline day, and never sooner than 24 h after it was announced, the default is recorded as the answer (`by: default`, `auto: true`) and the asker acts on it. Listed for 7 days under "Went ahead without you" with its undo. |
| `problem` | the supervisor | Something broken or shared, with a `topic`. Every open question on one topic is ONE item (the others are `dupe`s of it); answering it answers them all. |

- **What never goes ahead by itself**, whatever kind the asker gives: money, messages to real people, clinical
  content, deleting data, accounts and access, legal terms. The words are checked when the question is asked or
  sorted (refused with `needs_uri`: ask it as a `choice`) and again when the default would be applied.
- **Uri always wins.** His answer before the deadline stops the default. "Undo it" afterwards replaces the default
  with the answer "undo this, roll it back" (with the asker's own undo plan) and tells the asker. A question he
  reopens comes back as a `choice`: it never re-arms (the kind is changed before the answer is removed, so a
  reopen cut short cannot leave a live default). Reopening a problem reopens every question its answer covered.
  A switch on the Decisions page (`nav.py defaults off`) stops every default; nothing is lost, the questions wait
  for him instead. A switch file that cannot be read counts as off.
- **Nothing hides.** The switch, the answer and the deadline are checked again, under one lock, at the moment a
  default is written. An approval still open an hour after its time (`LATE_S`) is Uri's again, marked "its default
  did not go ahead". One default that cannot be written does not stop the others (`failed` in the run's result),
  and the asker is told even when the change log cannot be written. A merged problem carries what every project
  asked (`dupes[].question`), so one answer is never given blind.
- **The desk.** A problem stays with the supervisor for two days. It becomes Uri's (`escalated` says why) when it
  is still open after two days, when its deadline has passed, or when no supervisor is on duty (`state.json` paused,
  or no cycle in the last 2 h). The cycle lists the desk in `digest.json` `desk` and at the top of `digest.md`;
  the co-pilot solves each problem and closes it with `nav.py decide <id> other --note "solved: ..."`.
- **The cycle** counts and alerts only what is Uri's: `decisions_overall` and the loud alerts skip `auto`,
  `supervisor` and `dupe` items. An owner's `needs_uri` may still name any open question.
- Every sort, default, undo and switch is in the change log (`nav.py changes`), and the Watchtower brief lists what
  is going ahead and what went ahead, with the undo for each.

CLI and API: `ccc-navigator/NAV_CLI.md`, "Decisions".

## Control

- `bin/supervisor.py cycle [--dry-run]`, `status` (register as a table), `wake <project> [--reason TEXT]`
  (manual wake, same caps), `pause [<project>]`, `resume [<project>]`. Pause state lives in
  `supervisor/state.json`.
- Navigator: `GET /api/supervisor` returns `{register, digest_at, paused, cycles_tail}`;
  `POST /api/supervisor` `{action: "pause"|"resume"|"wake", project?, reason?}` (remote/phone gate like other
  mutations). CLI: `nav supervisor [--table]`, `nav supervisor pause|resume [project]`,
  `nav supervisor wake <project>`.
- launchd plist template in `systemd/` style folder used by this repo (see existing plists in
  `~/Library/LaunchAgents/com.standup-os.*.plist`); the builder adds the template, Claude installs it.

## UI: the status board

A pinned row "Supervisor" next to "Super-co-pilot" and "Decisions" opens a screen in the main pane: one
row per project with the owner's state dot, summary, days since shipped, open decisions (and oldest age),
last wake (reason, how long ago), flags as small warning chips, and a 🧠 that opens the owner (same path
as the project row). Header: last cycle time, paused or running, a Pause/Resume button. Works at 375 px.

### Mac alarm

`bin/alert-alarm.py` (launchd `com.standup-os.alert-alarm`, every 30 s) makes the Mac itself ring and say the
newest alert out loud while any loud alert is open, independent of Safari. It stops when the alert is
acknowledged anywhere. Quiet hours 00:00-07:00. `alert-alarm.py mute [MIN]` / `unmute`. It plays at the Mac's
current volume (×3); it never changes system volume.
