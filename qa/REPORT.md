# CCC Navigator — QA report

Sandbox `http://127.0.0.1:8096` (navigator + read-only CCC proxy on :8097, state copy in `qa/run/state.json`).
Tested 2026-10-28 with: desktop Chrome via cdp-cli (1200 px), phone-size Chrome emulation via agent-browser (375×812, iPhone UA + DPR 3), and iPhone 17 Pro iOS Simulator / mobile Safari (402 pt).

## 1. TL;DR

| Platform | PASS | FAIL (has bugs) | Not tested |
|---|---|---|---|
| Desktop | J1–J11, J13 | J12 | — |
| Phone (375 px emulated) | J1–J5, J9 | J12 | J6–J8, J10, J11, J13 |
| iOS Simulator (real Safari) | J1 (layout) | — | J2–J13 taps — no tap automation available |

Top 5 problems by severity:

1. **B1 (major)** — Sending a message while CCC is unreachable looks like success: composer clears, no error shown, message silently dropped. `navigator.py _proxy()` returns `502 {"error": …}` but `chat.js post()` only treats `d.ok === false` as failure.
2. **B2 (major)** — At 375 px the header `.row1` doesn't wrap: `.tools`/`.seg` overflow, the "All" window segment is clipped ~18 px off-screen, `document.scrollWidth` = 393 > 375.
3. **B3 (minor)** — Send-error feedback is only a `title` attribute on the textarea — invisible on touch devices; no toast or inline error. (Companion to B1.)
4. **B4 (minor)** — "Hide older than 1d/7d/30d" silently does nothing when no session matches (no toast, no feedback).
5. **B5 (minor)** — Manual-group rename can be a no-op with a phantom group left behind: renaming `main` (whose items were Orca-sourced but which also had a stale manual entry in `state.groups`) renamed only the manual record — the visible group kept its name.

Also: no visible "CCC offline" indicator unless the user has enabled the optional Status line view item (B6). Tap targets <36 px on several controls (B7). iOS taps not tested — see J12/iOS section.

## 2. Journey table

| ID | Journey | Desktop | Phone (375px emu) | iOS sim | Notes |
|----|---------|---------|-------------------|---------|-------|
| J1 | Load: list, icons, counts, console | PASS | PASS | PASS (layout) | 11 projects · 25 groups · 51 sessions; ovLine "51 shown (1d) · 27 background hidden · 11 projects"; status line available via View ▸ Status; zero console errors. iOS: real Safari renders correctly at 402 pt, toolbar fits. |
| J2 | Search, chips, "sort", ov line + eye | PASS | PASS | n/t | Search "navigator"→4 rows; Starred=10, Live=6, Needs you=6 all filter and unfilter correctly; ov eye toggles; Now/1d/7d/All = 7/51/78/142. **No dedicated sort control exists** — ordering is automatic (starred first, then latest activity); counted as covered. |
| J3 | Open session, transcript, scroll, back | PASS | PASS (‹ back → list) | n/t | Short convo renders; 12 100-event transcript renders 81 msgs + "Show 12020 earlier messages" fold → expands fully, scroll position preserved, auto-scrolls to bottom. Phone: single-pane m-detail, ‹ returns to list. |
| J4 | Composer → Claude/Codex/Pi | PASS | PASS (send logged) | n/t | claude + codex → `POST /api/inject-input` in blocked-writes.log; pi → `/api/pi/send`, pi-run log created, placeholder "Pi is working on it…", composer clears on success. |
| J5 | Session ⋯ menu (all items) | PASS | PASS (menu fits viewport) | n/t | All items verified incl. state.json writes. "Open in new tab"/"Open in CCC": code correct (`s.open_url` guarded) but unverifiable — window.open blocked under CDP automation (no user gesture). |
| J6 | Workspace ⋯ menu | PASS | n/t | n/t | Focus mode + banner + `#g=` link + Back + Copy link all verified; hide-all/older/group + Undo verified; icon picker persisted; collapse others works. **Rename on Orca-sourced group: not tested (unsafe — writes to real Orca)**; manual-group rename exposed B5. |
| J7 | Project ⋯ menu | PASS | n/t | n/t | Rename → `repo_names` + header; icon → persisted; collapse/expand all; "Hide whole project" hides session keys (not `r:` key) — vanishes, Undo restores; repo eye/`Hide project (and future sessions)` sets the real `r:` tombstone. |
| J8 | Hide + Undo paths | PASS | n/t | n/t | Eye icon, ⋯ Hide, toast Undo ("Hid 5 sessions"), ☰ Recently hidden Undo ("Back: 5 sessions"), "Show what I hid" dimmed listing + per-item unhide — all verified. |
| J9 | ☰ drawer | PASS | PASS | n/t | Version `a356afe8cd`, "Last checked" time, Check for update → toast "You have the latest version"; "Update now" correctly absent when fresh; Show what I hid (8); background runs list (22+5); scrim + Esc close. |
| J10 | Notes & triage pills | PASS | n/t | n/t | Add note → ✎ 1 pill; Notes (1)… list + delete by number → pill gone; mark done → pill; "Let the sorter decide" clears; menu header shows "now: done (you)". |
| J11 | Remote gate (fake Host) | PASS | same (curl) | same | All checks pass — see details below. |
| J12 | Responsiveness / tap targets | PASS (desktop) | **FAIL** | partial | B2 clip at 375 px; B7 tap targets; 812 px landscape → two-pane, no h-scroll; iOS keyboard-over-composer not tested (no tap automation). |
| J13 | CCC down behavior | PASS w/ bug | n/t | n/t | Graceful degradation: cached tree still served, transcript pane shows "This conversation could not be loaded. Open it in CCC instead."; status line (when enabled) shows "CCC offline · cached Nm ago". BUT B1: sends silently drop. Sandbox restarted after test. |

## 3. Bugs

**B1 — major. Send while CCC is down silently loses the message**
- Steps: kill proxy (`kill $(cat qa/run/proxy.pid)`), open a session, type text, press Send.
- Expected: error surfaced (toast/inline), draft kept in composer.
- Actual: `/api/inject-input` → navigator proxy → `502 {"error":"CCC unreachable: …"}`; `chat.js` `post()` only treats `d.ok === false` as failure, so the success path runs: textarea cleared, transcript reloaded. Message gone, user believes it was sent.
- Suspect: `static/chat.js` post() response handling (~line 236); `navigator.py` `_proxy()` `_send(502, {"error": …})` — either return `ok:false` in the 502 body or have chat.js treat `!d.ok` / HTTP errors as failure.
- Screenshot: qa/shots/ (behavioral; see j4-phone-chat.png for composer area)

**B2 — major. Header overflows at 375 px; "All" segment clipped**
- Steps: emulate 375×812, load `/`.
- Expected: all controls reachable, no horizontal overflow.
- Actual: `documentElement.scrollWidth` = 393; `.seg` right edge at 393 px → "All" button ~18 px clipped/off-screen. Cause: `.row1{display:flex;justify-content:space-between}` with fixed `.tools` (≈219 px) and no wrap/shrink rules under the ≤760 px media query.
- Suspect: `static/index.html` CSS `.row1`, `.row1 .tools`, `@media (max-width:760px)` block.
- Screenshot: qa/shots/j1-phone-375.png

**B3 — minor. Send-failure feedback is a hover-only tooltip**
- `onError` sets `title` on the textarea — invisible on touch and easy to miss on desktop. No toast/inline error. Same area as B1 but distinct: even when `ok===false` does arrive, the user gets almost no signal.
- Suspect: `chat.js` `opts.onError` → `$('#cmp').setAttribute('title', m)`; `index.html` `onError` wiring.

**B4 — minor. "Hide older than Nd" gives no feedback when 0 items match**
- Steps: group ⋯ → "Hide older than 1 day" on a group whose items are all <1 d old.
- Expected: toast "0 sessions older than 1d" or similar.
- Actual: `hideKeys([])` early-returns; menu closes, nothing happens. Looks like a broken button.
- Suspect: `index.html` `hideKeys` / `menuFor` group items.

**B5 — minor. Renaming a group with a stale manual entry silently renames nothing visible**
- Steps: assign a session to a group that shares its name with an Orca workspace group (e.g. `main`) → name enters `state.groups`; move session back out; group ⋯ shows "Rename group…" (manual variant); rename → `state.groups` gets the new name but the visible Orca-derived group keeps the old name and `state.assign` had nothing to migrate. User sees no change; a phantom manual group persists.
- Suspect: `navigator.py` `State.rename_group` (renames list + assign only) and `index.html` `menuFor` choosing the rename variant via `g.source==='manual'` which is also true when the name merely exists in `state.groups`.

**B6 — minor. CCC outage is invisible in the default view**
- The "CCC offline · cached" banner lives in `#status`, which is hidden unless the user enabled View ▸ "Status line". Default config shows stale data with no offline signal; combined with B1, a user can keep "sending" into the void.
- Suspect: `index.html` `renderStatus` + `VIEW_ITEMS` default `status:false`.

**B7 — minor. Tap targets below 36 px**
- Measured at 375 px: `#menuBtn` 28×28, `#theme`/`#foldAll` 28×26, window seg buttons 42×24, chips ≈100×24, `#viewBtn` 56×24, `.dots` 30×30, footer "Show" buttons 53×29. Rows (40–50 px) and Send (44×44) are fine.
- Suspect: `index.html` CSS (`@media max-width:760px` sets 30 px on `.dots,.eye,.star-b`; header buttons/chips have no mobile sizing).

**B8 — cosmetic. Footer wraps awkwardly on narrow screens**
- "… hidden by you [Show]" wraps the second Show button to its own line at ≤402 px.
- Screenshot: qa/shots/ios-list.png, qa/shots/j1-phone-375.png

## 4. Remote-gate detail (J11) — all verified against :8096 with `Host: nav-sandbox.test`

- `GET /api/tree`, `GET /`, `POST /api/hide`, `POST /api/pi/send` without cookie → **401** (API: `{"error":"not paired…"}`; `/`: pairing HTML page). No data leaked.
- `GET/POST /api/remote/pair`, `GET /api/remote/sessions` (remote) → **403 "only from the Mac itself"** ✓
- `GET /__pair?code=<valid>` → 200, shows only the "Pair this phone" button; code **not** spent (still in `qa/run/remote/pairings.json`).
- `POST /__pair` wrong Origin → **403**, code still unspent. Correct `Origin: https://nav-sandbox.test` → **303 → /** + `Set-Cookie: nav_session=…; HttpOnly; Secure; SameSite=Lax; 30d`.
- Code reuse → **403**. Cookie'd `GET /api/tree` → 200. Cookie'd POST with foreign Origin → **403 cross-origin**; with correct Origin → 200.
- Security headers on every remote response: HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`. All present.
- Rate limit: 5 recorded fails / 15 min → further `/__pair` requests → **429** (also blocks legitimate pairing while tripped — by design, worth knowing). Remote attempts logged to `qa/run/remote/remote-access.log`.
- Local requests (no Host override) need no cookie: `/api/tree` → 200 ✓

## 5. Every button/control found

Tested ✓: ☰ menu; ov eye; theme (auto→light→dark); foldAll; Now/1d/7d/All; search (+ `/` shortcut, Esc clear); Starred/Live/Needs-you chips; View ▾ (all 7 toggles + Reset); footer "Show" (background runs) + "Show" (hidden by you); repo/group collapse chevrons; session/group/repo inline star (shared handler — session verified directly); session/group/repo inline eye (shared handler); session ⋯ all 10 items; group ⋯ all items except Orca rename; repo ⋯ all items; ‹ back (mobile); chat title dbl-click rename; Details ⇄ Show conversation; nickname field; group picker (+filter/choose); new-group Add; rename/delete manual group; icon picker (grid/typed/reset); icon Reset; composer textarea + Send + Enter; "Show N earlier messages" fold; drag session→group; drawer: Check for update, Show what I hid, Show background runs, Recently-hidden Undo; toast Undo; focus bar Back/Copy link; drawer scrim + Esc; keyboard ↑↓/Enter/r.

Not tested: **Orca-workspace group rename** (unsafe — writes to real Orca, per brief); "Open in new tab"/"Open in CCC" popups (blocked by automation; code inspected, correct); "Update now" (only renders when a newer build exists — not reachable in sandbox); batch "Runs" links (rendered, not clicked); iOS taps (no tap automation; cliclick steered the real mouse — stopped).

## 6. Artifacts

`qa/shots/`: j1-desktop-initial.png, j1-phone-375.png, j3-chat-desktop.png, j3-long-transcript.png, j4-phone-chat.png, j5-phone-menu.png, j6-focus-mode.png, j12-landscape.png, ios-list.png, ios-drawer.png (drawer not open — taps unreliable, see §5), ios-tap-test.png.
`qa/run/blocked-writes.log` records the faked CCC writes (inject-input ×3, rename ×2). `qa/run/pi-runs/` has the Pi run log.

Sandbox left running/stopped per final step; a leftover navigator (PID 74513, started by the earlier sandbox.sh run) holds :8096 alongside the restarted proxy.
