# Navigator CLI

`nav.py` is a stdlib-only client for a running CCC Navigator. It defaults to
`http://127.0.0.1:8095`; use `--url URL` or `CCC_NAV_URL` to target another
instance. Every command prints JSON by default; add `--table` for human output.

```bash
cd ccc-navigator
python3 nav.py status
python3 nav.py ls --engine claude --live --table
python3 nav.py ls --repo standup --group infra --match deploy --limit 20
```

## Filters

`ls`, `hide`, `unhide`, `move`, `star` and `unstar` accept `--repo SUBSTR`,
`--group SUBSTR`, `--engine ENGINE`, `--older AGE`, `--newer AGE`, `--live`,
`--match TEXT`, and `--limit N`. Ages accept forms such as `14d`, `6h`, `30m`,
and `2w`. Hidden sessions are excluded unless `--hidden` or its alias
`--include-hidden` is supplied. `unhide` targets hidden sessions by default.

`--starred` narrows a selection to the sessions the user has starred
(pinned). It is available on `ls`, `hide`, `unhide`, `move` and `star`.

`--include-starred` lets a batch operation also touch starred sessions.
Without it, `hide` and `move` skip starred sessions and report them under
`skipped_starred`, so a starred session is never hidden by accident. Selecting
them explicitly with `--starred` counts as that opt-in, so `hide --starred`
still acts on them.

`star` and `unstar` look at hidden sessions too, so a hidden session can be
starred or unstarred without extra flags.

Batch commands (`hide`, `unhide`, `move`, `star`, `unstar`, `hide-group`,
`unhide-group`, `hide-repo`, `unhide-repo`) need at least one filter:
`--limit` on its own is not a filter.

## Sessions

```bash
# Hide or unhide; at least one selector is required.
python3 nav.py hide --repo standup --group scratch --dry-run
python3 nav.py hide --repo standup --group scratch
python3 nav.py unhide --engine codex --older 14d --dry-run

# Move sessions to a group; --dry-run is also available.
python3 nav.py move --repo standup --match release --to infrastructure --dry-run
python3 nav.py move --repo standup --match release --to infrastructure

# Star and unstar; --dry-run is also available.
python3 nav.py star --repo standup --group infra --dry-run
python3 nav.py star --repo standup --group infra
python3 nav.py unstar --repo standup --group infra --dry-run

# Rename one session, list a repo's groups, or show service status.
python3 nav.py nick claude:1234 "Billing fix"
python3 nav.py groups --repo /Users/me/projects/standup
python3 nav.py status --table
```

## Groups and projects

Groups and whole projects have their own navigator keys, so they can be hidden
without touching one session at a time. A group key is
`g:<repo key>|<group name>` and a project key is `r:<repo key>`; the commands
below resolve a substring to those keys for you.

```bash
# Inspect first: every group with its session count, hidden flag, star count,
# newest activity and Orca workspace path.
python3 nav.py ls --groups --repo standup --table
python3 nav.py ls --repos --table

# Hide or unhide a group (both --repo and --group are required).
python3 nav.py hide-group --repo standup --group scratch --dry-run
python3 nav.py hide-group --repo standup --group scratch
python3 nav.py unhide-group --repo standup --group scratch

# Hide or unhide a whole project (--repo is required).
python3 nav.py hide-repo --repo standup --dry-run
python3 nav.py hide-repo --repo standup
python3 nav.py unhide-repo --repo standup --dry-run

# Set or clear the emoji icon of a group or a project.
python3 nav.py icon --repo standup --group scratch --set "🧹" --dry-run
python3 nav.py icon --repo standup --set "🚀"
python3 nav.py icon --repo standup --group scratch --clear
```

Hiding a group or a project also hides the sessions inside it, and it keeps
working for sessions that show up later.

```bash
# Unhide every hidden session in a project, or in one of its groups (all time).
python3 nav.py unhide-all --repo shared-marketing
python3 nav.py unhide-all --repo shared-marketing --group main

# File sessions under another project than their folder's (e.g. work for
# shared-marketing that was started in another checkout). --back undoes it.
python3 nav.py move-project --key claude:<uuid> --key claude:<uuid> --to shared-marketing
python3 nav.py move-project --repo sleepy --match outreach --to shared-marketing --dry-run
python3 nav.py move-project --key claude:<uuid> --back
```

A moved session lands in the project's Ungrouped list (use `move` to put it in a
group). Starred sessions are skipped by filters, but an explicit `--key` is moved.

```bash
# Claude desktop sidebar groups decide the project of their sessions.
python3 nav.py desktop-groups --table
python3 nav.py map-desktop-group "BoadsBridge" --to boardsBridge
python3 nav.py map-desktop-group "CFO, Bookkeepping" --clear   # shows as its own project
```

Placement order: an explicit `move-project`, then the desktop group (its mapped
project, or a project named after the group), then the folder. In the UI:
Menu → Claude desktop groups….

```bash
# The Co-pilot button: one tap opens Uri's main assistant session in Claude
# (claude.ai/code on the phone; needs Remote Control on for that session).
python3 nav.py copilot                                   # show
python3 nav.py copilot --set local_<uuid>                # or claude:<uuid>
python3 nav.py copilot --clear
```

In the UI: a session's ⋯ menu → Make this my co-pilot.

## Renaming

`rename` writes the new name through to the tool that owns it: a session
rename reaches CCC, a group rename reaches its Orca workspace when the group
has one (otherwise the navigator's own group list), and a project rename
updates the navigator's display name.

```bash
python3 nav.py rename session claude:1234 "Billing fix"
python3 nav.py rename group --repo standup --group infra "Infrastructure"
python3 nav.py rename repo --repo standup "Standup OS"
```

The result reports which backend applied the name (`applied`), for example
`["navigator", "ccc"]` for a session.

## Audit

`audit` returns one JSON summary meant for planning a cleanup: per project the
number of sessions, the starred ones, the hidden ones, the newest activity,
and the same numbers per group including the age of the newest session in
days. Projects are sorted by newest activity first.

```bash
python3 nav.py audit
python3 nav.py audit | head -c 600
python3 nav.py audit --table
```

A safe cleanup workflow:

```bash
python3 nav.py audit
python3 nav.py ls --groups --repo standup --table
python3 nav.py hide-group --repo standup --group scratch --dry-run
python3 nav.py hide-group --repo standup --group scratch
```

## Curate

`curate` is the one command for cleaning up the list: it works out what is
noise and proposes what to hide, at project, group and session level.

Something is noise when it has been quiet for longer than `--older`, has no
running process, is not waiting on the user, is not starred, and has no Orca
agent that spoke inside that window. Everything else is signal and is left
alone. A starred project, group or session is never proposed, and one that is
proposed protects everything below it.

```bash
python3 nav.py curate                   # proposal only, nothing changes
python3 nav.py curate --older 2d        # quiet for over two days counts as stale
python3 nav.py curate --older 2d --apply
python3 nav.py curate --older 2d --apply --star-active   # also star the live groups
```

The proposal is one JSON object with `hide_repos`, `hide_groups`,
`hide_sessions` and `star_candidates`, each entry naming its exact key and the
reason it was chosen. `--apply` posts the keys to the navigator and sets
`applied` to true. Without `--apply` nothing is written.

`--star-active` stars groups that are live now or have an Orca agent attached,
so the things you are working on stay pinned to the top.

## Auto-resurface

Hides are tombstones, but they do not stay buried: a hidden session, group or
project comes back on its own as soon as it is running again or waiting on the
user. Set `CCC_NAV_RESURFACE=0` to turn that off.

Connection failures print a JSON error to stderr and exit non-zero.

## Titler

`titler.py` is a standalone background worker (python3 stdlib only) that gives
every visible session a short, sensible name. It runs server-side, not in the
UI: it asks the navigator for the sessions it is already showing, flattens
each transcript into a short excerpt, asks a small model for a two-to-five
word title, and writes the title back through the navigator's rename
endpoint. The sidebar then shows names instead of truncated prompts.

**Privacy: by default nothing leaves your machine.** Sessions can hold medical,
business and personal content. Out of the box the titler builds names locally
from the first message (rule-based, no network). An outside model is used only
if you opt in with `TITLER_ALLOW_EXTERNAL=1` or point `TITLER_CMD` at a command
you chose. Emails, links, phone numbers, long ID numbers and API keys are
removed from any excerpt before it is handed to a model.

```bash
cd ccc-navigator

# One pass over the visible sessions (this is also the default).
python3 titler.py

# See what it would name, without touching anything.
python3 titler.py --dry-run --max 2

# Keep it running: a cycle every ten minutes.
python3 titler.py --loop
python3 titler.py --loop 300
```

Options:

- `--once` runs a single cycle and exits (default).
- `--loop [SECONDS]` keeps running, sleeping between cycles (default 600
  when the value is omitted). When the navigator cannot be reached the worker
  backs off for five minutes instead of hammering it.
- `--dry-run` prints the proposed titles as JSON and changes nothing: no
  rename is posted and the state file is not written.
- `--max N` renames at most N sessions per cycle (default 20). Sessions that
  cannot be titled (too few messages, no growth, a failed model call) do not
  use up that budget.
- `--retitle` also reconsiders sessions the worker named before, but only
  when their transcript grew by 40% or more since it named them.
- `--url URL` points the worker at another navigator (default
  `http://127.0.0.1:8095`, env `CCC_NAV_URL`).

Every cycle prints one JSON line with the visible count, the candidates, the
titles that were applied (`proposals`), the sessions that were skipped and
why, and any errors. The exit code is 0 even when individual sessions fail.

Safety rules the worker follows:

- only sessions the navigator already shows are considered, so hidden
  sessions are never touched, and batched sessions (several runs sharing one
  prompt) are skipped;
- a title the user typed by hand is NEVER overwritten -- the worker only
  re-titles sessions it named itself;
- transcripts are truncated to the first user message plus the last three
  messages, and obvious secrets and personal data (API keys, `Bearer` tokens,
  long hex or base64 blobs, emails, links, phone numbers, long digit runs) are
  stripped before an excerpt is handed to any model;
- a reply is accepted only when it is a single line of at most 7 words and 40
  characters, with markdown, quotes and trailing punctuation removed.

When the outside model is enabled it is called through `droid exec -m
custom:MiniMax-M3-direct --auto low '<prompt>'`. Whether that lane is
non-training has not been verified, so it stays off unless you opt in.
`TITLER_CMD` replaces that command, with the prompt passed as the last
argument, which is how the tests run it against a stub:

```bash
TITLER_CMD="python3 my_stub.py" python3 titler.py --dry-run
```

The worker remembers what it named in `~/.ccc-navigator/titler.json` (one
entry per session with the title, a timestamp and the event count at the
time). `TITLER_STATE` points that file somewhere else. The file is written
atomically.

## Talking to sessions

```bash
python3 nav.py read <session> --last 10 --table      # latest messages, plain text
python3 nav.py send <session> "message" --wait 90    # deliver through CCC, wait for the reply
```

`<session>` is a key (`claude:<id>`), a session id, or a unique id prefix. CCC
refuses sessions whose folder it does not know (`repo_not_allowed`).

**Pi sessions** work the same way. Pi has no remote input, so the navigator
resumes the session file headlessly (`pi --session <file> -p "<text>"`); the
reply is appended to the session and shows in `nav read` and the chat pane
(about 30-60 s per turn). Each run is logged in `~/.ccc-navigator/pi-runs/` and
Pi runs its tools without approval prompts, like any message sent through CCC.
If a Pi terminal is open in the same folder the result carries a `warning`: that
terminal will not show the exchange until it is reopened.

## Notes

```bash
python3 nav.py note <session> "decide pricing after the ASO check"
python3 nav.py note <session> "checked, still waiting" --by copilot
python3 nav.py notes <session> --table      # numbered list
python3 nav.py notes                        # every session that has notes
python3 nav.py note <session> --rm 2        # delete note 2
```

Notes live in `state.json` (`notes`), show as ✎ N on the row (hover for the
latest), and in the row's ⋯ menu (Add note, Notes).

## Decisions: what only Uri can decide

The **Decisions** chip (first in the header, also on the phone) opens the inbox: every open question,
P1 first, then by deadline, each with its answer buttons. A tap records the answer (Undo in the toast,
Reopen in the Answered list). Questions come from Watchtower's `decisions.json` and from agents:

```bash
# An agent asks. With --session, the answer is sent back into that session as a message.
NAV_ACTOR=copilot python3 nav.py ask --question "Deploy the CRM fix now?" --option deploy --option hold \
    --lane gtm-crm --priority 1 --deadline 2026-11-01 --session claude:<uuid>

python3 nav.py decisions --table              # open questions
python3 nav.py decisions --answered --table   # recent answers (agents read these)
python3 nav.py decide crm-fix-1a2b3 hold --note "after the duplicate review"
python3 nav.py decide crm-fix-1a2b3 --reopen
```

Answers live in `~/.ccc-navigator/answers.json` (Watchtower's file is never edited; its brief drops
answered ones). Every ask, answer and reopen is in `~/.ccc-navigator/changes.jsonl` with its actor
(`$NAV_ACTOR` for the CLI, `you` for the UI and the phone).

## Triage (live status of every session)

`triage.py` labels every visible session from local rules only (nothing leaves
the machine): `working` (running now), `needs-you` (approval waiting, or its
last message asks you something), `blocked`, `done`, `idle`, `stale` (quiet
over 3 days). It re-reads a transcript only when the session changed. The
launchd job `com.standup-os.triage` runs it every two minutes
(log `/tmp/standup-triage.log`).

```bash
python3 nav.py triage --table                       # run a pass now, show it
python3 nav.py triage --table --only needs-you,blocked
python3 nav.py mark <session> done --reason "shipped"   # your label; the sorter never overrides it
python3 nav.py mark <session> clear                     # hand it back to the sorter
python3 nav.py brief --table                            # the live snapshot, per project
```

The "Needs you" chip counts the sorter's `needs-you` too. The row shows the
label (hover for why); idle and working are not labelled to keep rows quiet.

## Workspaces: show everything, share a link

In the sidebar, a workspace's ⋯ menu has two extra entries:

- **Show all sessions here (incl. hidden)** switches the list to that one
  workspace, all time, hidden sessions included (dimmed), so you can hunt for a
  lost terminal. **Back to normal view** returns.
- **Copy link to this workspace** copies a link such as
  `http://127.0.0.1:8095/#g=g:<project>|<workspace>&p=<orca path>`. Opening it
  lands on that workspace in the focused view.

An agent resolves a pasted link with:

```bash
python3 nav.py ws "<link>" --table      # project, workspace, path, every session (hidden too)
python3 nav.py ws "<link>"              # same as JSON
```

## Phone access (issue #5)

The navigator is reachable from the phone at `https://cmd-m110rk.vera.baby`
through a Cloudflare Tunnel (`nav-mac`, launchd `com.standup-os.nav-tunnel`).
The Mac opens no ports; the navigator still listens on 127.0.0.1 only.

Every request through the tunnel needs a paired session; without one it gets a
401 and nothing else. There is no password: pair a phone with a one-time link.

```bash
python3 nav.py remote pair --table      # one-time link: works once, expires in 15 minutes
python3 nav.py remote sessions --table  # paired phones
python3 nav.py remote revoke all        # sign every phone out (or pass an id prefix)
python3 nav.py remote status --table    # on/off, host, tunnel state
```

Open the link on the phone and press **Pair this phone** (just opening the link spends nothing, so
link previews can't use the code up). Then install it: Android Chrome ⋮ → Install, iPhone Share → Add to
Home Screen.
Sessions last 30 days. Pairing and revoking work only from the Mac itself.

Turn it off: `launchctl bootout gui/$(id -u)/com.standup-os.nav-tunnel` (the
URL stops answering), or remove `CCC_NAV_PUBLIC_HOST` from the navigator's
launchd plist. Remote requests are logged (no codes or cookies) in
`~/.ccc-navigator/remote-access.log`.

The Mac must be awake and online. A closed MacBook sleeps unless it is on power
with an external display.
