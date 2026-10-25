---
name: standup-nav
description: use when asked to sort, hide, move, rename or list the user's Claude/Devin/Codex sessions in the navigator
---

# Standup Navigator

Use `python3 ccc-navigator/nav.py` to operate the running navigator's JSON API.

- Always run a batch mutation with `--dry-run` first, inspect the reported count and keys, then rerun it without `--dry-run`. This applies to `hide`, `unhide`, `move`, `star`, `unstar`, `hide-group`, `unhide-group`, `hide-repo`, `unhide-repo` and `icon`.
- Never delete anything. This CLI intentionally has no delete operation; hiding is reversible with `unhide`.
- Starred sessions are never hidden or moved by a batch operation unless `--include-starred` is passed, or they are selected explicitly with `--starred`. Check the `skipped_starred` list in the result before assuming a batch was complete.
- Batch operations need at least one filter; `--limit` alone is not a filter. This covers `hide`, `unhide`, `move`, `star`, `unstar`, `hide-group`, `unhide-group` and `hide-repo`. Prefer targeted filters such as `--repo`, `--group`, `--engine`, `--match`, `--live`, `--starred`, or an age filter instead of listing every session key.
- Use `nick KEY NAME` for a session rename, `rename session|group|repo ...` when the name must reach CCC or the Orca workspace, and `move ... --to GROUP` for sorting.
- Audit before you curate: `audit` summarises every project and group (sessions, starred, hidden, newest activity, group ages), and `ls --groups` / `ls --repos` list the exact groups and projects with their session counts, hidden flags and star counts.
- Use `groups --repo REPO` to discover a repo's exact key and groups; `hide-group` needs both `--repo` and `--group`, `hide-repo` needs `--repo`.
- Default output is JSON. Use `--table` only when the user asks for a human-readable view.

Example safe batch workflow:

```bash
python3 ccc-navigator/nav.py audit
python3 ccc-navigator/nav.py hide --repo standup --group scratch --dry-run
python3 ccc-navigator/nav.py hide --repo standup --group scratch
```
