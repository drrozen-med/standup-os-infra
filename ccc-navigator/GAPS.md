# Navigator and Co-pilot: goal, known bugs, work plan

Updated 2026-10-31. Percentages are a judgment, not a measurement.

## Goal

One place (the Navigator) to see and move everything, plus a Co-pilot that keeps Uri on top of all projects, moves things, and eventually gives orders for him.

| Part | Done | Gaps |
|---|---|---|
| One place | ~80% | G1, G2, G3, G4, D1-D4, B5, B8 |
| Co-pilot keeps Uri on top | ~50% | G5, G6, G7, G8, G9 |
| Co-pilot gives orders | ~10% | G10, G11, G12 |

## Known bugs and gaps

**Session state (the dots, times and "needs you")**
- **D1** Times lie. Navigator shows CCC's `modified`, which lags up to 17 h on active sessions (measured: 1015 min shown vs 5 min real). Project headers inherit it.
- **D2** Green means "a process exists", not "working". Claude desktop sessions keep their process after finishing, so they stay green; the sorter then labels every live session "working" without reading it.
- **D3** Two "needs you" signals that disagree: yellow dot (CCC approval/question card) and the sorter's "needs-you" tag (question in last message). The sorter re-checks only when CCC's (stale) time changes, so old labels stick.
- **D4** No red dot exists; red is only the "blocked" text tag.

**One place**
- **G1** Drag and drop to reorder projects/channels not built.
- **G2** Never tested on Uri's phone (layout, Co-pilot chat, update speed ~15 min).
- **G3** Switching the Co-pilot has no real setting (only a session menu item and `nav.py copilot --set`).
- **G4** Issue #14 (API v1: action registry, one ref format, change log with undo, capabilities) steps 2-5 not started.
- **B5** Renaming a group that shares its name with an Orca workspace renames nothing visible and leaves a phantom manual group (qa/REPORT.md).
- **B8** Footer "hidden by you [Show]" wraps badly at <= 402 px.

**Co-pilot keeps Uri on top**
- **G5** A Co-pilot outage is invisible. Check-ins failed ~30 h (no credits, then no network); nothing told Uri. Back-off stops the pile-up but does not alert.
- **G6** Uri's answers are the bottleneck (20 open). The ranked "top 7" exists only in a chat message, not in the inbox.
- **G7** Check-ins use plan credits (up to ~34/day) with no daily cap.
- **G8** Check-ins depend on the Mac being awake and online; one ran 52 min after sleep and failed.
- **G9** A real chat reply verified on the Mac only, not from the phone.

**Co-pilot gives orders**
- **G10** No written rules for what the Co-pilot may do without asking Uri.
- **G11** It does not nudge worker sessions, start jobs or unblock agents on its own.
- **G12** The Co-pilot row cannot be reached by keyboard or screen reader.

## Work plan

Coding is delegated to OpenCode Go (via Outsourcerer); the main session orchestrates, reviews, tests on the isolated copy (:8098, read-only CCC proxy) and merges. One work package = one branch = one PR. Packages that touch the same files run one after another.

| WP | Fixes | Files | Model | Order |
|---|---|---|---|---|
| WP1 Session state | D1-D4 | navigator.py, triage.py, static/index.html, tests | kimi-k3.1-pro | now |
| WP2 Check-in reliability | G5, G7, G8 | bin/copilot-checkin.py | glm-5.3 | now (parallel) |
| WP3 Small UI fixes | B5, B8, G3, G12 | navigator.py, static/index.html | glm-5.3 | after WP1 |
| WP4 Decisions "top" view | G6 | decisions.py, static/index.html | glm-5.3 | after WP3 |
| WP5 Drag and drop reorder | G1 | navigator.py, static/index.html | kimi-k3.1-pro | after WP4 |
| WP6 API v1 (issue #14) | G4 | new actions.py, navigator.py, nav.py | kimi-k3.1-pro | after WP5 |
| Phone check | G2, G9 | none (375 px emulation here, real phone by Uri) | main session | after WP3 |
| Authority rules | G10, G11 | draft for Uri's approval | main session | needs Uri |

### WP1 target behaviour (agreed 2026-10-31)

- **Time** = the last real user/assistant message time, read from the session transcript by the Navigator, not CCC's `modified`.
- **One state per session**, one rule feeding the dot, the Live / Needs you filters and the project header:
  - **Working** (green, pulsing): the agent is mid-turn now.
  - **Needs you** (yellow, sorted first): it asked a question or waits on an approval.
  - **Resting** (grey): finished within 24 h, nothing pending.
  - **Dormant** (faded): no activity for more than 24 h.
  - "Blocked" stays a red tag. Whether a process exists no longer sets the color.
- Project header shows counts ("2 working, 1 needs you") instead of one time.
