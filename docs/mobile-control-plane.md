# Phone control plane

Goal: one app on the owner's phone, behind Google sign-in, that shows what needs
a decision (the Inbox), the sessions and projects (the Navigator), and later
controls the agents on the `.251` VM. Later wrapped in Capacitor.

## Today's problem

- The Navigator runs on the Mac at `127.0.0.1:8095` and reads the Command Center
  (`:8090`), Orca's database and Pi sessions from disk. A phone cannot reach it.
- The Project Inbox lives on claude.ai as a hosted page. It is reachable, but it
  is a separate place from the Navigator.
- The `.251` VM is overloaded (load 23-92 on 4 CPUs), so it should not host
  anything heavy.

## Design: a small hub that the Mac and the VM call out to

```
Phone (browser now, Capacitor later)
   |  Google sign-in, HTTPS
   v
HUB  (always on, small)
   - owner-only sign-in (one allowed Google account)
   - Inbox: decisions, answers, notes
   - Index: projects, sessions, status (titles and states only)
   - Command queue + audit log
   ^                      ^
   | outbound only        | outbound only
Mac connector          .251 connector
(Navigator data)       (agents: mm-agent services)
```

Why a hub and not "expose the Mac":
- The Mac sleeps and changes networks. The hub is always up.
- Nothing on the Mac or the VM accepts inbound connections. Both connect out.
- One sign-in, one audit log, one kill switch.

Recommended hub: a Cloudflare Worker with a database (D1) and a Durable Object
for live connections. The owner already deploys Workers, and it puts no load on
`.251`. It may need the $5 Workers Paid plan (open question in the Inbox).

## Sign-in

- Only one Google account is allowed (the owner's), checked on the server.
- Browser phase: Google sign-in, then a short-lived session cookie.
- Capacitor phase: Google blocks sign-in inside an embedded web view. Use the
  system browser sheet or a native Google sign-in plugin, send the Google ID
  token to the hub, and the hub verifies it (audience, expiry, allowed email)
  and issues its own short-lived token. Plan for this from day one; it is the
  usual reason a wrapped app "suddenly can't log in".

## What crosses the wire (privacy first)

- The hub stores **titles, states, counts and decisions** only.
- Conversation text is **never** copied to the hub. When the owner opens a
  session, the hub asks the connector for it live, shows it, and does not keep it.
- Sessions can contain medical, business and personal content. The Navigator
  namer was changed to stay on the machine by default (PR #3). The hub must not
  reintroduce that risk.

## Commands (phone controls things)

Commands are a short allowlist, never a shell:

| Tier | Examples | Rule |
|---|---|---|
| Act alone | hide/star a session, mark a card answered | no confirmation |
| Act and tell | send a message to a session, start a read-only check | logged, push notification |
| Ask first | merge, deploy, spend, delete, send outreach | second tap with a clear summary |

Every command is written to an audit log (who, what, when, result). A single
"pause everything" switch stops all connectors from executing commands.

## Phases

1. **This week, fastest:** a Cloudflare Tunnel from the Mac to a hostname, behind
   Cloudflare Access with Google login, for the Navigator. Phone works in the
   browser (add to home screen). Read and light actions only. Throwaway; the
   Mac must be awake.
2. **Hub v1:** Worker + D1, Google sign-in, move the Inbox in (the claude.ai Inbox
   keeps working until the switch), Mac connector pushes the project/session
   index.
3. **Capacitor shell:** same web app in a native shell, native Google sign-in,
   push notifications for "needs you".
4. **`.251` connector:** the existing `mm-agent@` services report status and take
   allowlisted commands. Starts read-only.

## Decisions needed (also in the Inbox)

1. Which hostname/domain to use for the hub.
2. Hub on Cloudflare (recommended) or on `.251`.
3. Go-ahead for Phase 1 on the Mac (needs the Mac to run `cloudflared`).

## Risks

- The hub is the most valuable thing to attack. Single allowed account, short
  tokens, audit log, no shell, no stored transcripts.
- A Mac connector that can run commands is remote control of the Mac. Keep the
  verb list tiny and review every addition.
- Apple and Google store review for a wrapped app takes time; use TestFlight and
  an internal Play track for personal use.
