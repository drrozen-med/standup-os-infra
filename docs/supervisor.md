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
   Never wake when: the owner is working now (claude: session activity in the last 3 minutes, or a tool
   running; opencode/devin: its Outsourcerer job is running; a job over 90 min is treated as hung and
   cancelled), `state` is `waiting-on-uri` with nothing newly answered, the owner was woken in the last
   30 minutes, it already had 24 wakes today, or supervision is paused (globally or for that project).
   One Outsourcerer job per owner at a time. A failed launch is logged (cycles.log + owner `log.md`) and
   raises no alert unless 3 consecutive failures (then one loud alert).
   How a wake runs depends on the owner's **engine** (`claude` default, or `opencode`/`devin`; see
   "Engines"). A `claude` wake sends one message through the Navigator (`nav.py send <owner key> <text>`);
   an `opencode`/`devin` wake launches one Outsourcerer job (one git worktree per job) with the owner brief
   + wake text + engine constraints, and tracks `{job_id, started_at, engine}` in `state.json`. The text
   starts with `[Supervisor wake: <reason>]`, says what to do, and ends with "End with owner.py status set".
   Each wake appends `[supervisor] woke owner...` to the project's `log.md` (with engine/job for Outsourcerer).
   Then raise **loud alerts** for the few decisions that need Uri now (section "Loud alerts" below).
4. **Register**: rewrite `~/.ccc-navigator/supervisor/register.json`, one row per project:
   `project, owner_key, engine, job (id/state/started_at/status), state, summary, updated_at, stale,
   problem, last_shipped (text + when), days_since_shipped, decisions_open (count), oldest_decision_days,
   idle_checkins (consecutive cycles with nothing new in status), wakes_today, last_wake (reason + when),
   launch_failures, flags` (e.g. `idle-2x`, `no-status`, `blocked-past-due`, `decision-7d`). "Shipped"
   means a non-empty `shipped` list in a status write; track the last one per project in
   `~/.ccc-navigator/supervisor/state.json` (also holds per-owner job/engine and wake bookkeeping).
   Append one line per cycle to `~/.ccc-navigator/supervisor/cycles.log`: time, wakes done, skips with reason.
5. **Hand judgment to the AI supervisor**: the existing co-pilot check-in (`bin/copilot-checkin.py`)
   keeps its 15-minute schedule; its prompt changes to: read `digest.md` and the register first, act on
   `flags` (two idle check-ins: re-brief the owner or start it fresh from `owner.py brief`; decisions older
   than their deadline: chase Uri once with a default), and never re-do what the cycle already did.

Env: `SUPERVISOR_DIR` (default `~/.ccc-navigator/supervisor`), `OWNER_DIR`, `CCC_NAV_URL`,
`SUPERVISOR_DRY_RUN=1` (decide and log wakes, send nothing), `SUPERVISOR_ENGINE` (overrides the per-owner
engine for all owners; tests). Everything is atomic-write; a failing owner never stops the others; the run
always exits 0 after logging the error.


## Engines

Each owner runs on an engine, set in `~/.ccc-navigator/owners/<slug>/engine` (one line):
`claude` (default if missing) sends wakes via Navigator `nav send`; `opencode` / `devin` launch an
Outsourcerer job instead (`OSRC_REQUIRE_INTERACTIVE=0`, resolved from the latest plugin cache glob;
opencode: `--provider opencode bg edit -m opencode-go/iceman-contributor-free`; devin:
`--provider devin bg -m swe-2-high`), `cwd` = the project folder (`~/projects/<slug>` when present).
The prompt is `owner.py brief <slug>` + the wake text + the engine constraint (own git worktree, one
turn then `owner.py status set`, Outsourcerer-delegable tools only, no money, no messages to real
people). `SUPERVISOR_ENGINE` overrides all owners. `supervisor.py engine <project|all> <engine>` sets it.

Job state lives in `state.json` per owner (`job_id`, `started_at`, `engine`). Busy while
`outsourcerer.sh status` shows the job running; a job over 90 min is cancelled as hung; one job per
owner. Launch failures log to `cycles.log` and the owner's `log.md` as `[supervisor]`; 3 consecutive
failures raise one loud alert. The register/digest/status show engine and job state.

## Loud alerts

The escalation ladder's loud step (#79 rule 5). An alert turns every open Navigator page red until Uri attends
to it: a red bar above everything (phone too), a two-tone chime every 20 s (Web Audio, no files), the tab title
flashing `(!) <title>` and one system notification per alert. It stops on **I'm on it** (ack), on opening the
linked decision, or on answering that decision in the inbox; on other devices it stops at their next refresh.
Store, API and CLI: `ccc-navigator/NAV_CLI.md`, "Loud alerts".

Keep it rare, or it stops meaning anything. **Only the cycle raises alerts; owners never do** (an owner turns
its question into a decision with `nav.py ask`; the cycle decides whether it is loud). In step 3, after the
wakes, each cycle raises at most one alert per decision, for open decisions in Uri's inbox that are:

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

## Control

- `bin/supervisor.py cycle [--dry-run]`, `status` (register as a table), `wake <project> [--reason TEXT]`
  (manual wake, same caps), `engine <project|all> <claude|opencode|devin>`, `pause [<project>]`,
  `resume [<project>]`. Pause state lives in `supervisor/state.json`.
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
