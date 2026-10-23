---
name: standup-nav
description: use when asked to sort, hide, move, rename or list the user's Claude/Devin/Codex sessions in the navigator
---

# Standup Navigator

Use `python3 ccc-navigator/nav.py` to operate the running navigator's JSON API.

- Always run a batch mutation with `--dry-run` first, inspect the reported count and keys, then rerun it without `--dry-run`.
- Never delete anything. This CLI intentionally has no delete operation.
- Prefer targeted filters such as `--repo`, `--group`, `--engine`, `--match`, `--live`, or an age filter instead of listing every session key.
- Use `nick KEY NAME` for a rename and `move ... --to GROUP` for sorting. Use `groups --repo REPO` to discover a repo's exact key and groups.
- Default output is JSON. Use `--table` only when the user asks for a human-readable view.
- The `hide` and `unhide` commands require at least one selector; `--limit` by itself is not a selector.

Example safe batch workflow:

```bash
python3 ccc-navigator/nav.py hide --repo standup --group scratch --dry-run
python3 ccc-navigator/nav.py hide --repo standup --group scratch
```
