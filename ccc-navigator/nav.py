#!/usr/bin/env python3
"""Command-line client for the local CCC Navigator JSON API.

The navigator must already be running. By default this command talks to
http://127.0.0.1:8095; override that with --url or CCC_NAV_URL.

Every command emits JSON by default. Use --table for human-readable output.
Batch mutations support --dry-run so the exact affected session keys can be
inspected before making changes. Sessions are hidden and starred by key;
groups and whole projects use their own keys (g:<repo>|<group> and r:<repo>),
which the group and project commands resolve for you. `audit` summarises every
project and group so a cleanup can be planned before anything is hidden.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8095"
REQUEST_TIMEOUT = 120
AGE_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$", re.IGNORECASE)
AGE_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}
# Navigator state keys for things that are not sessions.
GROUP_KEY_PREFIX = "g:"
REPO_KEY_PREFIX = "r:"


def group_key(repo, group):
    """Navigator key of a group: g:<repo key>|<group name>."""
    return f"{GROUP_KEY_PREFIX}{repo}|{group}"


def repo_key(repo):
    """Navigator key of a whole project: r:<repo key>."""
    return f"{REPO_KEY_PREFIX}{repo}"


class NavError(Exception):
    """A request to the navigator failed."""


class UsageError(Exception):
    """The command was understood but is unsafe or incomplete."""


class NavigatorClient:
    """Small stdlib HTTP client for the navigator's local JSON API."""

    def __init__(self, base_url=DEFAULT_URL):
        self.base_url = base_url.rstrip("/")

    def _request(self, path, method="GET", payload=None):
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json", "X-Nav-Actor": os.environ.get("NAV_ACTOR", "cli")}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
            suffix = f": {detail}" if detail else ""
            raise NavError(f"navigator returned HTTP {exc.code}{suffix}") from exc
        except Exception as exc:
            raise NavError(f"cannot reach navigator at {self.base_url}: {exc}") from exc
        try:
            result = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise NavError(f"navigator returned invalid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise NavError("navigator returned an unexpected JSON response")
        return result

    def tree(self):
        query = urllib.parse.urlencode({"window": "all", "hidden": "1"})
        return self._request(f"/api/tree?{query}")

    def post(self, path, payload):
        return self._request(path, method="POST", payload=payload)

    def get(self, path):
        return self._request(path)


def parse_age(value):
    """Parse a relative age such as 14d or 6h into seconds."""
    text = str(value).strip()
    match = AGE_RE.fullmatch(text)
    if not match:
        raise argparse.ArgumentTypeError("age must look like 14d, 6h, 30m, or 2w")
    amount = float(match.group(1))
    seconds = amount * AGE_SECONDS[match.group(2).lower()]
    if seconds <= 0:
        raise argparse.ArgumentTypeError("age must be greater than zero")
    return seconds


def flatten_tree(tree):
    """Flatten the navigator tree and retain each item's repo key."""
    flattened = []
    for repo in tree.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        rkey = str(repo.get("key") or "")
        for group in repo.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("name") or "")
            for raw_item in group.get("items") or []:
                if not isinstance(raw_item, dict):
                    continue
                item = dict(raw_item)
                item["repo"] = rkey
                item["repo_name"] = str(repo.get("name") or "")
                item["repo_path"] = str(repo.get("path") or "")
                if not item.get("group"):
                    item["group"] = group_name
                flattened.append(item)
    return flattened


def _contains(value, needle):
    return str(needle).casefold() in str(value or "").casefold()


def matching_repos(tree, needle=None):
    """Repos whose key, name or path contains the needle (case-insensitive).

    An exact repo key always wins over a substring match, so `--repo` with a
    full path never touches the sibling repos that live inside it.
    """
    repos = [repo for repo in tree.get("repos") or [] if isinstance(repo, dict)]
    if not needle:
        return repos
    exact = [repo for repo in repos if str(repo.get("key") or "") == str(needle)]
    if exact:
        return exact
    return [
        repo
        for repo in repos
        if any(_contains(repo.get(field), needle) for field in ("key", "name", "orig_name", "path"))
    ]


def matching_groups(tree, repo_needle=None, group_needle=None):
    """Groups of the repos matching repo_needle whose name matches group_needle."""
    repo_keys = {str(repo.get("key") or "") for repo in matching_repos(tree, repo_needle)}
    groups = []
    for group in collect_groups(tree):
        if group["repo"] not in repo_keys:
            continue
        if group_needle is not None and not _contains(group["group"], group_needle):
            continue
        groups.append(group)
    return groups


def collect_groups(tree):
    """Every group in the tree with its exact name, repo key and items."""
    groups = []
    for repo in tree.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        rkey = str(repo.get("key") or "")
        rname = str(repo.get("name") or "")
        seen = set()
        for group in repo.get("groups") or []:
            if not isinstance(group, dict):
                continue
            name = str(group.get("name") or "")
            seen.add(name)
            groups.append(
                {
                    "repo": rkey,
                    "repo_name": rname,
                    "group": name,
                    "hidden": bool(group.get("hidden")),
                    "pinned": bool(group.get("pinned")),
                    "orca_path": str(group.get("orca_path") or ""),
                    "items": [item for item in (group.get("items") or []) if isinstance(item, dict)],
                }
            )
        for name in repo.get("group_names") or []:
            name = str(name)
            if name and name not in seen:
                groups.append(
                    {
                        "repo": rkey,
                        "repo_name": rname,
                        "group": name,
                        "hidden": False,
                        "pinned": False,
                        "orca_path": "",
                        "items": [],
                    }
                )
    return groups


def collect_repos(tree):
    """Every repo in the tree with its flattened items."""
    repos = []
    for repo in tree.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        items = [
            item
            for group in repo.get("groups") or []
            if isinstance(group, dict)
            for item in (group.get("items") or [])
            if isinstance(item, dict)
        ]
        repos.append(
            {
                "key": str(repo.get("key") or ""),
                "name": str(repo.get("name") or ""),
                "orig_name": str(repo.get("orig_name") or ""),
                "path": str(repo.get("path") or ""),
                "hidden": bool(repo.get("hidden")),
                "pinned": bool(repo.get("pinned")),
                "group_names": [
                    str(group.get("name") or "")
                    for group in repo.get("groups") or []
                    if isinstance(group, dict)
                ],
                "items": items,
            }
        )
    return repos


def find_item(tree, key):
    """The flattened tree item with this session key, or None."""
    for item in flatten_tree(tree):
        if str(item.get("key") or "") == str(key):
            return item
    return None


def split_starred(items, include_starred):
    """Split matched items into (targets, skipped) so starred sessions stay safe."""
    if include_starred:
        return list(items), []
    targets, skipped = [], []
    for item in items:
        (skipped if item.get("pinned") else targets).append(item)
    return targets, skipped


def item_keys(items):
    return [str(item.get("key")) for item in items if item.get("key")]


def filter_items(
    items,
    *,
    repo=None,
    group=None,
    engine=None,
    older=None,
    newer=None,
    live=False,
    match=None,
    hidden_mode="exclude",
    starred=False,
    limit=None,
    now=None,
):
    """Apply CLI filters to flattened navigator items.

    hidden_mode is one of "exclude", "include", or "only".
    starred keeps only items the user has starred (pinned).
    """
    now = dt.datetime.now().timestamp() if now is None else now
    result = []
    for item in items:
        hidden = bool(item.get("hidden"))
        if hidden_mode == "exclude" and hidden:
            continue
        if hidden_mode == "only" and not hidden:
            continue

        if repo is not None:
            repo_fields = (item.get("repo"), item.get("repo_name"), item.get("repo_path"))
            if not any(_contains(value, repo) for value in repo_fields):
                continue

        if group is not None and not _contains(item.get("group", ""), group):
            continue

        if engine is not None and str(item.get("engine") or "").casefold() != engine.casefold():
            continue

        mtime = float(item.get("mtime") or 0)
        if older is not None and mtime >= now - older:
            continue
        if newer is not None and mtime <= now - newer:
            continue

        if live and not bool(item.get("live")):
            continue

        if match is not None:
            searchable = (item.get("nick"), item.get("original"))
            if not any(_contains(value, match) for value in searchable):
                continue

        if starred and not item.get("pinned"):
            continue

        result.append(item)

    if limit is not None:
        result = result[:limit]
    return result


def visibility_mode(args, default="exclude"):
    if getattr(args, "include_hidden", False):
        return "include"
    return default


def filter_from_args(items, args, *, hidden_default="exclude"):
    return filter_items(
        items,
        repo=args.repo,
        group=args.group,
        engine=args.engine,
        older=args.older,
        newer=args.newer,
        live=args.live,
        match=args.match_text,
        hidden_mode=visibility_mode(args, hidden_default),
        starred=bool(getattr(args, "starred", False)),
        limit=args.limit,
    )


def has_selector(args):
    """Whether any safety-relevant filter (not --limit) was supplied."""
    return any(
        (
            args.repo is not None,
            args.group is not None,
            args.engine is not None,
            args.older is not None,
            args.newer is not None,
            bool(args.live),
            bool(args.include_hidden),
            args.match_text is not None,
            bool(getattr(args, "starred", False)),
        )
    )


def _add_global_options(parser, *, subcommand=False):
    url_default = argparse.SUPPRESS if subcommand else os.environ.get("CCC_NAV_URL", DEFAULT_URL)
    table_default = argparse.SUPPRESS if subcommand else False
    parser.add_argument(
        "--url",
        default=url_default,
        help=f"navigator base URL (default: {DEFAULT_URL}, env: CCC_NAV_URL)",
    )
    parser.add_argument(
        "--table",
        action="store_true",
        default=table_default,
        help="print human-readable output instead of JSON",
    )


def _add_filter_options(parser, *, with_dry_run=False, with_starred=False, with_include_starred=False):
    parser.add_argument("--repo", help="substring of repo key, name, or path")
    parser.add_argument("--group", help="substring of group name")
    parser.add_argument("--engine", help="engine name, for example claude or codex")
    parser.add_argument("--older", type=parse_age, metavar="AGE", help="only items older than AGE (14d, 6h)")
    parser.add_argument("--newer", type=parse_age, metavar="AGE", help="only items newer than AGE (7d, 6h)")
    parser.add_argument("--live", action="store_true", help="only live sessions")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument(
        "--hidden",
        dest="include_hidden",
        action="store_true",
        help="include hidden and visible items (hidden items are excluded by default)",
    )
    visibility.add_argument(
        "--include-hidden",
        dest="include_hidden",
        action="store_true",
        help="alias for --hidden",
    )
    parser.add_argument("--match", dest="match_text", help="case-insensitive nickname or title substring")
    parser.add_argument("--limit", type=int, help="return at most N matching items")
    if with_starred:
        parser.add_argument(
            "--starred",
            action="store_true",
            help="only items the user has starred (pinned)",
        )
    if with_include_starred:
        parser.add_argument(
            "--include-starred",
            action="store_true",
            help="let a batch operation also touch starred items (they are skipped by default)",
        )
    if with_dry_run:
        parser.add_argument("--dry-run", action="store_true", help="show affected keys without changing them")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_global_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ls = subparsers.add_parser("ls", help="list flattened sessions, groups or repos")
    _add_global_options(ls, subcommand=True)
    _add_filter_options(ls, with_starred=True)
    listing = ls.add_mutually_exclusive_group()
    listing.add_argument("--groups", action="store_true", help="list groups instead of sessions")
    listing.add_argument("--repos", action="store_true", help="list repos instead of sessions")

    for command, help_text in (("hide", "hide matching sessions"), ("unhide", "unhide matching sessions")):
        sub = subparsers.add_parser(command, help=help_text)
        _add_global_options(sub, subcommand=True)
        _add_filter_options(sub, with_dry_run=True, with_starred=True, with_include_starred=True)

    move = subparsers.add_parser("move", help="move matching sessions to a group")
    _add_global_options(move, subcommand=True)
    _add_filter_options(move, with_dry_run=True, with_starred=True, with_include_starred=True)
    move.add_argument("--to", required=True, metavar="GROUP", help="destination group name")

    dl = subparsers.add_parser("decisions", help="the decision inbox: what waits on Uri (open first; --answered for recent answers)")
    _add_global_options(dl, subcommand=True)
    dl.add_argument("--answered", action="store_true", help="show recent answers instead of open questions")

    dc = subparsers.add_parser("decide", help="answer a decision (as the actor in $NAV_ACTOR), or --reopen it")
    _add_global_options(dc, subcommand=True)
    dc.add_argument("id", help="decision id (from nav decisions)")
    dc.add_argument("choice", nargs="?", default="", help="one of its options, or 'other' with --note")
    dc.add_argument("--note", default="", help="free-text answer or detail")
    dc.add_argument("--reopen", action="store_true", help="remove the answer so it is open again")

    ak = subparsers.add_parser("ask", help="put a question to Uri: it appears in his decision inbox (and on his phone)")
    _add_global_options(ak, subcommand=True)
    ak.add_argument("--question", required=True)
    ak.add_argument("--option", action="append", required=True, help="answer option (repeat, 1-8)")
    ak.add_argument("--lane", default="", help="lane/project, e.g. aba-cdl-launch")
    ak.add_argument("--priority", type=int, default=2, choices=(1, 2, 3))
    ak.add_argument("--deadline", help="YYYY-MM-DD")
    ak.add_argument("--session", default="", help="session key that gets the answer as a message, e.g. claude:<uuid>")
    ak.add_argument("--source", default="", help="where the question comes from (PR, report)")
    ak.add_argument("--id", default="", help="stable id (default: derived from the question)")

    mp = subparsers.add_parser("move-project", help="file sessions under another project than their folder's (--back undoes)")
    _add_global_options(mp, subcommand=True)
    _add_filter_options(mp, with_dry_run=True, with_starred=True, with_include_starred=True)
    mp.add_argument("--key", action="append", default=[], metavar="KEY", help="exact session key (repeatable), e.g. claude:<uuid>")
    dest = mp.add_mutually_exclusive_group(required=True)
    dest.add_argument("--to", metavar="PROJECT", help="destination project: exact key, or a substring of its name/path matching one project")
    dest.add_argument("--back", action="store_true", help="put the sessions back under their folder's project")

    cp = subparsers.add_parser("copilot", help="show, set or clear Uri's co-pilot session (the Co-pilot button)")
    _add_global_options(cp, subcommand=True)
    cpx = cp.add_mutually_exclusive_group()
    cpx.add_argument("--set", metavar="KEY", help="session key, e.g. claude:<uuid>, or a Claude desktop id local_<uuid>")
    cpx.add_argument("--clear", action="store_true", help="remove the Co-pilot button")

    dg = subparsers.add_parser("desktop-groups", help="Claude desktop sidebar groups and the project each files under")
    _add_global_options(dg, subcommand=True)

    md = subparsers.add_parser("map-desktop-group", help="file a desktop sidebar group's sessions under a project")
    _add_global_options(md, subcommand=True)
    md.add_argument("group", metavar="GROUP", help="exact desktop group name, e.g. 'BoadsBridge'")
    mdest = md.add_mutually_exclusive_group(required=True)
    mdest.add_argument("--to", metavar="PROJECT", help="exact project key, or a substring matching one project")
    mdest.add_argument("--clear", action="store_true", help="unmap: the group shows as its own project")

    ua = subparsers.add_parser("unhide-all", help="unhide every hidden session in one project (or one of its groups), all time")
    _add_global_options(ua, subcommand=True)
    ua.add_argument("--repo", required=True, metavar="PROJECT", help="exact project key, or a substring matching one project")
    ua.add_argument("--group", metavar="NAME", help="exact group name ('' for Ungrouped); omit for the whole project")

    for command, help_text in (("star", "star matching sessions"), ("unstar", "remove the star from matching sessions")):
        sub = subparsers.add_parser(command, help=help_text)
        _add_global_options(sub, subcommand=True)
        _add_filter_options(
            sub,
            with_dry_run=True,
            with_starred=command == "star",
            with_include_starred=True,
        )

    for command, help_text in (
        ("hide-group", "hide a group of sessions"),
        ("unhide-group", "unhide a group of sessions"),
    ):
        sub = subparsers.add_parser(command, help=help_text)
        _add_global_options(sub, subcommand=True)
        sub.add_argument("--repo", required=True, metavar="SUBSTR", help="substring of repo key, name, or path")
        sub.add_argument("--group", required=True, metavar="SUBSTR", help="substring of group name")
        sub.add_argument("--dry-run", action="store_true", help="show affected keys without changing them")

    for command, help_text in (
        ("hide-repo", "hide a whole project"),
        ("unhide-repo", "unhide a whole project"),
    ):
        sub = subparsers.add_parser(command, help=help_text)
        _add_global_options(sub, subcommand=True)
        sub.add_argument("--repo", required=True, metavar="SUBSTR", help="substring of repo key, name, or path")
        sub.add_argument("--dry-run", action="store_true", help="show affected keys without changing them")

    icon = subparsers.add_parser("icon", help="set or clear the emoji icon of a group or project")
    _add_global_options(icon, subcommand=True)
    icon.add_argument("--repo", metavar="SUBSTR", help="substring of repo key, name, or path")
    icon.add_argument("--group", metavar="SUBSTR", help="substring of group name")
    icon_mode = icon.add_mutually_exclusive_group(required=True)
    icon_mode.add_argument("--set", dest="set_icon", metavar="EMOJI", help="emoji to show next to the group or project")
    icon_mode.add_argument("--clear", action="store_true", help="remove the icon")
    icon.add_argument("--dry-run", action="store_true", help="show affected keys without changing them")

    rename = subparsers.add_parser(
        "rename",
        help="rename a session, a group or a project",
        epilog=(
            "examples:\n"
            "  rename session claude:1234 'Billing fix'\n"
            "  rename group --repo standup --group infra 'Infrastructure'\n"
            "  rename repo --repo standup 'Standup OS'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_options(rename, subcommand=True)
    rename.add_argument("target", choices=("session", "group", "repo"), help="what to rename")
    rename.add_argument("rest", nargs="*", metavar="ARG", help="session KEY, or the NEW_NAME")
    rename.add_argument("--repo", metavar="SUBSTR", help="substring of repo key, name, or path")
    rename.add_argument("--group", metavar="SUBSTR", help="substring of group name")

    audit = subparsers.add_parser("audit", help="summarise projects, groups and stale sessions")
    _add_global_options(audit, subcommand=True)

    curate = subparsers.add_parser(
        "curate",
        help="propose (or apply) hiding of stale groups, sessions and projects",
        epilog=(
            "examples:\n"
            "  curate                     # proposal only, nothing changes\n"
            "  curate --older 2d          # quieter than the 14d default\n"
            "  curate --older 2d --apply  # hide what the proposal lists\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_options(curate, subcommand=True)
    curate.add_argument("--older", default="14d", metavar="AGE", help="how long before something is stale")
    curate.add_argument("--apply", action="store_true", help="actually hide the proposed keys")
    curate.add_argument(
        "--star-active",
        action="store_true",
        help="also star groups that are live or have an agent attached (needs --apply)",
    )

    nick = subparsers.add_parser("nick", help="set a session nickname")
    _add_global_options(nick, subcommand=True)
    nick.add_argument("key", help="session key")
    nick.add_argument("name", help="new nickname")

    groups = subparsers.add_parser("groups", help="list groups for a repo")
    _add_global_options(groups, subcommand=True)
    groups.add_argument("--repo", required=True, help="repo key or substring")

    send = subparsers.add_parser("send", help="send a message into a session (through CCC) and optionally wait for the reply")
    _add_global_options(send, subcommand=True)
    send.add_argument("target", help="session key (engine:id), session id, or a unique id prefix")
    send.add_argument("text", help="message to send")
    send.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="wait up to this long for the agent's reply")
    send.add_argument("--dry-run", action="store_true", help="resolve the target and show what would be sent")

    read = subparsers.add_parser("read", help="print the latest messages of a session as plain text")
    _add_global_options(read, subcommand=True)
    read.add_argument("target", help="session key (engine:id), session id, or a unique id prefix")
    read.add_argument("--last", type=int, default=10, help="how many messages (default 10)")
    read.add_argument("--chars", type=int, default=1200, help="max characters per message (default 1200)")

    note = subparsers.add_parser("note", help="add a note to a session (or --rm N to delete one)")
    _add_global_options(note, subcommand=True)
    note.add_argument("target", help="session key (engine:id), session id, or a unique id prefix")
    note.add_argument("text", nargs="?", default="", help="the note")
    note.add_argument("--rm", type=int, default=None, metavar="N", help="delete note number N (from `notes`)")
    note.add_argument("--by", default="you", help="who wrote it (default: you)")

    notes = subparsers.add_parser("notes", help="list notes for one session, or every session with notes")
    _add_global_options(notes, subcommand=True)
    notes.add_argument("target", nargs="?", default="", help="session key, id or prefix (omit for all)")

    mark = subparsers.add_parser("mark", help="set a session's triage label yourself (the sorter will not override it)")
    _add_global_options(mark, subcommand=True)
    mark.add_argument("target", help="session key (engine:id), session id, or a unique id prefix")
    mark.add_argument("label", help="working | needs-you | blocked | done | idle | stale | any word, or 'clear'")
    mark.add_argument("--reason", default="", help="why")

    triage = subparsers.add_parser("triage", help="sort visible sessions into working / needs-you / blocked / done / idle / stale")
    _add_global_options(triage, subcommand=True)
    triage.add_argument("--window", default="7d", choices=["now", "1d", "7d", "all"])
    triage.add_argument("--dry-run", action="store_true", help="show labels without saving them")
    triage.add_argument("--only", default="", help="print only these labels, comma-separated (e.g. needs-you,blocked)")

    brief = subparsers.add_parser("brief", help="live snapshot: per project, what is running, waiting on you, blocked or done")
    _add_global_options(brief, subcommand=True)
    brief.add_argument("--window", default="7d", choices=["now", "1d", "7d", "all"])

    ws = subparsers.add_parser("ws", help="resolve a workspace link (from the ⋯ menu, Copy link) and list all its sessions, hidden included")
    _add_global_options(ws, subcommand=True)
    ws.add_argument("link", help="the copied link, or just its #g=... part")

    rem = subparsers.add_parser("remote", help="phone access: pair a phone, list or revoke paired phones")
    _add_global_options(rem, subcommand=True)
    rem.add_argument("action", choices=["status", "pair", "sessions", "revoke"])
    rem.add_argument("target", nargs="?", default="", help="for revoke: a session id prefix, or 'all'")

    status = subparsers.add_parser("status", help="show navigator and CCC status")
    _add_global_options(status, subcommand=True)

    return parser


def print_json(value):
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def print_error(exc):
    print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)


def _display_time(value):
    timestamp = float(value or 0)
    if not timestamp:
        return ""
    return dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


def print_table(headers, rows):
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())


def print_items_table(items):
    rows = []
    for item in items:
        rows.append(
            [
                item.get("key", ""),
                item.get("nick") or item.get("original", ""),
                item.get("repo_name") or item.get("repo", ""),
                item.get("group") or "(ungrouped)",
                item.get("engine", ""),
                item.get("triage", ""),
                "yes" if item.get("live") else "",
                "yes" if item.get("hidden") else "",
                _display_time(item.get("mtime")),
            ]
        )
    print_table(["KEY", "TITLE", "REPO", "GROUP", "ENGINE", "TRIAGE", "LIVE", "HIDDEN", "MTIME"], rows)


def print_mutation_table(result):
    print(f"count: {result['count']}")
    for key in result.get("keys", []):
        print(key)


def cmd_ls(args, client):
    tree = client.tree()
    if args.groups:
        return _ls_groups(args, tree)
    if args.repos:
        return _ls_repos(args, tree)
    items = filter_from_args(flatten_tree(tree), args)
    if args.table:
        print_items_table(items)
    else:
        print_json(items)
    return 0


def _item_filter(args, items, *, hidden_default="include"):
    """Item-level filters (engine/age/live/match) applied inside a group or repo."""
    return filter_items(
        items,
        engine=args.engine,
        older=args.older,
        newer=args.newer,
        live=args.live,
        match=args.match_text,
        hidden_mode=visibility_mode(args, hidden_default),
        starred=bool(getattr(args, "starred", False)),
        now=None,
    )


def _ls_groups(args, tree):
    icons = tree.get("icons") or {}
    rows = []
    for group in matching_groups(tree, args.repo, args.group):
        items = _item_filter(args, group["items"])
        newest = max((float(item.get("mtime") or 0) for item in items), default=0.0)
        rows.append(
            {
                "repo": group["repo"],
                "repo_name": group["repo_name"],
                "group": group["group"],
                "count": len(items),
                "hidden": group["hidden"],
                "starred_count": sum(1 for item in items if item.get("pinned")),
                "newest_mtime": newest,
                "orca_path": group["orca_path"],
                "icon": str(icons.get(group_key(group["repo"], group["group"])) or ""),
            }
        )
    if args.limit is not None:
        rows = rows[: args.limit]
    if args.table:
        print_table(
            ["REPO", "GROUP", "COUNT", "HIDDEN", "STARRED", "NEWEST", "ORCA_PATH"],
            [
                [
                    row["repo"],
                    row["group"],
                    row["count"],
                    "yes" if row["hidden"] else "",
                    row["starred_count"],
                    _display_time(row["newest_mtime"]),
                    row["orca_path"],
                ]
                for row in rows
            ],
        )
    else:
        print_json(rows)
    return 0


def _ls_repos(args, tree):
    icons = tree.get("icons") or {}
    allowed = {str(repo.get("key") or "") for repo in matching_repos(tree, args.repo)}
    rows = []
    for repo in collect_repos(tree):
        if repo["key"] not in allowed:
            continue
        if args.group is not None:
            if not any(_contains(name, args.group) for name in repo["group_names"]):
                continue
        items = _item_filter(args, repo["items"])
        newest = max((float(item.get("mtime") or 0) for item in items), default=0.0)
        rows.append(
            {
                "repo": repo["key"],
                "name": repo["name"],
                "orig_name": repo["orig_name"],
                "count": len(items),
                "hidden": repo["hidden"],
                "starred_count": sum(1 for item in items if item.get("pinned")),
                "newest_mtime": newest,
                "group_count": len(repo["group_names"]),
                "path": repo["path"],
                "icon": str(icons.get(repo_key(repo["key"])) or ""),
            }
        )
    if args.limit is not None:
        rows = rows[: args.limit]
    if args.table:
        print_table(
            ["REPO", "NAME", "COUNT", "HIDDEN", "STARRED", "GROUPS", "NEWEST"],
            [
                [
                    row["repo"],
                    row["name"],
                    row["count"],
                    "yes" if row["hidden"] else "",
                    row["starred_count"],
                    row["group_count"],
                    _display_time(row["newest_mtime"]),
                ]
                for row in rows
            ],
        )
    else:
        print_json(rows)
    return 0


def cmd_hide(args, client, action):
    if not has_selector(args):
        raise UsageError("hide/unhide require at least one filter; --limit alone is not a filter")
    tree = client.tree()
    default_mode = "exclude" if action == "hide" else "only"
    items = filter_from_args(flatten_tree(tree), args, hidden_default=default_mode)
    skipped = []
    if action == "hide" and not args.include_starred and not args.starred:
        # starred sessions stay visible unless the user opts in explicitly
        items, skipped = split_starred(items, False)
    keys = item_keys(items)
    result = {
        "action": action,
        "count": len(keys),
        "keys": keys,
        "skipped_starred": item_keys(skipped),
        "dry_run": bool(args.dry_run),
    }
    if keys and not args.dry_run:
        client.post("/api/hide", {"keys": keys, "hidden": action == "hide"})
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_move(args, client):
    if not has_selector(args):
        raise UsageError("move requires at least one filter; --limit alone is not a filter")
    tree = client.tree()
    items = filter_from_args(flatten_tree(tree), args)
    skipped = []
    if not args.include_starred and not args.starred:
        items, skipped = split_starred(items, False)
    keys = item_keys(items)
    if not args.dry_run:
        for item in items:
            key = item.get("key")
            repo = item.get("repo")
            if not key or not repo:
                raise NavError(f"item is missing key or repo key: {key or '(unknown)'}")
            client.post("/api/assign", {"key": key, "repo": repo, "group": args.to})
    result = {
        "action": "move",
        "group": args.to,
        "count": len(keys),
        "keys": keys,
        "skipped_starred": item_keys(skipped),
        "dry_run": bool(args.dry_run),
    }
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_move_project(args, client):
    if not args.key and not has_selector(args):
        raise UsageError("move-project needs --key or at least one filter; --limit alone is not a filter")
    tree = client.tree()
    items = flatten_tree(tree)
    if args.key:
        want = set(args.key)
        items = [i for i in items if i.get("key") in want]
        missing = sorted(want - {i.get("key") for i in items})
        if missing:
            raise NavError("unknown session key(s): " + ", ".join(missing))
    else:
        items = filter_from_args(items, args)
    skipped = []
    if not args.key and not args.include_starred and not args.starred:   # named keys are moved as asked
        items, skipped = split_starred(items, False)
    items = [i for i in items if not str(i.get("key") or "").startswith("batch:")]
    target = {"key": "", "name": "(its folder's project)"}
    if not args.back:
        repos = matching_repos(tree, args.to)
        if len(repos) != 1:
            names = ", ".join(r.get("name", "?") for r in repos[:8]) or "none"
            raise UsageError(f"--to must match exactly one project, matched {len(repos)}: {names}")
        target = repos[0]
    keys = item_keys(items)
    if keys and not args.dry_run:
        client.post("/api/move-project", {"keys": keys, "repo": target["key"]})
    result = {
        "action": "move-project",
        "project": target["key"],
        "project_name": target["name"],
        "count": len(keys),
        "keys": keys,
        "skipped_starred": item_keys(skipped),
        "dry_run": bool(args.dry_run),
    }
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_copilot(args, client):
    if args.set or args.clear:
        key = args.set or ""
        if key.startswith("local_"):          # Claude desktop id -> CLI session key
            key = desktop_to_key(key)
        res = client.post("/api/copilot", {"key": key})
        print_json(res.get("copilot"))
    else:
        print_json(client.tree().get("copilot"))
    return 0


def desktop_to_key(local_id):
    """claude:<cli id> for a Claude desktop session id, read from the desktop app's records."""
    import glob
    pattern = os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions/*/*/%s.json" % local_id)
    for path in glob.glob(pattern):
        try:
            with open(path) as fh:
                sid = json.load(fh).get("cliSessionId")
        except (OSError, ValueError):
            continue
        if sid:
            return "claude:" + sid
    raise UsageError("no Claude desktop session " + local_id)


def cmd_desktop_groups(args, client):
    rows = client.get("/api/desktop-groups").get("groups") or []
    if args.table:
        for g in rows:
            print(f"{g['sessions']:>4}  {g['name']:<32}  -> {g['project']}{'' if g['mapped'] else '  (own project)'}")
    else:
        print_json(rows)
    return 0


def cmd_map_desktop_group(args, client):
    groups = {g["name"] for g in client.get("/api/desktop-groups").get("groups") or []}
    if args.group not in groups:
        raise UsageError(f"unknown desktop group {args.group!r}; known: " + ", ".join(sorted(groups)))
    repo = ""
    path = os.path.expanduser(args.to or "")
    if path.startswith("/") and os.path.isdir(path):     # a project folder with no sessions of its own yet
        repo = os.path.realpath(path)
    elif args.to:
        repos = matching_repos(client.tree(), args.to)
        if len(repos) != 1:
            names = ", ".join(r.get("name", "?") for r in repos[:8]) or "none"
            raise UsageError(f"--to must match exactly one project, matched {len(repos)}: {names}")
        repo = repos[0]["key"]
    res = client.post("/api/desktop-map", {"group": args.group, "repo": repo})
    print_json({"action": "map-desktop-group", "group": args.group, "project": repo or None, "change": res.get("change")})
    return 0


def cmd_unhide_all(args, client):
    repos = matching_repos(client.tree(), args.repo)
    if len(repos) != 1:
        names = ", ".join(r.get("name", "?") for r in repos[:8]) or "none"
        raise UsageError(f"--repo must match exactly one project, matched {len(repos)}: {names}")
    body = {"repo": repos[0]["key"]}
    if args.group is not None:
        body["group"] = args.group
    res = client.post("/api/unhide-scope", body)
    result = {"action": "unhide-all", "project": repos[0]["key"], "group": args.group,
              "count": res.get("count", 0), "keys": []}
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_decisions(args, client):
    d = client.get("/api/decisions")
    rows = d.get("answered" if args.answered else "open") or []
    if not args.table:
        print_json({"count": len(rows), "decisions": rows})
        return 0
    if not rows:
        print("nothing answered yet" if args.answered else "nothing waits on Uri")
    for x in rows:
        if args.answered:
            a = x.get("answer") or {}
            print("%-24s -> %s%s  (by %s)" % (x["id"], a.get("choice"), (": " + a["note"]) if a.get("note") else "", a.get("by")))
        else:
            left = x.get("days_left")
            due = "" if left is None else ("  [%dd left]" % left if left >= 0 else "  [%dd overdue]" % -left)
            print("P%s %-24s %s%s\n     options: %s" % (x.get("priority", 3), x["id"], x.get("question", ""), due,
                                                     " / ".join(map(str, x.get("options") or []))))
    return 0


def cmd_decide(args, client):
    if args.reopen:
        res = client.post("/api/decisions/reopen", {"id": args.id})
    else:
        if not args.choice and not args.note:
            raise UsageError("give a choice (or 'other' with --note)")
        res = client.post("/api/decisions/answer", {"id": args.id, "choice": args.choice, "note": args.note})
    if not res.get("ok"):
        raise NavError((res.get("error") or {}).get("message") or "failed")
    print_json(res)
    return 0


def cmd_ask(args, client):
    res = client.post("/api/decisions/ask", {"question": args.question, "options": args.option, "lane": args.lane,
                                            "priority": args.priority, "deadline": args.deadline, "session": args.session,
                                            "source": args.source, "id": args.id})
    if not res.get("ok"):
        raise NavError((res.get("error") or {}).get("message") or "failed")
    print_json(res)
    return 0


def cmd_star(args, client, pinned):
    action = "star" if pinned else "unstar"
    if not has_selector(args):
        raise UsageError(f"{action} requires at least one filter; --limit alone is not a filter")
    tree = client.tree()
    items = filter_from_args(flatten_tree(tree), args, hidden_default="include")
    keys = item_keys(items)
    result = {
        "action": action,
        "count": len(keys),
        "keys": keys,
        "dry_run": bool(args.dry_run),
    }
    if keys and not args.dry_run:
        for key in keys:
            client.post("/api/pin", {"key": key, "pinned": pinned})
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_hide_group(args, client, hidden):
    action = "hide-group" if hidden else "unhide-group"
    tree = client.tree()
    groups = matching_groups(tree, args.repo, args.group)
    keys = [group_key(group["repo"], group["group"]) for group in groups]
    result = {
        "action": action,
        "count": len(keys),
        "keys": keys,
        "groups": [
            {"repo": group["repo"], "group": group["group"], "hidden": group["hidden"]}
            for group in groups
        ],
        "dry_run": bool(args.dry_run),
    }
    if keys and not args.dry_run:
        client.post("/api/hide", {"keys": keys, "hidden": hidden})
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_hide_repo(args, client, hidden):
    action = "hide-repo" if hidden else "unhide-repo"
    tree = client.tree()
    repos = matching_repos(tree, args.repo)
    keys = [repo_key(repo["key"]) for repo in repos]
    result = {
        "action": action,
        "count": len(keys),
        "keys": keys,
        "repos": [
            {"repo": repo["key"], "name": repo["name"], "hidden": bool(repo.get("hidden"))}
            for repo in repos
        ],
        "dry_run": bool(args.dry_run),
    }
    if keys and not args.dry_run:
        client.post("/api/hide", {"keys": keys, "hidden": hidden})
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_icon(args, client):
    if not args.repo and not args.group:
        raise UsageError("icon needs a target: --repo, --group, or both")
    tree = client.tree()
    if args.group:
        targets = [
            (
                group_key(group["repo"], group["group"]),
                {"kind": "group", "repo": group["repo"], "group": group["group"]},
            )
            for group in matching_groups(tree, args.repo, args.group)
        ]
    else:
        targets = [
            (repo_key(repo["key"]), {"kind": "repo", "repo": repo["key"], "name": repo["name"]})
            for repo in matching_repos(tree, args.repo)
        ]
    if not targets:
        raise UsageError("icon target matched nothing; check --repo/--group")
    icon = "" if args.clear else str(args.set_icon or "")
    result = {
        "action": "icon",
        "icon": icon,
        "count": len(targets),
        "keys": [key for key, _ in targets],
        "targets": [target for _, target in targets],
        "dry_run": bool(args.dry_run),
    }
    if not args.dry_run:
        for key, _ in targets:
            client.post("/api/icon", {"key": key, "icon": icon})
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
    return 0


def cmd_rename(args, client):
    target = args.target
    rest = list(args.rest or [])
    tree = client.tree()
    if target == "session":
        if len(rest) != 2:
            raise UsageError("rename session expects KEY and NEW_NAME")
        key, name = rest[0], rest[1]
        item = find_item(tree, key)
        payload = {
            "kind": "session",
            "key": key,
            "name": name,
            "sid": str((item or {}).get("sid") or ""),
            "engine": str((item or {}).get("engine") or ""),
        }
        described = {"kind": "session", "key": key, "found": item is not None}
    elif target == "group":
        if len(rest) != 1:
            raise UsageError("rename group expects NEW_NAME")
        if not args.repo or not args.group:
            raise UsageError("rename group requires --repo and --group")
        name = rest[0]
        matches = matching_groups(tree, args.repo, args.group)
        if len(matches) != 1:
            raise UsageError(f"rename group needs exactly one match, got {len(matches)}")
        group = matches[0]
        payload = {
            "kind": "group",
            "repo": group["repo"],
            "group": group["group"],
            "name": name,
            "orca_path": group["orca_path"],
        }
        described = {
            "kind": "group",
            "repo": group["repo"],
            "group": group["group"],
            "orca_path": group["orca_path"],
        }
    else:
        if len(rest) != 1:
            raise UsageError("rename repo expects NEW_NAME")
        if not args.repo:
            raise UsageError("rename repo requires --repo")
        name = rest[0]
        matches = matching_repos(tree, args.repo)
        if len(matches) != 1:
            raise UsageError(f"rename repo needs exactly one match, got {len(matches)}")
        repo = matches[0]
        payload = {"kind": "repo", "key": repo["key"], "name": name}
        described = {"kind": "repo", "repo": repo["key"], "name": repo["name"]}
    response = client.post("/api/rename", payload)
    result = {
        "action": "rename",
        "name": name,
        "target": described,
        "applied": response.get("applied") or [],
        "ok": bool(response.get("ok", True)),
    }
    if args.table:
        print(f"{described['kind']}: {name} (applied: {', '.join(result['applied']) or 'none'})")
    else:
        print_json(result)
    return 0


def cmd_audit(args, client):
    tree = client.tree()
    now = dt.datetime.now().timestamp()
    groups_by_repo = {}
    for group in collect_groups(tree):
        groups_by_repo.setdefault(group["repo"], []).append(group)
    repos = []
    for repo in collect_repos(tree):
        groups = []
        for group in groups_by_repo.get(repo["key"], []):
            items = group["items"]
            newest = max((float(item.get("mtime") or 0) for item in items), default=0.0)
            groups.append(
                {
                    "name": group["group"],
                    "count": len(items),
                    "starred": sum(1 for item in items if item.get("pinned")),
                    "hidden": group["hidden"],
                    "newest_age_days": round((now - newest) / 86400, 2) if newest else None,
                }
            )
        groups.sort(key=lambda entry: -(entry["newest_age_days"] or 0))
        newest = max((float(item.get("mtime") or 0) for item in repo["items"]), default=0.0)
        repos.append(
            {
                "key": repo["key"],
                "name": repo["name"],
                "sessions": len(repo["items"]),
                "starred": sum(1 for item in repo["items"] if item.get("pinned")),
                "hidden_count": sum(1 for item in repo["items"] if item.get("hidden")),
                "newest": newest,
                "newest_age_days": round((now - newest) / 86400, 2) if newest else None,
                "groups": groups,
            }
        )
    repos.sort(key=lambda repo: -repo["newest"])
    result = {
        "generated_at": now,
        "repos": repos,
        "totals": {
            "repos": len(repos),
            "sessions": sum(repo["sessions"] for repo in repos),
            "starred": sum(repo["starred"] for repo in repos),
            "hidden": sum(repo["hidden_count"] for repo in repos),
        },
    }
    if args.table:
        print_table(
            ["REPO", "SESSIONS", "STARRED", "HIDDEN", "NEWEST_AGE_DAYS", "GROUPS"],
            [
                [
                    repo["name"],
                    repo["sessions"],
                    repo["starred"],
                    repo["hidden_count"],
                    repo["newest_age_days"],
                    len(repo["groups"]),
                ]
                for repo in repos
            ],
        )
    else:
        print_json(result)
    return 0


ORCA_TIMEOUT = 45


def orca_activity(now, window_seconds):
    """Ask the Orca CLI which workspaces exist and which have an agent in them.

    Returns (attached_paths, silent_paths, workspace_paths, problems).
    "Attached" means a connected terminal that actually produced output inside
    the window: a terminal sitting silent since forever is not evidence of
    activity, so it lands in `silent_paths` and protects nothing. Orca being
    missing or slow must not stop a curation pass: it only means the pass falls
    back to the navigator's own liveness signals.
    """
    attached = set()
    silent = set()
    workspaces = set()
    problems = []

    def orca_json(*argv):
        proc = subprocess.run(
            ["orca", *argv, "--json"],
            capture_output=True,
            text=True,
            timeout=ORCA_TIMEOUT,
        )
        payload = json.loads(proc.stdout or "{}")
        if not payload.get("ok", True):
            raise RuntimeError(payload.get("error") or "orca returned an error")
        return payload.get("result") or {}

    try:
        for wt in orca_json("worktree", "list").get("worktrees") or []:
            path = str(wt.get("path") or "")
            if path:
                workspaces.add(path)
    except Exception as exc:
        problems.append(f"worktrees: {exc}")

    try:
        for term in orca_json("terminal", "list").get("terminals") or []:
            path = str(term.get("worktreePath") or "")
            if not path or not term.get("connected") or term.get("orphaned"):
                continue
            last = term.get("lastOutputAt")
            if last and now - float(last) / 1000.0 <= window_seconds:
                attached.add(path)
            else:
                silent.add(path)
    except Exception as exc:
        problems.append(f"terminals: {exc}")

    return attached, silent, workspaces, problems


def cmd_curate(args, client):
    """Decide what is noise: stale groups, sessions and projects to hide.

    A session is signal when it is running, waiting on the user, or starred, or
    when the Orca workspace it belongs to has an agent that spoke recently.
    Everything else that has been quiet for longer than --older is a candidate
    for hiding. Starred items are never proposed.
    """
    tree = client.tree()
    now = dt.datetime.now().timestamp()
    window = parse_age(args.older)
    cutoff = now - window
    attached, silent, workspaces, problems = orca_activity(now, window)

    def is_active(item):
        return bool(item.get("live") or item.get("needs_you"))

    def group_records():
        records = []
        for group in collect_groups(tree):
            items = group["items"]
            newest = max((float(i.get("mtime") or 0) for i in items), default=0.0)
            live = any(is_active(i) for i in items)
            pinned = bool(group.get("pinned")) or any(i.get("pinned") for i in items)
            in_workspace = any(
                str(i.get("orca_path") or "") in attached for i in items if i.get("orca_path")
            )
            records.append(
                {
                    "repo": group["repo"],
                    "repo_name": group["repo_name"],
                    "group": group["group"],
                    "key": group_key(group["repo"], group["group"]),
                    "sessions": len(items),
                    "newest": newest,
                    "newest_age_days": round((now - newest) / 86400, 2) if newest else None,
                    "live": live,
                    "orca_attached": in_workspace,
                    "starred": pinned,
                    "hidden": bool(group.get("hidden")),
                    "items": items,
                }
            )
        return records

    groups = group_records()
    repos = collect_repos(tree)
    items_by_repo = {}
    for group in groups:
        items_by_repo.setdefault(group["repo"], []).extend(group["items"])

    # a project is noise only when every one of its groups is
    hide_repos = []
    repo_stale = {}
    for repo in repos:
        repo_groups = [g for g in groups if g["repo"] == repo["key"]]
        newest = max((g["newest"] for g in repo_groups), default=0.0)
        active = any(g["live"] or g["orca_attached"] for g in repo_groups)
        pinned = bool(repo.get("pinned")) or any(g["starred"] for g in repo_groups)
        repo_stale[repo["key"]] = (not active) and newest < cutoff and not pinned
        if repo_stale[repo["key"]] and not repo.get("hidden") and repo["items"]:
            hide_repos.append(
                {
                    "key": repo_key(repo["key"]),
                    "repo": repo["key"],
                    "name": repo["name"],
                    "sessions": len(repo["items"]),
                    "newest_age_days": round((now - newest) / 86400, 2) if newest else None,
                    "reason": "no active session, quiet for over %s" % args.older,
                }
            )

    hidden_repo_keys = {r["repo"] for r in hide_repos}
    # starred (or already hidden) containers protect everything below them, so
    # protection is inherited rather than level-by-level
    protected_repos = {r["key"] for r in repos if r.get("hidden") or r.get("pinned")}
    hide_groups = []
    for group in groups:
        if group["repo"] in hidden_repo_keys or group["repo"] in protected_repos:
            continue
        if group["hidden"] or group["starred"] or not group["items"]:
            continue
        if group["live"] or group["orca_attached"]:
            continue
        if group["newest"] >= cutoff:
            continue
        hide_groups.append(
            {
                "key": group["key"],
                "repo": group["repo"],
                "repo_name": group["repo_name"],
                "group": group["group"],
                "sessions": group["sessions"],
                "newest_age_days": group["newest_age_days"],
                "reason": "quiet for over %s, no live agent" % args.older,
            }
        )

    hidden_group_keys = {(g["repo"], g["group"]) for g in hide_groups}
    hide_sessions = []
    for group in groups:
        if group["repo"] in hidden_repo_keys or group["repo"] in protected_repos:
            continue
        if group["hidden"] or group["starred"] or (group["repo"], group["group"]) in hidden_group_keys:
            continue
        for item in group["items"]:
            if item.get("hidden") or item.get("pinned") or is_active(item):
                continue
            mtime = float(item.get("mtime") or 0)
            if mtime >= cutoff:
                continue
            hide_sessions.append(
                {
                    "key": item.get("key"),
                    "repo": group["repo"],
                    "repo_name": group["repo_name"],
                    "group": group["group"],
                    "engine": item.get("engine"),
                    "original": item.get("original"),
                    "age_days": round((now - mtime) / 86400, 2),
                    "reason": "idle for over %s" % args.older,
                }
            )

    star_candidates = [
        {
            "key": group["key"],
            "repo": group["repo"],
            "group": group["group"],
            "sessions": group["sessions"],
            "reason": "live now" if group["live"] else "agent attached in this workspace",
        }
        for group in groups
        if (group["live"] or group["orca_attached"]) and not group["starred"] and group["items"]
    ]

    keys = [entry["key"] for entry in (hide_repos + hide_groups + hide_sessions) if entry.get("key")]
    applied = False
    if args.apply and keys:
        client.post("/api/hide", {"keys": keys, "hidden": True})
        applied = True
    starred = []
    if args.star_active and args.apply:
        for entry in star_candidates:
            client.post("/api/pin", {"key": entry["key"], "pinned": True})
            starred.append(entry["key"])

    result = {
        "generated_at": now,
        "older": args.older,
        "applied": applied,
        "orca": {
            "attached_workspaces": sorted(attached),
            "silent_workspaces": sorted(silent),
            "known_workspaces": len(workspaces),
            "problems": problems,
        },
        "totals": {
            "groups": len(groups),
            "sessions": sum(g["sessions"] for g in groups),
            "live": sum(1 for g in groups if g["live"]),
            "starred": sum(1 for g in groups if g["starred"]),
            "hidden": sum(1 for g in groups if g["hidden"]),
        },
        "hide_repos": hide_repos,
        "hide_groups": hide_groups,
        "hide_sessions": hide_sessions,
        "star_candidates": star_candidates,
        "starred": starred,
        "hidden_keys": keys,
    }
    if args.table:
        print_table(
            ["ACTION", "REPO", "GROUP", "SESSIONS", "AGE_DAYS", "REASON"],
            [
                [
                    "hide-repo",
                    entry["name"],
                    "-",
                    entry["sessions"],
                    entry["newest_age_days"],
                    entry["reason"],
                ]
                for entry in hide_repos
            ]
            + [
                [
                    "hide-group",
                    entry["repo_name"],
                    entry["group"],
                    entry["sessions"],
                    entry["newest_age_days"],
                    entry["reason"],
                ]
                for entry in hide_groups
            ]
            + [
                [
                    "hide-session",
                    entry["repo_name"],
                    entry["group"],
                    1,
                    entry["age_days"],
                    entry["reason"],
                ]
                for entry in hide_sessions
            ],
        )
    else:
        print_json(result)
    return 0


def resolve_session(client, target):
    """(sid, engine, item) for a key, a session id or a unique id prefix.
    Sessions the navigator never lists (headless runs) still work by raw id."""
    target = str(target or "").strip()
    if not target:
        raise UsageError("a session key or id is required")
    items = flatten_tree(client.tree())
    exact = [i for i in items if target in (str(i.get("key")), str(i.get("sid")))]
    if not exact:
        exact = [i for i in items if str(i.get("sid") or "").startswith(target)
                 or str(i.get("key") or "").startswith(target)]
    if len(exact) > 1:
        raise UsageError("%r matches %d sessions; use the full key" % (target, len(exact)))
    if exact:
        it = exact[0]
        return str(it.get("sid") or ""), str(it.get("engine") or ""), it
    if ":" in target:
        engine, sid = target.split(":", 1)
        return sid, engine, None
    return target, "", None


def flatten_events(events):
    """[(role, text)] from a conversation payload, tools and thinking folded to one line."""
    out = []
    for ev in events or []:
        role = str(ev.get("type") or ev.get("role") or "event")
        parts = []
        if isinstance(ev.get("text"), str):
            parts.append(ev["text"])
        for b in ev.get("blocks") or []:
            kind = str(b.get("kind") or b.get("type") or "")
            txt = b.get("text") if isinstance(b.get("text"), str) else ""
            if kind in ("text", "markdown", "") and txt:
                parts.append(txt)
            elif kind in ("tool", "tool_use", "tool_call", "command"):
                parts.append("[tool] " + str(b.get("name") or b.get("title") or txt)[:120])
        text = "\n".join(p for p in parts if p).strip()
        if text:
            out.append((role, text))
    return out


def fetch_events(client, sid, engine):
    path = ("/api/pi/transcript?sid=" + urllib.parse.quote(sid)) if engine == "pi" \
        else ("/api/conversations/" + urllib.parse.quote(sid))
    return client._request(path).get("events") or []


def cmd_read(args, client):
    sid, engine, item = resolve_session(client, args.target)
    msgs = flatten_events(fetch_events(client, sid, engine))[-max(1, args.last):]
    if args.table:
        for role, text in msgs:
            print("%s: %s\n" % (role.upper(), text[:args.chars]))
        return 0
    print_json({"sid": sid, "engine": engine, "title": (item or {}).get("nick") or (item or {}).get("original"),
                "messages": [{"role": r, "text": x[:args.chars]} for r, x in msgs]})
    return 0


def cmd_send(args, client):
    sid, engine, item = resolve_session(client, args.target)
    text = args.text.strip()
    if not text:
        raise UsageError("empty message")
    title = (item or {}).get("nick") or (item or {}).get("original") or ""
    if args.dry_run:
        print_json({"dry_run": True, "sid": sid, "engine": engine, "title": title, "text": text})
        return 0
    before = len(fetch_events(client, sid, engine)) if args.wait else 0
    if engine == "pi":  # Pi has no remote input: the navigator resumes the session headlessly
        res = client.post("/api/pi/send", {"sid": sid, "text": text})
        if res.get("ok") is False:
            raise NavError("Pi send failed: %s" % (res.get("error") or res))
    else:
        res = client.post("/api/inject-input", {"session_id": sid, "text": text})
        if res.get("ok") is False:
            raise NavError("CCC refused the message: %s" % (res.get("error") or res))
    out = {"sent": True, "sid": sid, "engine": engine, "title": title, "ccc": res}
    if args.wait:
        deadline = time.time() + args.wait
        reply = None
        while time.time() < deadline and reply is None:
            time.sleep(3)
            try:
                msgs = flatten_events(fetch_events(client, sid, engine)[before:])
            except NavError:
                continue
            seen_user = False
            for role, x in msgs:
                if role.startswith("user") and text[:40] in x:
                    seen_user = True
                elif seen_user and role == "assistant" and not x.startswith("[tool]"):
                    reply = x
        out["reply"] = reply
        out["waited"] = True
        if reply is None:
            out["note"] = "no reply within %ds (it may still be working)" % args.wait
    print_json(out)
    return 0


def _note_key(client, target):
    sid, engine, item = resolve_session(client, target)
    if item:
        return item["key"], item
    if not engine:
        raise UsageError("%r is not a listed session; use engine:id" % target)
    return "%s:%s" % (engine, sid), None


def cmd_note(args, client):
    key, item = _note_key(client, args.target)
    if args.rm is not None:
        res = client.post("/api/note", {"key": key, "remove": args.rm - 1})
        print_json({"key": key, "removed": args.rm, "ok": res.get("ok")})
        return 0
    if not args.text.strip():
        raise UsageError("empty note")
    res = client.post("/api/note", {"key": key, "text": args.text, "by": args.by})
    print_json({"key": key, "title": (item or {}).get("nick") or (item or {}).get("original"), "note": res.get("note")})
    return 0


def cmd_notes(args, client):
    if args.target:
        key, item = _note_key(client, args.target)
        notes = {key: client._request("/api/notes?key=" + urllib.parse.quote(key)).get("notes") or []}
    else:
        notes = client._request("/api/notes").get("notes") or {}
    if args.table:
        for key, lst in notes.items():
            print(key)
            for n, note in enumerate(lst, 1):
                print("  %d. [%s %s] %s" % (n, _display_time(note.get("ts")), note.get("by", ""), note.get("text", "")))
        return 0
    print_json(notes)
    return 0


def cmd_mark(args, client):
    key, item = _note_key(client, args.target)
    label = "" if args.label.lower() == "clear" else args.label.lower()
    # clearing must go through as the owner, then the sorter is free to label it again
    client.post("/api/triage", {"entries": {key: {"label": label, "reason": args.reason, "by": "you"}}})
    print_json({"key": key, "label": label or None, "by": "you"})
    return 0


def cmd_triage(args, client):
    import triage
    res = triage.run_pass(client, args.window, args.dry_run)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    if only:
        res["results"] = [r for r in res["results"] if r.get("label") in only]
    if args.table:
        print("counts: " + ", ".join("%s %d" % kv for kv in sorted(res["counts"].items())))
        order = {k: i for i, k in enumerate(["needs-you", "blocked", "working", "done", "idle", "stale"])}
        rows = sorted(res["results"], key=lambda r: order.get(r.get("label"), 9))
        print_table(["LABEL", "KEY", "TITLE", "WHY"],
                    [[r.get("label"), r["key"], (r.get("title") or "")[:40], (r.get("reason") or "")[:70]] for r in rows])
        return 0
    print_json(res)
    return 0


BRIEF_ORDER = ["needs-you", "blocked", "working", "done", "idle", "stale"]


def cmd_brief(args, client):
    """The live picture, from labels the triage worker already saved: no transcript reads."""
    tree = client._request("/api/tree?window=%s" % args.window)
    projects = {}
    for it in flatten_tree(tree):
        if it.get("hidden"):
            continue
        label = "working" if it.get("live") else ("needs-you" if it.get("needs_you") else (it.get("triage") or "idle"))
        p = projects.setdefault(it.get("repo_name") or it.get("repo") or "?", {"counts": {}, "attention": [], "working": []})
        p["counts"][label] = p["counts"].get(label, 0) + 1
        row = {"key": it["key"], "title": it.get("nick") or it.get("original"), "group": it.get("group") or "",
               "why": it.get("triage_reason") or "", "note": it.get("last_note") or "", "by": it.get("triage_by") or ""}
        if label in ("needs-you", "blocked"):
            p["attention"].append(dict(row, label=label))
        elif label == "working":
            p["working"].append(row)
    rank = lambda kv: (-len(kv[1]["attention"]), -len(kv[1]["working"]), kv[0].lower())
    out = {"at": time.strftime("%Y-%m-%d %H:%M"), "window": args.window,
           "totals": {k: sum(p["counts"].get(k, 0) for p in projects.values()) for k in BRIEF_ORDER},
           "projects": dict(sorted(projects.items(), key=rank))}
    if not args.table:
        print_json(out)
        return 0
    t = out["totals"]
    print("%s  |  %d need you, %d blocked, %d working, %d done" % (out["at"], t["needs-you"], t["blocked"], t["working"], t["done"]))
    for name, p in out["projects"].items():
        if not p["attention"] and not p["working"]:
            continue
        counts = ", ".join("%s %d" % (k, p["counts"][k]) for k in BRIEF_ORDER if p["counts"].get(k))
        print("\n%s  (%s)" % (name, counts))
        for r in p["attention"]:
            print("  ! %-9s %s  -- %s" % (r["label"], (r["title"] or "")[:50], (r["why"] or "")[:90]))
            if r["note"]:
                print("              note: %s" % r["note"][:100])
        for r in p["working"]:
            print("  > working   %s" % (r["title"] or "")[:60])
    return 0


def parse_workspace_link(link):
    """(repo_key, group_name, orca_path) from a copied workspace link."""
    frag = urllib.parse.urlparse(link).fragment if "#" in link else link.lstrip("#")
    q = urllib.parse.parse_qs(frag)
    key = (q.get("g") or [""])[0]
    if key.startswith(GROUP_KEY_PREFIX):
        key = key[len(GROUP_KEY_PREFIX):]
    if "|" not in key:
        raise UsageError("not a workspace link: expected #g=g:<project>|<workspace>")
    repo, group = key.rsplit("|", 1)
    return repo, group, (q.get("p") or [""])[0]


def cmd_ws(args, client):
    repo_key, group_name, path = parse_workspace_link(args.link)
    tree = client.tree()  # window=all, hidden included
    repo = next((r for r in tree.get("repos") or [] if r.get("key") == repo_key), None)
    if repo is None:
        raise NavError("no project with key %r" % repo_key)
    grp = next((g for g in repo.get("groups") or [] if g.get("name") == group_name), None) \
        or next((g for g in repo.get("groups") or [] if path and g.get("orca_path") == path), None)
    if grp is None:
        raise NavError("workspace %r not found in %s (renamed? try its path: %s)" % (group_name, repo.get("name"), path or "unknown"))
    items = sorted(grp.get("items") or [], key=lambda i: -(i.get("mtime") or 0))
    out = {"project": repo.get("name"), "project_key": repo_key, "workspace": grp.get("name"),
           "orca_path": grp.get("orca_path") or path, "hidden_workspace": bool(grp.get("hidden")),
           "sessions": len(items), "hidden_sessions": sum(1 for i in items if i.get("hidden")),
           "items": [{"key": i["key"], "title": i.get("nick") or i.get("original"), "engine": i.get("engine"),
                      "triage": i.get("triage") or ("working" if i.get("live") else ""), "hidden": bool(i.get("hidden")),
                      "mtime": i.get("mtime")} for i in items]}
    if args.table:
        print("%s / %s   %s" % (out["project"], out["workspace"], out["orca_path"]))
        print("%d sessions, %d hidden" % (out["sessions"], out["hidden_sessions"]))
        print_table(["KEY", "TITLE", "ENGINE", "STATUS", "HIDDEN", "LAST"],
                    [[i["key"], (i["title"] or "")[:44], i["engine"], i["triage"], "yes" if i["hidden"] else "", _display_time(i["mtime"])] for i in out["items"]])
        return 0
    print_json(out)
    return 0


def _tunnel_state():
    try:
        out = subprocess.run(["launchctl", "print", "gui/%d/com.standup-os.nav-tunnel" % os.getuid()],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    m = re.search(r"^\s*state = (\S+)", out, re.M)
    return m.group(1) if m else "not installed"


def cmd_remote(args, client):
    if args.action == "pair":
        res = client.post("/api/remote/pair", {})
        if args.table:
            print(res["link"])
            print("works once, expires %s" % _display_time(res["expires_at"]))
            return 0
        print_json(res)
        return 0
    if args.action == "revoke":
        if not args.target:
            raise UsageError("revoke needs a session id prefix (from `nav remote sessions`) or 'all'")
        print_json(client.post("/api/remote/revoke", {"id": args.target}))
        return 0
    info = client._request("/api/remote/sessions")
    if args.action == "sessions":
        if args.table:
            print_table(["ID", "PHONE", "LAST SEEN", "EXPIRES"],
                        [[x["id"], x["label"][:50], _display_time(x["last_seen"]), _display_time(x["expires"])]
                         for x in info["sessions"]])
            return 0
        print_json(info["sessions"])
        return 0
    out = {"enabled": info["enabled"], "public_host": info["public_host"], "tunnel": _tunnel_state(),
           "paired_phones": len(info["sessions"])}
    if args.table:
        print("remote access: %s  host: %s  tunnel: %s  paired phones: %d" % (
            "on" if out["enabled"] else "off", out["public_host"] or "-", out["tunnel"], out["paired_phones"]))
        return 0
    print_json(out)
    return 0


def cmd_nick(args, client):
    client.post("/api/nickname", {"key": args.key, "name": args.name})
    result = {"key": args.key, "name": args.name}
    if args.table:
        print(f"{args.key}: {args.name}")
    else:
        print_json(result)
    return 0


def cmd_groups(args, client):
    tree = client.tree()
    repos = [repo for repo in tree.get("repos") or [] if isinstance(repo, dict)]
    exact = [repo for repo in repos if str(repo.get("key") or "") == args.repo]
    matches = exact or [
        repo
        for repo in repos
        if _contains(repo.get("key"), args.repo) or _contains(repo.get("name"), args.repo)
    ]
    details = []
    for repo in matches:
        names = set(repo.get("group_names") or [])
        for group in repo.get("groups") or []:
            if isinstance(group, dict):
                names.add(str(group.get("name") or ""))
        details.append(
            {
                "repo": str(repo.get("key") or ""),
                "name": str(repo.get("name") or ""),
                "groups": sorted(names, key=str.casefold),
            }
        )
    result = {
        "repo": details[0]["repo"] if len(details) == 1 else args.repo,
        "groups": details[0]["groups"] if len(details) == 1 else [],
        "matches": details,
    }
    if args.table:
        rows = [[detail["repo"], detail["name"], ", ".join(detail["groups"])] for detail in details]
        print_table(["REPO", "NAME", "GROUPS"], rows)
    else:
        print_json(result)
    return 0


def cmd_status(args, client):
    tree = client.tree()
    items = flatten_tree(tree)
    hidden = sum(1 for item in items if item.get("hidden"))
    live = sum(1 for item in items if item.get("live"))
    group_count = sum(len(repo.get("groups") or []) for repo in tree.get("repos") or [])
    ccc = ((tree.get("sources") or {}).get("ccc") or {})
    result = {
        "navigator": {"reachable": True, "url": client.base_url},
        "ccc": {
            "reachable": bool(ccc.get("ok")),
            "base": ccc.get("base", ""),
            "stale": bool(ccc.get("stale")),
        },
        "counts": {
            "repos": len(tree.get("repos") or []),
            "groups": group_count,
            "sessions": len(items),
            "visible": len(items) - hidden,
            "hidden": hidden,
            "live": live,
        },
        "generated_at": tree.get("generated_at"),
    }
    if args.table:
        rows = [
            ["navigator", "reachable", result["navigator"]["reachable"]],
            ["ccc", "reachable", result["ccc"]["reachable"]],
            *[[name, "count", count] for name, count in result["counts"].items()],
        ]
        print_table(["METRIC", "KIND", "VALUE"], rows)
    else:
        print_json(result)
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    limit = getattr(args, "limit", None)
    if limit is not None and limit < 0:
        parser.error("--limit must be zero or greater")

    client = NavigatorClient(args.url)
    try:
        if args.command == "ls":
            return cmd_ls(args, client)
        if args.command in ("hide", "unhide"):
            return cmd_hide(args, client, args.command)
        if args.command == "move":
            return cmd_move(args, client)
        if args.command == "decisions":
            return cmd_decisions(args, client)
        if args.command == "decide":
            return cmd_decide(args, client)
        if args.command == "ask":
            return cmd_ask(args, client)
        if args.command == "move-project":
            return cmd_move_project(args, client)
        if args.command == "unhide-all":
            return cmd_unhide_all(args, client)
        if args.command == "copilot":
            return cmd_copilot(args, client)
        if args.command == "desktop-groups":
            return cmd_desktop_groups(args, client)
        if args.command == "map-desktop-group":
            return cmd_map_desktop_group(args, client)
        if args.command in ("star", "unstar"):
            return cmd_star(args, client, args.command == "star")
        if args.command in ("hide-group", "unhide-group"):
            return cmd_hide_group(args, client, args.command == "hide-group")
        if args.command in ("hide-repo", "unhide-repo"):
            return cmd_hide_repo(args, client, args.command == "hide-repo")
        if args.command == "icon":
            return cmd_icon(args, client)
        if args.command == "rename":
            return cmd_rename(args, client)
        if args.command == "audit":
            return cmd_audit(args, client)
        if args.command == "curate":
            return cmd_curate(args, client)
        if args.command == "nick":
            return cmd_nick(args, client)
        if args.command == "groups":
            return cmd_groups(args, client)
        if args.command == "status":
            return cmd_status(args, client)
        if args.command == "send":
            return cmd_send(args, client)
        if args.command == "read":
            return cmd_read(args, client)
        if args.command == "note":
            return cmd_note(args, client)
        if args.command == "notes":
            return cmd_notes(args, client)
        if args.command == "mark":
            return cmd_mark(args, client)
        if args.command == "triage":
            return cmd_triage(args, client)
        if args.command == "brief":
            return cmd_brief(args, client)
        if args.command == "ws":
            return cmd_ws(args, client)
        if args.command == "remote":
            return cmd_remote(args, client)
    except UsageError as exc:
        print_error(exc)
        return 2
    except NavError as exc:
        print_error(exc)
        return 1
    except BrokenPipeError:
        # a downstream reader (head, less) closed the pipe: exit quietly
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0

    print_error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
