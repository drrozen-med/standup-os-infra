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
