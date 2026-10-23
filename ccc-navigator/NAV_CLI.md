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

`ls`, `hide`, `unhide`, and `move` accept `--repo SUBSTR`, `--group SUBSTR`,
`--engine ENGINE`, `--older AGE`, `--newer AGE`, `--live`, `--match TEXT`, and
`--limit N`. Ages accept forms such as `14d`, `6h`, `30m`, and `2w`. Hidden
sessions are excluded unless `--hidden` or its alias `--include-hidden` is
supplied. `unhide` targets hidden sessions by default.

## Commands

```bash
# Hide or unhide; at least one selector is required.
python3 nav.py hide --repo standup --group scratch --dry-run
python3 nav.py hide --repo standup --group scratch
python3 nav.py unhide --engine codex --older 14d --dry-run

# Move sessions to a group; --dry-run is also available.
python3 nav.py move --repo standup --match release --to infrastructure --dry-run
python3 nav.py move --repo standup --match release --to infrastructure

# Rename one session, list a repo's groups, or show service status.
python3 nav.py nick claude:1234 "Billing fix"
python3 nav.py groups --repo /Users/me/projects/standup
python3 nav.py status --table
```

Connection failures print a JSON error to stderr and exit non-zero.
