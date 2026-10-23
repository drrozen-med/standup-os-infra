#!/usr/bin/env python3
"""Command-line client for the local CCC Navigator JSON API.

The navigator must already be running. By default this command talks to
http://127.0.0.1:8095; override that with --url or CCC_NAV_URL.

Every command emits JSON by default. Use --table for human-readable output.
Batch mutations support --dry-run so the exact affected session keys can be
inspected before making changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8095"
REQUEST_TIMEOUT = 10
AGE_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$", re.IGNORECASE)
AGE_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


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
        headers = {"Accept": "application/json"}
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
        repo_key = str(repo.get("key") or "")
        for group in repo.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("name") or "")
            for raw_item in group.get("items") or []:
                if not isinstance(raw_item, dict):
                    continue
                item = dict(raw_item)
                item["repo"] = repo_key
                item["repo_name"] = str(repo.get("name") or "")
                item["repo_path"] = str(repo.get("path") or "")
                if not item.get("group"):
                    item["group"] = group_name
                flattened.append(item)
    return flattened


def _contains(value, needle):
    return str(needle).casefold() in str(value or "").casefold()


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
    limit=None,
    now=None,
):
    """Apply CLI filters to flattened navigator items.

    hidden_mode is one of "exclude", "include", or "only".
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


def _add_filter_options(parser, *, with_dry_run=False):
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
    if with_dry_run:
        parser.add_argument("--dry-run", action="store_true", help="show affected keys without changing them")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_global_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ls = subparsers.add_parser("ls", help="list flattened sessions")
    _add_global_options(ls, subcommand=True)
    _add_filter_options(ls)

    for command, help_text in (("hide", "hide matching sessions"), ("unhide", "unhide matching sessions")):
        sub = subparsers.add_parser(command, help=help_text)
        _add_global_options(sub, subcommand=True)
        _add_filter_options(sub, with_dry_run=True)

    move = subparsers.add_parser("move", help="move matching sessions to a group")
    _add_global_options(move, subcommand=True)
    _add_filter_options(move, with_dry_run=True)
    move.add_argument("--to", required=True, metavar="GROUP", help="destination group name")

    nick = subparsers.add_parser("nick", help="set a session nickname")
    _add_global_options(nick, subcommand=True)
    nick.add_argument("key", help="session key")
    nick.add_argument("name", help="new nickname")

    groups = subparsers.add_parser("groups", help="list groups for a repo")
    _add_global_options(groups, subcommand=True)
    groups.add_argument("--repo", required=True, help="repo key or substring")

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
                "yes" if item.get("live") else "",
                "yes" if item.get("hidden") else "",
                _display_time(item.get("mtime")),
            ]
        )
    print_table(["KEY", "TITLE", "REPO", "GROUP", "ENGINE", "LIVE", "HIDDEN", "MTIME"], rows)


def print_mutation_table(result):
    print(f"count: {result['count']}")
    for key in result.get("keys", []):
        print(key)


def cmd_ls(args, client):
    tree = client.tree()
    items = filter_from_args(flatten_tree(tree), args)
    if args.table:
        print_items_table(items)
    else:
        print_json(items)
    return 0


def cmd_hide(args, client, action):
    if not has_selector(args):
        raise UsageError("hide/unhide require at least one filter; --limit alone is not a filter")
    tree = client.tree()
    default_mode = "exclude" if action == "hide" else "only"
    items = filter_from_args(flatten_tree(tree), args, hidden_default=default_mode)
    keys = [str(item.get("key")) for item in items if item.get("key")]
    result = {
        "action": action,
        "count": len(keys),
        "keys": keys,
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
    tree = client.tree()
    items = filter_from_args(flatten_tree(tree), args)
    keys = [str(item.get("key")) for item in items if item.get("key")]
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
        "dry_run": bool(args.dry_run),
    }
    if args.table:
        print_mutation_table(result)
    else:
        print_json(result)
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
        if args.command == "nick":
            return cmd_nick(args, client)
        if args.command == "groups":
            return cmd_groups(args, client)
        if args.command == "status":
            return cmd_status(args, client)
    except UsageError as exc:
        print_error(exc)
        return 2
    except NavError as exc:
        print_error(exc)
        return 1

    print_error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
