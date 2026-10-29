# QA brief: CCC Navigator, full click-through (desktop + mobile)

You are the QA tester. Goal: try every user journey and every button of the CCC Navigator
web app, on desktop and phone sizes, and report what works and what is broken. **Do not fix
code.** Report only.

## The app under test (a sandbox, already running)

- Sandbox URL: **http://127.0.0.1:8096** (if it is down: `qa/sandbox.sh stop; qa/sandbox.sh`).
- Code: `ccc-navigator/` (navigator.py server, static/index.html UI, static/chat.js, remote.py,
  NAV_CLI.md describes features). Read NAV_CLI.md first.
- The sandbox is safe to click: every write to CCC (send message, rename, stop, inject) is faked
  and logged to `qa/run/blocked-writes.log`; state is a copy at `qa/run/state.json`; Pi sends run
  `/usr/bin/true`. Use the log and the state copy to verify that a button sent the right request.

## Hard rules

1. **Never use http://127.0.0.1:8095, :8090, or https://cmd-m110rk.vera.baby.** Those are the
   owner's real system. Only :8096.
2. **Never rename a workspace (group).** That one action writes straight to Orca for real.
   Renaming sessions and projects is fine. Note "workspace rename: not tested (unsafe in sandbox)".
3. Don't edit anything outside `qa/`. Don't commit, push, or open PRs.
4. Don't use the owner's Pixel phone or adb.

## Tools

- Desktop browser: `cdp-cli launch --port 9223` then `cdp-cli new http://127.0.0.1:8096/`,
  `cdp-cli snapshot|click|fill|key|eval|screenshot "<page>"`. (`agent-browser` is an alternative.)
- Phone size in Chrome: use CDP device emulation (`Emulation.setDeviceMetricsOverride`
  375x812, mobile:true, plus touch) via `cdp-cli eval` or agent-browser's device option.
- iOS Simulator (real mobile Safari): `xcrun simctl boot "iPhone 17 Pro"; open -a Simulator;
  xcrun simctl openurl booted http://127.0.0.1:8096/`; screenshots with
  `xcrun simctl io booted screenshot qa/shots/<name>.png`. Use it at least for layout, the
  single-pane flow, the ☰ drawer and Add to Home Screen look. Shut it down at the end
  (`xcrun simctl shutdown booted`).
- Save every screenshot under `qa/shots/`.

## Journeys to cover (each: steps, expected, actual, PASS/FAIL, screenshot)

Desktop (wide window) and phone (375px) for each where it applies:

J1 Load: list renders, projects → workspaces → sessions, icons, counts, no console errors.
J2 Search/filter chips (incl. "Needs you"), sort, the Overview line + eye toggle.
J3 Open a session: transcript loads, scrolls, long messages, back button on phone.
J4 Composer: send a message to a Claude, a Codex, and a Pi session; check the blocked-writes
   log (or Pi run status) got it; check the UI feedback/toast.
J5 Session ⋯ menu: every item (rename, star/unstar, hide, icon, move, note, mark/triage, popout,
   copy, etc.). Verify in `qa/run/state.json` or the log.
J6 Workspace ⋯ menu: every item except rename — "Show all sessions here (incl. hidden)" (focus
   banner, Back, #g= link), "Copy link to this workspace", hide group.
J7 Project ⋯ menu: rename project, hide project, icon.
J8 Hide and Undo: hide a session, a workspace, a project; ☰ → Recently hidden → Undo each;
   "Show what I hid"; toast Undo.
J9 ☰ drawer: version, last check, Check for update, Update now, background runs list.
J10 Notes and triage pills: add/remove a note, set and clear a mark, see pills update.
J11 Remote gate (phone access security), against :8096 using a fake host header:
    - `curl -H 'Host: nav-sandbox.test' http://127.0.0.1:8096/api/tree` → 401, no data.
    - same for `/`, POST `/api/hide`, `/api/pi/send`, `/api/remote/pair` → 401/403.
    - make a pairing code: `python3 -c` using `remote.Store('qa/run/remote').create_pairing()`
      (import from ccc-navigator/), then GET `/__pair?code=…` shows a button only, POST spends
      it, sets cookie, second use 403, 6 bad codes → 429, foreign Origin POST → 403,
      remote responses carry the security headers. Local (no Host override) needs no cookie.
J12 Responsiveness: rotate/resize, keyboard over composer on iOS sim, tap targets ≥ 36px,
    no horizontal scroll at 375px.
J13 Errors: stop the proxy (`kill $(cat qa/run/proxy.pid)`) and see how the UI behaves when
    CCC is down; restart with `qa/sandbox.sh` afterwards.

Also click every other visible button/link you find that is not listed, and list it.

## Deliverable

Write `qa/REPORT.md`:
1. TL;DR: counts of PASS / FAIL / not tested, and the top 5 problems by severity.
2. Table: ID, journey, desktop result, phone result, iOS sim result, notes.
3. Bugs: B1, B2… each with steps to reproduce, expected, actual, screenshot path, severity
   (blocker / major / minor / cosmetic), and the file/function you suspect (no fix).
4. Every button found, with tested yes/no.

When done, set the Orca workspace comment:
`orca worktree set --worktree active --comment "QA done: <n> pass / <n> fail" --json`
and stop the sandbox (`qa/sandbox.sh stop`).
