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
