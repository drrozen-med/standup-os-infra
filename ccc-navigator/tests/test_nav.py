import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nav  # noqa: E402


class NavFilters(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000
        self.items = [
            {
                "key": "claude:1", "original": "Billing fix", "nick": "",
                "repo": "/code/app", "repo_name": "app", "group": "infra",
                "engine": "claude", "live": True, "mtime": self.now - 1800,
            },
            {
                "key": "codex:2", "original": "Docs update", "nick": "Nightly docs",
                "repo": "/code/site", "repo_name": "site", "group": "docs",
                "engine": "codex", "live": False, "mtime": self.now - 20 * 86400,
                "hidden": True,
            },
            {
                "key": "devin:3", "original": "Deploy", "nick": "",
                "repo": "/code/app", "repo_name": "app", "group": "infra",
                "engine": "devin", "live": False, "mtime": self.now - 3 * 3600,
            },
        ]

    def test_flatten_tree_keeps_repo_context(self):
        tree = {
            "repos": [{
                "key": "/code/app", "name": "app", "path": "/code/app",
                "groups": [{"name": "infra", "items": [self.items[0]]}],
            }]
        }
        flat = nav.flatten_tree(tree)
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["repo"], "/code/app")
        self.assertEqual(flat[0]["group"], "infra")

    def test_combined_filters(self):
        found = nav.filter_items(
            self.items, repo="app", group="infra", engine="claude",
            live=True, older=10 * 60, newer=3600, now=self.now,
        )
        self.assertEqual([item["key"] for item in found], ["claude:1"])

    def test_hidden_modes_and_match(self):
        self.assertEqual(
            [item["key"] for item in nav.filter_items(self.items, now=self.now)],
            ["claude:1", "devin:3"],
        )
        self.assertEqual(
            [item["key"] for item in nav.filter_items(self.items, hidden_mode="include", now=self.now)],
            ["claude:1", "codex:2", "devin:3"],
        )
        self.assertEqual(
            [item["key"] for item in nav.filter_items(self.items, hidden_mode="only", now=self.now)],
            ["codex:2"],
        )
        self.assertEqual(
            [item["key"] for item in nav.filter_items(
                self.items, match="NIGHTLY", hidden_mode="include", now=self.now)],
            ["codex:2"],
        )

    def test_hidden_flag_alias(self):
        parser = nav.build_parser()
        self.assertTrue(parser.parse_args(["ls", "--hidden"]).include_hidden)
        self.assertTrue(parser.parse_args(["ls", "--include-hidden"]).include_hidden)

    def test_age_parsing_and_limit(self):
        self.assertEqual(nav.parse_age("14d"), 14 * 86400)
        self.assertEqual(nav.parse_age("6h"), 6 * 3600)
        with self.assertRaises(argparse.ArgumentTypeError):
            nav.parse_age("later")
        found = nav.filter_items(self.items, limit=1, now=self.now)
        self.assertEqual(len(found), 1)


class NavStarredFilters(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000
        self.items = [
            {"key": "claude:1", "original": "Billing fix", "nick": "", "repo": "/code/app",
             "repo_name": "app", "group": "infra", "engine": "claude", "live": True,
             "mtime": self.now - 1800, "pinned": True},
            {"key": "codex:2", "original": "Docs", "nick": "", "repo": "/code/site",
             "repo_name": "site", "group": "docs", "engine": "codex", "live": False,
             "mtime": self.now - 7200},
            {"key": "devin:3", "original": "Deploy", "nick": "", "repo": "/code/app",
             "repo_name": "app", "group": "infra", "engine": "devin", "live": False,
             "mtime": self.now - 3600},
        ]

    def test_starred_filter_keeps_only_pinned(self):
        found = nav.filter_items(self.items, starred=True, now=self.now)
        self.assertEqual([item["key"] for item in found], ["claude:1"])

    def test_starred_filter_is_off_by_default(self):
        found = nav.filter_items(self.items, now=self.now)
        self.assertEqual(len(found), 3)

    def test_split_starred_protects_pinned(self):
        targets, skipped = nav.split_starred(self.items, False)
        self.assertEqual([item["key"] for item in targets], ["codex:2", "devin:3"])
        self.assertEqual([item["key"] for item in skipped], ["claude:1"])

    def test_split_starred_can_include_pinned(self):
        targets, skipped = nav.split_starred(self.items, True)
        self.assertEqual(len(targets), 3)
        self.assertEqual(skipped, [])

    def test_starred_counts_as_a_selector(self):
        parser = nav.build_parser()
        self.assertTrue(nav.has_selector(parser.parse_args(["hide", "--starred"])))
        self.assertFalse(nav.has_selector(parser.parse_args(["hide", "--limit", "3"])))

    def test_rank_argument_parsing(self):
        parser = nav.build_parser()
        args = parser.parse_args(["rank", "id1", "id2", "--why", "id1=why1", "--why", "id2=why2"])
        self.assertEqual(args.id, ["id1", "id2"])
        self.assertEqual(args.why, ["id1=why1", "id2=why2"])
        args_clear = parser.parse_args(["rank", "--clear"])
        self.assertTrue(args_clear.clear)


class NavKeyLogic(unittest.TestCase):
    def setUp(self):
        self.tree = {
            "icons": {"r:/code/app": "🚀"},
            "repos": [
                {
                    "key": "/code/app", "name": "app", "orig_name": "app", "path": "/code/app",
                    "hidden": False, "group_names": ["infra", "scratch"],
                    "groups": [
                        {"name": "infra", "hidden": False, "orca_path": "/code/app",
                         "items": [{"key": "claude:1", "mtime": 100.0, "pinned": True}]},
                        {"name": "scratch", "hidden": True, "orca_path": "",
                         "items": [{"key": "codex:2", "mtime": 50.0}]},
                    ],
                },
                {
                    "key": "/code/site", "name": "site", "orig_name": "site", "path": "/code/site",
                    "hidden": True, "group_names": ["docs"],
                    "groups": [
                        {"name": "docs", "hidden": False, "orca_path": "",
                         "items": [{"key": "devin:3", "mtime": 10.0}]},
                    ],
                },
            ],
        }

    def test_key_formats(self):
        self.assertEqual(nav.group_key("/code/app", "infra"), "g:/code/app|infra")
        self.assertEqual(nav.repo_key("/code/app"), "r:/code/app")

    def test_matching_repos_prefers_an_exact_key(self):
        exact = nav.matching_repos(self.tree, "/code/app")
        self.assertEqual([repo["key"] for repo in exact], ["/code/app"])
        loose = nav.matching_repos(self.tree, "app")
        self.assertEqual([repo["key"] for repo in loose], ["/code/app"])

    def test_matching_groups_filters_by_repo_and_name(self):
        groups = nav.matching_groups(self.tree, "/code/app", "infra")
        self.assertEqual([group["group"] for group in groups], ["infra"])
        self.assertEqual(groups[0]["hidden"], False)
        self.assertEqual(groups[0]["orca_path"], "/code/app")

    def test_collect_groups_keeps_empty_manual_groups(self):
        names = {group["group"] for group in nav.collect_groups(self.tree)}
        self.assertIn("scratch", names)
        self.assertIn("docs", names)

    def test_collect_repos_reads_hidden_flags(self):
        repos = nav.collect_repos(self.tree)
        self.assertEqual(repos[0]["hidden"], False)
        self.assertEqual(repos[1]["hidden"], True)
        self.assertEqual(repos[0]["items"][0]["key"], "claude:1")

    def test_find_item_by_key(self):
        self.assertEqual(nav.find_item(self.tree, "codex:2")["group"], "scratch")
        self.assertIsNone(nav.find_item(self.tree, "nope:9"))


class FakeClient:
    """Records the API calls a command makes instead of talking to a server."""

    def __init__(self, tree):
        self.tree_data = tree
        self.posts = []

    def tree(self):
        return self.tree_data

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {"ok": True, "applied": ["navigator"]}


def run(command, args, client):
    """Run a nav command with stdout captured; return the parsed JSON output."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = command(args, client)
    text = buffer.getvalue().strip()
    return code, (json.loads(text) if text.startswith("{") or text.startswith("[") else text)


def parse_fails(parser, argv):
    """argparse errors out with a usage message; swallow it and report it."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(argv)
        except SystemExit:
            return True
    return False


class NavCommands(unittest.TestCase):
    def setUp(self):
        self.tree = {
            "icons": {},
            "repos": [
                {
                    "key": "/code/app", "name": "app", "orig_name": "app", "path": "/code/app",
                    "hidden": False, "group_names": ["infra"],
                    "groups": [
                        {"name": "infra", "hidden": False, "orca_path": "/code/app",
                         "items": [
                             {"key": "claude:1", "sid": "s1", "engine": "claude", "mtime": 200.0,
                              "pinned": True, "hidden": False, "original": "Billing"},
                             {"key": "claude:2", "sid": "s2", "engine": "claude", "mtime": 100.0,
                              "pinned": False, "hidden": False, "original": "Docs"},
                         ]},
                    ],
                },
            ],
        }
        self.parser = nav.build_parser()

    def test_hide_skips_starred_sessions_by_default(self):
        args = self.parser.parse_args(["hide", "--repo", "app"])
        client = FakeClient(self.tree)
        _, result = run(lambda a, c: nav.cmd_hide(a, c, "hide"), args, client)
        self.assertEqual(result["keys"], ["claude:2"])
        self.assertEqual(result["skipped_starred"], ["claude:1"])
        self.assertEqual(client.posts[0][1]["keys"], ["claude:2"])

    def test_hide_dry_run_reports_keys_without_posting(self):
        args = self.parser.parse_args(["hide", "--repo", "app", "--dry-run"])
        client = FakeClient(self.tree)
        _, result = run(lambda a, c: nav.cmd_hide(a, c, "hide"), args, client)
        self.assertTrue(result["dry_run"])
        self.assertEqual(client.posts, [])

    def test_hide_include_starred_posts_all_keys(self):
        args = self.parser.parse_args(["hide", "--repo", "app", "--include-starred"])
        client = FakeClient(self.tree)
        _, result = run(lambda a, c: nav.cmd_hide(a, c, "hide"), args, client)
        self.assertEqual(result["keys"], ["claude:1", "claude:2"])
        self.assertEqual(len(client.posts), 1)
        path, payload = client.posts[0]
        self.assertEqual(path, "/api/hide")
        self.assertEqual(payload["hidden"], True)
        self.assertEqual(payload["keys"], ["claude:1", "claude:2"])

    def test_hide_requires_a_filter(self):
        args = self.parser.parse_args(["hide"])
        client = FakeClient(self.tree)
        with self.assertRaises(nav.UsageError):
            nav.cmd_hide(args, client, "hide")

    def test_move_requires_a_filter(self):
        args = self.parser.parse_args(["move", "--to", "infra"])
        client = FakeClient(self.tree)
        with self.assertRaises(nav.UsageError):
            nav.cmd_move(args, client)

    def test_move_skips_starred_sessions_by_default(self):
        args = self.parser.parse_args(["move", "--repo", "app", "--to", "infra", "--dry-run"])
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_move, args, client)
        self.assertEqual(result["keys"], ["claude:2"])
        self.assertEqual(result["skipped_starred"], ["claude:1"])
        self.assertEqual(client.posts, [])

    def test_move_project_by_key_posts_target(self):
        self.tree["repos"].append({"key": "/code/web", "name": "web", "path": "/code/web", "groups": []})
        args = self.parser.parse_args(["move-project", "--key", "claude:1", "--to", "web"])
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_move_project, args, client)
        self.assertEqual(result["keys"], ["claude:1"])          # starred, but named explicitly
        self.assertEqual(client.posts, [("/api/move-project", {"keys": ["claude:1"], "repo": "/code/web"})])

    def test_unhide_all_posts_scope(self):
        client = FakeClient(self.tree)
        run(nav.cmd_unhide_all, self.parser.parse_args(["unhide-all", "--repo", "app", "--group", "infra"]), client)
        run(nav.cmd_unhide_all, self.parser.parse_args(["unhide-all", "--repo", "app"]), client)
        self.assertEqual(client.posts, [("/api/unhide-scope", {"repo": "/code/app", "group": "infra"}),
                                        ("/api/unhide-scope", {"repo": "/code/app"})])

    def test_move_project_back_and_guards(self):
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_move_project, self.parser.parse_args(["move-project", "--key", "claude:2", "--back"]), client)
        self.assertEqual(client.posts[0][1], {"keys": ["claude:2"], "repo": ""})
        with self.assertRaises(nav.UsageError):      # no selector
            nav.cmd_move_project(self.parser.parse_args(["move-project", "--to", "app"]), client)
        with self.assertRaises(nav.NavError):        # unknown key
            nav.cmd_move_project(self.parser.parse_args(["move-project", "--key", "claude:9", "--to", "app"]), client)
        with self.assertRaises(nav.UsageError):      # ambiguous / no project
            nav.cmd_move_project(self.parser.parse_args(["move-project", "--key", "claude:2", "--to", "zzz"]), client)

    def test_move_can_include_starred_sessions(self):
        args = self.parser.parse_args(
            ["move", "--repo", "app", "--to", "infra", "--include-starred", "--dry-run"]
        )
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_move, args, client)
        self.assertEqual(result["keys"], ["claude:1", "claude:2"])
        self.assertEqual(result["skipped_starred"], [])

    def test_star_posts_pins(self):
        args = self.parser.parse_args(["star", "--repo", "app"])
        client = FakeClient(self.tree)
        run(lambda a, c: nav.cmd_star(a, c, True), args, client)
        self.assertEqual([path for path, _ in client.posts], ["/api/pin", "/api/pin"])
        self.assertEqual(client.posts[0][1], {"key": "claude:1", "pinned": True})

    def test_unstar_clears_pins(self):
        args = self.parser.parse_args(["unstar", "--repo", "app"])
        client = FakeClient(self.tree)
        run(lambda a, c: nav.cmd_star(a, c, False), args, client)
        self.assertEqual(client.posts[0][1], {"key": "claude:1", "pinned": False})

    def test_star_dry_run_does_not_post(self):
        args = self.parser.parse_args(["star", "--repo", "app", "--dry-run"])
        client = FakeClient(self.tree)
        _, result = run(lambda a, c: nav.cmd_star(a, c, True), args, client)
        self.assertEqual(result["count"], 2)
        self.assertEqual(client.posts, [])

    def test_hide_group_resolves_group_keys(self):
        args = self.parser.parse_args(["hide-group", "--repo", "/code/app", "--group", "infra"])
        client = FakeClient(self.tree)
        run(lambda a, c: nav.cmd_hide_group(a, c, True), args, client)
        self.assertEqual(client.posts[0][1], {"keys": ["g:/code/app|infra"], "hidden": True})

    def test_unhide_group_resolves_group_keys(self):
        args = self.parser.parse_args(["unhide-group", "--repo", "/code/app", "--group", "infra"])
        client = FakeClient(self.tree)
        run(lambda a, c: nav.cmd_hide_group(a, c, False), args, client)
        self.assertEqual(client.posts[0][1], {"keys": ["g:/code/app|infra"], "hidden": False})

    def test_hide_repo_resolves_repo_keys(self):
        args = self.parser.parse_args(["hide-repo", "--repo", "/code/app"])
        client = FakeClient(self.tree)
        run(lambda a, c: nav.cmd_hide_repo(a, c, True), args, client)
        self.assertEqual(client.posts[0][1], {"keys": ["r:/code/app"], "hidden": True})

    def test_icon_sets_the_group_key(self):
        args = self.parser.parse_args(["icon", "--repo", "/code/app", "--group", "infra", "--set", "🧪"])
        client = FakeClient(self.tree)
        run(nav.cmd_icon, args, client)
        self.assertEqual(client.posts[0], ("/api/icon", {"key": "g:/code/app|infra", "icon": "🧪"}))

    def test_icon_clear_sends_an_empty_icon(self):
        args = self.parser.parse_args(["icon", "--repo", "/code/app", "--clear"])
        client = FakeClient(self.tree)
        run(nav.cmd_icon, args, client)
        self.assertEqual(client.posts[0], ("/api/icon", {"key": "r:/code/app", "icon": ""}))

    def test_icon_without_a_target_is_refused(self):
        args = self.parser.parse_args(["icon", "--set", "🧪"])
        client = FakeClient(self.tree)
        with self.assertRaises(nav.UsageError):
            nav.cmd_icon(args, client)

    def test_rename_session_carries_sid_and_engine(self):
        args = self.parser.parse_args(["rename", "session", "claude:1", "Billing fix"])
        client = FakeClient(self.tree)
        run(nav.cmd_rename, args, client)
        self.assertEqual(
            client.posts[0][1],
            {"kind": "session", "key": "claude:1", "name": "Billing fix", "sid": "s1", "engine": "claude"},
        )

    def test_rename_group_carries_orca_path(self):
        args = self.parser.parse_args(["rename", "group", "--repo", "/code/app", "--group", "infra", "Ops"])
        client = FakeClient(self.tree)
        run(nav.cmd_rename, args, client)
        self.assertEqual(
            client.posts[0][1],
            {"kind": "group", "repo": "/code/app", "group": "infra", "name": "Ops", "orca_path": "/code/app"},
        )

    def test_rename_repo_uses_the_repo_key(self):
        args = self.parser.parse_args(["rename", "repo", "--repo", "/code/app", "Standup"])
        client = FakeClient(self.tree)
        run(nav.cmd_rename, args, client)
        self.assertEqual(
            client.posts[0][1], {"kind": "repo", "key": "/code/app", "name": "Standup"}
        )

    def test_rename_needs_the_right_arguments(self):
        client = FakeClient(self.tree)
        with self.assertRaises(nav.UsageError):
            nav.cmd_rename(self.parser.parse_args(["rename", "session", "claude:1"]), client)
        with self.assertRaises(nav.UsageError):
            nav.cmd_rename(self.parser.parse_args(["rename", "group", "Ops"]), client)

    def test_audit_summarises_repos_and_groups(self):
        args = self.parser.parse_args(["audit"])
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_audit, args, client)
        self.assertEqual(result["totals"]["repos"], 1)
        self.assertEqual(result["totals"]["sessions"], 2)
        self.assertEqual(result["totals"]["starred"], 1)
        repo = result["repos"][0]
        self.assertEqual(repo["name"], "app")
        self.assertEqual(repo["sessions"], 2)
        self.assertEqual(repo["starred"], 1)
        self.assertEqual(repo["groups"][0]["name"], "infra")
        self.assertEqual(repo["groups"][0]["count"], 2)
        self.assertEqual(repo["groups"][0]["starred"], 1)
        self.assertIsInstance(repo["groups"][0]["newest_age_days"], float)

    def test_ls_groups_lists_group_stats(self):
        args = self.parser.parse_args(["ls", "--groups"])
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_ls, args, client)
        self.assertEqual(result[0]["group"], "infra")
        self.assertEqual(result[0]["count"], 2)
        self.assertEqual(result[0]["starred_count"], 1)
        self.assertEqual(result[0]["repo"], "/code/app")

    def test_ls_repos_lists_repo_stats(self):
        args = self.parser.parse_args(["ls", "--repos"])
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_ls, args, client)
        self.assertEqual(result[0]["repo"], "/code/app")
        self.assertEqual(result[0]["count"], 2)
        self.assertEqual(result[0]["starred_count"], 1)


class NavParser(unittest.TestCase):
    def setUp(self):
        self.parser = nav.build_parser()

    def test_new_commands_exist(self):
        samples = {
            "star": ["star"],
            "unstar": ["unstar"],
            "hide-group": ["hide-group", "--repo", "app", "--group", "infra"],
            "unhide-group": ["unhide-group", "--repo", "app", "--group", "infra"],
            "hide-repo": ["hide-repo", "--repo", "app"],
            "unhide-repo": ["unhide-repo", "--repo", "app"],
            "icon": ["icon", "--repo", "app", "--set", "x"],
            "rename": ["rename", "session", "claude:1", "x"],
            "audit": ["audit"],
        }
        for command, argv in samples.items():
            self.assertEqual(self.parser.parse_args(argv).command, command)

    def test_order_parser_repos_and_groups(self):
        args = self.parser.parse_args(["order", "repos", "/r/A", "site"])
        self.assertEqual(args.command, "order")
        self.assertEqual(args.order_kind, "repos")
        self.assertEqual(args.keys, ["/r/A", "site"])
        args = self.parser.parse_args(["order", "groups", "--repo", "R", "a", "b"])
        self.assertEqual(args.command, "order")
        self.assertEqual(args.order_kind, "groups")
        self.assertEqual(args.repo, "R")
        self.assertEqual(args.names, ["a", "b"])

    def test_starred_filter_exists_where_it_should(self):
        for command in ("ls", "hide", "unhide", "move", "star"):
            extra = ["--to", "infra"] if command == "move" else []
            self.assertTrue(self.parser.parse_args([command] + extra + ["--starred"]).starred)

    def test_hide_group_requires_repo_and_group(self):
        self.assertTrue(parse_fails(self.parser, ["hide-group", "--repo", "app"]))
        self.assertTrue(parse_fails(self.parser, ["hide-group", "--group", "infra"]))

    def test_icon_requires_set_or_clear(self):
        self.assertTrue(parse_fails(self.parser, ["icon", "--repo", "app"]))

    def test_ls_groups_and_repos_are_exclusive(self):
        self.assertTrue(self.parser.parse_args(["ls", "--groups"]).groups)
        self.assertTrue(self.parser.parse_args(["ls", "--repos"]).repos)
        self.assertTrue(parse_fails(self.parser, ["ls", "--groups", "--repos"]))

    def test_rename_parses_the_target_and_name(self):
        args = self.parser.parse_args(["rename", "session", "claude:1", "Billing fix"])
        self.assertEqual(args.target, "session")
        self.assertEqual(args.rest, ["claude:1", "Billing fix"])
        args = self.parser.parse_args(["rename", "group", "--repo", "app", "--group", "infra", "Ops"])
        self.assertEqual(args.target, "group")
        self.assertEqual(args.rest, ["Ops"])
        self.assertEqual(args.repo, "app")
        self.assertEqual(args.group, "infra")


class Curate(unittest.TestCase):
    """curate decides what is noise: stale work gets proposed for hiding."""

    def setUp(self):
        self.now = 1_700_000_000
        self.parser = nav.build_parser()
        # one live group, one stale group, one stale session inside a live group
        self.tree = {
            "icons": {},
            "repos": [
                {
                    "key": "/code/app", "name": "app", "orig_name": "app", "path": "/code/app",
                    "hidden": False, "group_names": ["live", "stale"],
                    "groups": [
                        {"name": "live", "hidden": False, "orca_path": "/w/app/live",
                         "items": [
                             {"key": "claude:1", "sid": "s1", "engine": "claude",
                              "mtime": self.now - 60, "live": True, "pinned": False,
                              "hidden": False, "original": "Live work", "orca_path": "/w/app/live"},
                             {"key": "claude:2", "sid": "s2", "engine": "claude",
                              "mtime": self.now - 30 * 86400, "live": False, "pinned": False,
                              "hidden": False, "original": "Old session", "orca_path": "/w/app/live"},
                         ]},
                        {"name": "stale", "hidden": False, "orca_path": "/w/app/stale",
                         "items": [
                             {"key": "codex:3", "sid": "s3", "engine": "codex",
                              "mtime": self.now - 20 * 86400, "live": False, "pinned": False,
                              "hidden": False, "original": "Dead group", "orca_path": "/w/app/stale"},
                         ]},
                    ],
                },
            ],
        }
        self.attached = set()
        self._orca = nav.orca_activity
        nav.orca_activity = lambda now, window: (self.attached, set(), {"/w/app/live"}, [])
        self.addCleanup(lambda: setattr(nav, "orca_activity", self._orca))

    def curate(self, argv=("curate", "--older", "2d")):
        args = self.parser.parse_args(list(argv))
        client = FakeClient(self.tree)
        _, result = run(nav.cmd_curate, args, client)
        return result, client

    def test_proposal_only_changes_nothing(self):
        result, client = self.curate()
        self.assertFalse(result["applied"])
        self.assertEqual(client.posts, [])

    def test_stale_group_is_proposed(self):
        result, _ = self.curate()
        self.assertEqual([g["group"] for g in result["hide_groups"]], ["stale"])
        self.assertEqual(result["hide_groups"][0]["key"], "g:/code/app|stale")

    def test_live_group_is_kept(self):
        result, _ = self.curate()
        self.assertNotIn("live", [g["group"] for g in result["hide_groups"]])

    def test_stale_session_inside_live_group_is_proposed(self):
        result, _ = self.curate()
        self.assertEqual([s["key"] for s in result["hide_sessions"]], ["claude:2"])

    def test_orca_attached_workspace_is_kept(self):
        self.attached = {"/w/app/stale"}
        result, _ = self.curate()
        self.assertEqual(result["hide_groups"], [])

    def test_apply_posts_the_hidden_keys(self):
        result, client = self.curate(("curate", "--older", "2d", "--apply"))
        self.assertTrue(result["applied"])
        self.assertEqual(len(client.posts), 1)
        path, payload = client.posts[0]
        self.assertEqual(path, "/api/hide")
        self.assertEqual(payload["hidden"], True)
        self.assertIn("g:/code/app|stale", payload["keys"])
        self.assertIn("claude:2", payload["keys"])

    def test_starred_item_is_never_proposed(self):
        self.tree["repos"][0]["groups"][1]["items"][0]["pinned"] = True
        result, _ = self.curate()
        # the starred session keeps its whole group out of the proposal
        self.assertEqual(result["hide_groups"], [])
        self.assertNotIn("codex:3", [s["key"] for s in result["hide_sessions"]])

    def test_starred_session_in_live_group_is_not_proposed(self):
        self.tree["repos"][0]["groups"][0]["items"][1]["pinned"] = True
        result, _ = self.curate()
        self.assertNotIn("claude:2", [s["key"] for s in result["hide_sessions"]])

    def test_starred_group_is_never_proposed(self):
        self.tree["repos"][0]["groups"][1]["pinned"] = True
        result, _ = self.curate()
        self.assertEqual(result["hide_groups"], [])
        self.assertNotIn("codex:3", [s["key"] for s in result["hide_sessions"]])

    def test_starred_project_is_never_proposed(self):
        self.tree["repos"][0]["pinned"] = True
        result, _ = self.curate()
        self.assertEqual(result["hide_repos"], [])
        self.assertEqual(result["hide_groups"], [])
        self.assertEqual(result["hide_sessions"], [])

    def test_active_group_is_a_star_candidate(self):
        result, _ = self.curate()
        self.assertEqual([c["group"] for c in result["star_candidates"]], ["live"])

class WorkspaceLink(unittest.TestCase):
    def test_parses_a_copied_link(self):
        link = ("http://127.0.0.1:8095/#g=g%3A%2FUsers%2Fme%2Fprojects%2FboardsBridge%7Clab%20results"
                "&p=%2FUsers%2Fme%2Fprojects%2FboardsBridge")
        self.assertEqual(nav.parse_workspace_link(link),
                         ("/Users/me/projects/boardsBridge", "lab results", "/Users/me/projects/boardsBridge"))

    def test_accepts_the_bare_fragment_and_rejects_junk(self):
        self.assertEqual(nav.parse_workspace_link("#g=g:folder:tmp|main")[:2], ("folder:tmp", "main"))
        with self.assertRaises(nav.UsageError):
            nav.parse_workspace_link("http://127.0.0.1:8095/")


if __name__ == "__main__":
    unittest.main()
