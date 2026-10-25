---
name: standup-nav
description: Read this before working on or operating the CCC Navigator. Explains why it exists, what Uri actually wants from it (see only what matters, filter signal from noise), the rules that protect his curation, and how to drive the nav.py CLI. Use when asked to sort, hide, star, move, rename, audit or curate sessions across Claude/Devin/Codex/Pi/Orca.
---

# CCC Navigator — read this first

## Why this exists

Uri runs many coding agents at once — Claude Code, Codex, Devin, Cursor, Pi, OpenCode — across many
products, most of them inside Orca worktrees. Claude Command Center (CCC) already collects every one of
those sessions, but it lists them by *working folder*, so every worktree becomes its own "project" and
the real work is buried under headless runs, title-generation noise and abandoned experiments.

The Navigator is a separate small web app that reads CCC and shows the same sessions the way Uri
actually works: **project → group → session**. It never writes to CCC's or Orca's own data.

## What Uri wants from it

The reason this tool was built is not browsing. It is **a single place to manage everything that
matters, and to see only that**. Cleaning up — hiding and starring — is roughly 80% of the point.
Everything else is in service of that.

That makes these the things that actually matter, in order:

1. **Signal over noise.** The default view should be the few projects he is working on now, not 343
   folders. Stale work should be able to leave the list without being destroyed.
2. **Nothing he curated is ever lost.** Stars, hides, names and groups are his decisions. They must
   survive restarts, bad writes and automatic changes. Hiding is reversible; deleting is not possible.
3. **The list should maintain itself.** He should not have to do a cleanup pass every week. Work that
   goes quiet leaves; work that comes back returns on its own.

## The model to hold in your head

- **Project** = the main git repo. Worktrees and Orca workspaces fold into their parent.
- **Group** = a folder inside a project: an Orca workspace name, or one he filed by hand.
- **Session** = one conversation from one engine.

Three actions, at every level: **star** (keep, sort first), **hide** (tombstone, reversible),
**rename** (writes through to CCC, and to Orca for workspace groups).

## Rules that are not negotiable

- **Never delete.** There is no delete command on purpose. Anything you can do with this CLI can be undone.
- **Never hide something that is starred.** Starred means "this matters to me". Batch operations skip
  starred items unless `--include-starred` or `--starred` is passed explicitly.
- **Dry-run first, always.** Every batch mutation takes `--dry-run`. Run it, read the reported keys,
  only then run it for real.
- **Batch commands need a real filter.** `--limit` alone is not a filter. Use `--repo`, `--group`,
  `--engine`, `--match`, `--live`, `--starred` or an age such as `--older 14d`.
- **Hides are tombstones that rescue themselves.** A hidden session, group or project returns on its own
  when it is running again or waiting on Uri. So hiding is safe — it is not a statement about the future.

## How to use the CLI

Run from the `ccc-navigator/` folder. Navigator must be running (`http://127.0.0.1:8095`).
Default output is JSON; `--table` is for humans.

```bash
python3 nav.py status                      # is everything up?
python3 nav.py audit                       # per project/group: sessions, stars, hides, newest age
python3 nav.py curate                      # what is noise? propose hides (writes nothing)
python3 nav.py curate --older 2d --apply   # do it
python3 nav.py ls --groups --repo standup  # one project's groups
python3 nav.py hide --repo standup --match scratch --dry-run
python3 nav.py hide-group --repo standup --group scratch
python3 nav.py star --repo standup --group infra
python3 nav.py move --repo standup --match "billing" --to infra
python3 nav.py rename session claude:1234 "Billing fix"
```

| Command | What it does |
|---|---|
| `ls` | sessions, or `--groups` / `--repos` |
| `curate` | propose (or `--apply`) hiding of stale work |
| `hide` / `unhide` | sessions |
| `hide-group` / `unhide-group` | one group (`--repo` + `--group`) |
| `hide-repo` / `unhide-repo` | one project (`--repo`) |
| `star` / `unstar` | sessions; `--star-active` on `curate` stars live groups |
| `move ... --to GROUP` | file sessions into a group |
| `rename session\|group\|repo` | write-through naming |
| `nick KEY NAME` | rename one session in the navigator only |
| `icon --set EMOJI` | project/group icon |
| `audit` | plan a cleanup before you make one |
| `status` | navigator + CCC health |

Filters shared by most commands: `--repo`, `--group`, `--engine`, `--older AGE`, `--newer AGE`,
`--live`, `--match TEXT`, `--starred`, `--limit N`. Ages look like `30m`, `6h`, `14d`, `2w`.

## A safe session

```bash
python3 nav.py status
python3 nav.py curate --older 2d            # read the proposal
python3 nav.py curate --older 2d --apply    # only if the keys look right
```

If something looks wrong afterwards, the state is backed up before every write:
`~/.ccc-navigator/state.json.bak` and `~/.ccc-navigator/backups/state-YYYYMMDD.json`.
