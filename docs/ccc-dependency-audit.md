# CCC Dependency Audit (Phase 0 for #53)

**Repo:** drrozen-med/standup-os-infra (this worktree)  
**Scope:** our own code in `ccc-navigator/`, `bin/`, `watchtower/`, `qa/`; `~/.claude/skills/ccc-orchestration/`; `~/Library/LaunchAgents/com.standup-os.*.plist`; hooks in `~/.claude/settings.json`.  
**Clean-room:** no CCC source read/copied.  
**Date:** 2026-11-03  

## Dependency table

| File:Line(s) | Component | Endpoint/CLI used | Fields we rely on | What breaks if CCC is gone |
|--------------|-----------|-------------------|-------------------|----------------------------|
| `ccc-navigator/navigator.py:488-523, 1676-1680, 1779-1783, 1989-1993, 2036-2040, 2577-2714, 2822` | Navigator (server) | `GET /api/conversations?all=1&compact=1` (CCCSource), `POST /api/inject-input` (chat reply/send), `POST /api/conversations/<sid>/rename` (rename), `GET /api/conversations/<sid>` (transcript/proxy), `GET /api/question?session_id=...`, `POST /api/answer-question`. Also `--ccc` default `http://localhost:8090` (env `CCC_URL`). | Session list shape from CCC (`conversations`/items with ids, engines, paths/state flags like `is_live`, `needs_approval`, `question_waiting`, titles, cwd/paths), transcript events/blocks, question payloads, inject/answer/rename responses (`ok` etc.). | Core UI tree, chat/popout, sending messages into sessions, answering headless questions, renaming sessions all fail/proxy to CCC. Navigator cannot list/load sessions or relay actions. |
| `ccc-navigator/nav.py:625, 686, 1782-1817` | Navigator CLI | Uses Navigator API (`/api/pi/transcript`, `/api/inject-input`) which in turn depend on CCC; also CLI talks to Navigator (`--url` default `http://127.0.0.1:8095`). `send` posts to Navigator which proxies to CCC. `status` expects Navigator up. | Navigator response shapes; `res.get("ok")` on send, transcript events. | `nav send/brief/status` etc. depend on Navigator which depends on CCC for session data/actions. Direct CCC dependency is indirect via Navigator. |
| `ccc-navigator/titler.py:395-410` | Titler (auto-namer) | Navigator API via `Client` (defaults to `http://127.0.0.1:8095`): `GET /api/tree?window=...`, `GET /api/pi/transcript?sid=...`, `GET /api/conversations/<sid>`, `POST /api/rename`. Navigator proxies/uses CCC. | Tree items, transcript events, rename payload/response. | Titler cannot read tree/transcripts or rename sessions without Navigator/CCC. |
| `ccc-navigator/static/chat.js:268, 636` | Chat UI | `GET /api/conversations/<sid>` or `/api/pi/transcript?sid=...`, `POST /api/inject-input` or `/api/pi/send` (via Navigator). | Transcript shape for rendering blocks/events. | Chat cannot load transcript or send messages. |
| `ccc-navigator/tests/test_navigator.py:744,766` | Tests | Mock/expect `args.ccc = http://localhost:8090`. | CCC base URL wiring. | Tests assert CCC integration wiring. |
| `ccc-navigator/tests/test_titler.py:93-94` | Tests | Mock CCC endpoints under `/api/conversations/`. | Transcript fetch shape. | Tests for titler depend on CCC-shaped responses. |
| `ccc-navigator/tests/test_ask.py:225,231` | Tests | Exercise Navigator endpoints that proxy to CCC (`/api/inject-input`). | Inject flow. | Tests depend on CCC proxy behavior. |
| `ccc-navigator/tests/test_remote.py:183-195` | Tests | Check remote access guards include `/api/conversations/*`, `/api/inject-input`. | Route protection. | Tests assert CCC-proxied routes are protected. |
| `bin/copilot-checkin.py:34-36, 95-106, 201` | Copilot check-in | Navigator API: `GET /api/decisions`, `GET /api/tree?window=7d`, `GET /api/conversations/<sid>`. (No direct CCC URL; uses Navigator at `:8095`.) | Decisions/answers, tree copilot key, transcript events for "last reply" detection. | If Navigator/CCC down/missing, check-in skips/fails to read state; behavior degrades. Indirect CCC dependency. |
| `bin/test_copilot_checkin.py:76-84, 268, 344` | Tests | Fake Navigator returning `/api/decisions`, `/api/tree`, `/api/conversations/*`. | Navigator API shapes. | Tests model Navigator/CCC-backed data. |
| `bin/ccc-watchdog.sh:4, 1-21` | Watchdog (launchd) | `curl` to `http://127.0.0.1:8090/` (CCC), restarts `homebrew.mxcl.ccc` via `launchctl kickstart`. | CCC liveness. | If CCC crashes, watchdog restarts Homebrew CCC. With CCC removed, this becomes pointless/broken. |
| `~/Library/LaunchAgents/com.standup-os.ccc-watchdog.plist` | launchd job | Runs `bin/ccc-watchdog.sh` every 30s. | CCC process/port. | Launches watchdog that expects CCC. |
| `~/Library/LaunchAgents/com.standup-os.navigator.plist` | launchd job | Runs `navigator.py --no-browser` (no explicit `--ccc` shown; uses default `http://localhost:8090`). | CCC at default port. | Navigator starts and will try to read from CCC by default. |
| `~/.claude/skills/ccc-orchestration/SKILL.md:13,17,39,42,45,47,52,61,63,65-68,70,72,79,83,89,103,105,119,123,133` | Skill (orchestration) | Many CCC endpoints: `/api/health`, `/api/version`, `/api/sessions?repo_path=...`, `/api/sessions/spawned`, `/api/sessions?all=1`, `/api/sessions/spawn`, `/api/inject-input`, `/api/ask`, `/api/group-chat/*`, `/api/federation/*`. Uses `CCC_URL` from `~/.claude/command-center/port.txt` or env/default `:8090`. | Session lists/spawn results, models/efforts, ask/inject/group-chat/federation shapes. | Skill cannot discover/spawn/inject/ask/group-chat/federate without CCC. This is a direct, heavy dependency. |
| `~/.claude/settings.json:256,329,382` | Hooks | `ccc-navigator/hooks/claude_hook.py` (local hook) — references ccc-navigator paths, not necessarily CCC endpoints directly. | Hook behavior depends on Navigator/CCC indirectly. | Hooks run local code; impact indirect via Navigator/CCC state. |
| `qa/REPORT.md`, `qa/BRIEF.md`, `qa/sandbox.sh` | QA/docs | Reference CCC URLs/endpoints in test scenarios (e.g. `POST /api/inject-input` proxied, sandbox copies state). | Expected behavior tied to CCC. | QA expectations/documentation tied to CCC; sandbox assumes Navigator/CCC model. |
| `watchtower/README.md:44-45` | Docs | References `nav.py brief` (Navigator). | Indirect. | Indirect dependency via Navigator. |

## Surprises

- **Navigator is the hub.** Most "our code" doesn't call CCC directly except `navigator.py` (server) and `titler.py`/CLI go through Navigator. The blast radius is: Navigator + anything using it (`nav.py`, `titler.py`, chat UI, `copilot-checkin.py` indirectly).
- **CCC-watchdog is a hard restart path.** `bin/ccc-watchdog.sh` only knows about Homebrew's `homebrew.mxcl.ccc` and port 8090 — it will need to be removed/replaced when cutting over.
- **Orchestration skill is CCC-heavy.** `~/.claude/skills/ccc-orchestration` uses a large surface area (spawn, ask, group-chat, federation). Issue says "spawn a headless session (only if the audit shows we use it)" — audit shows it *exists* as a skill capability; actual usage in this repo's scripts is indirect via Navigator/chat, but the skill itself is a direct CCC client.
- **Tests encode CCC shapes.** Many tests mock CCC endpoints (`/api/conversations/*`, `/api/inject-input`) — they'll need updating when replacing CCC.
- **Default ports are baked in.** Navigator defaults to `:8090` for CCC (`CCC_URL`/`--ccc`), plist has no override. Easy to miss.
- **Answer/question relay is explicit.** Navigator has dedicated code paths for `/api/question`, `/api/answer-question` proxying — matches issue's "Answer a headless AskUserQuestion" need.
- **Rename goes through CCC.** Both Navigator (`/api/conversations/<sid>/rename`) and Titler (`/api/rename`) call CCC/Navigator rename paths.
- **No direct reads of CCC filesystem in our codebase.** Dependencies are all network/HTTP to `127.0.0.1:8090` or via Navigator API — clean-room friendly (we observe behavior/contracts).
