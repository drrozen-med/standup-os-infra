# Working agreement for agents in this repo

The owner (Uri) does not want to be asked at every step. In this repo you have standing authority to:

- branch, commit, push and open a PR;
- merge your own PR once the tests pass and the change has been checked in the running app (browser for UI changes);
- restart the navigator (`launchctl kickstart -k gui/$(id -u)/com.standup-os.navigator`) and other `com.standup-os.*` jobs;
- go straight on to the next step of an agreed plan (for example the steps of issue #14).

Report after the fact: what merged, what you verified, and what you could not verify.

Still stop and ask first for:

- anything irreversible or destructive (deleting data, force-push, rewriting history, clearing `~/.ccc-navigator` state);
- sending messages to people, or to real agent sessions;
- spending money;
- changing other repos or projects, or their CI;
- security changes to phone access (`remote.py`, the tunnel, pairing);
- choices between product directions the owner has not decided.

Commit messages end with the `Co-Authored-By` line the harness gives you; PR descriptions end with the "Generated with Claude Code" line.

Every navigator feature must work from both the UI and the CLI (`ccc-navigator/nav.py`, documented in `ccc-navigator/NAV_CLI.md`).
