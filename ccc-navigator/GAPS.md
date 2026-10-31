# Navigator and Co-pilot: goal and open gaps

Updated 2026-10-31. Percentages are a judgment, not a measurement.

## Goal

One place (the Navigator) to see and move everything, plus a Co-pilot that keeps Uri on top of all projects, moves things, and eventually gives orders for him.

## Where we are

| Part | Done | Gaps |
|---|---|---|
| One place | ~80% | G1, G2, G3, G4 |
| Co-pilot keeps Uri on top | ~50% | G5, G6, G7, G8, G9 |
| Co-pilot gives orders | ~10% | G10, G11, G12 |

## Gaps

**One place**
- **G1** Drag and drop to reorganize channels and projects is not built (announced, not started).
- **G2** Never tested from Uri's phone: layout, Co-pilot chat, and how fast a new version reaches it (about 15 minutes).
- **G3** Switching the Co-pilot has no real setting: only a session menu item and `nav.py copilot --set`.
- **G4** Sorting still has known bugs (B5, B8) and issue #14 steps 2 to 5 are not started.

**Keeps Uri on top**
- **G5** A Co-pilot outage is invisible. Check-ins failed for about 30 hours (no credits, then no network) and nothing told Uri; the errors just piled into the chat. The back-off stops the pile-up but still does not alert.
- **G6** Uri's answers are the bottleneck: 20 decisions open. The ranked "top 7" exists only in a chat message, not as a view in the inbox.
- **G7** Check-ins use plan credits (up to about 34 a day) with no budget or cap, and credits ran out once.
- **G8** Check-ins depend on the Mac being awake and online. A check-in that started at 10:06 after sleep ran 52 minutes and failed.
- **G9** A real reply to a chat message is verified on the Mac only; not from the phone.

**Gives orders**
- **G10** No written rules for what the Co-pilot may do without asking Uri. Today it only closes verifiably finished decisions.
- **G11** It does not message or nudge worker sessions, start jobs, or unblock agents on its own.
- **G12** The Co-pilot row cannot be reached by keyboard or screen reader (minor).
