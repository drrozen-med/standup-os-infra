"""One way to point at a session, group (workspace) or project. See issue #14 (R2).

    nav://session/<engine>:<id>
    nav://group/<project key>|<group name>      (both parts URL-encoded)
    nav://project/<project key>                 (URL-encoded)

parse_ref also takes the older forms, so nothing already copied stops working:
bare session keys (claude:<uuid>), g:/r: keys, the workspace "Copy link" URL
(.../#g=g:<project>|<group>&p=...), and a Claude desktop id (local_<uuid>).

resolve() matches exactly and never guesses; find() is the substring search,
and it only returns candidates.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

SCHEME = "nav://"
KINDS = ("session", "group", "project")
GROUP_PREFIX, PROJECT_PREFIX = "g:", "r:"
SESSION_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")
DESKTOP_RE = re.compile(r"^local_[0-9a-fA-F-]{8,40}$")
MAX_REF = 2000


class RefError(ValueError):
    """code is one of: bad_ref, unknown_desktop_id."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Ref:
    kind: str           # "session" | "group" | "project"
    key: str            # session key, or project key
    group: str = ""     # group name (kind == "group" only; "" is Ungrouped)

    def __str__(self):
        return format_ref(self)


def _q(text):
    return urllib.parse.quote(text, safe="")


def format_ref(ref):
    if ref.kind == "session":
        return SCHEME + "session/" + ref.key
    if ref.kind == "group":
        return SCHEME + "group/" + _q(ref.key) + "|" + _q(ref.group)
    if ref.kind == "project":
        return SCHEME + "project/" + _q(ref.key)
    raise RefError("bad_ref", "unknown ref kind: %r" % (ref.kind,))


def _group_key(body):
    """'<project>|<group>' from a g: key; the group name is the part after the last '|'."""
    if "|" not in body:
        raise RefError("bad_ref", "group key needs <project>|<group>")
    project, group = body.rsplit("|", 1)
    if not project:
        raise RefError("bad_ref", "group key has no project")
    return Ref("group", project, group)


def parse_ref(text, desktop_ids=None):
    """Ref from any supported form. desktop_ids maps local_<uuid> -> CLI session id."""
    text = str(text or "").strip()
    if not text or len(text) > MAX_REF:
        raise RefError("bad_ref", "empty or over-long ref")
    if text.startswith(SCHEME):
        kind, _, rest = text[len(SCHEME):].partition("/")
        if kind == "session" and SESSION_KEY_RE.match(rest):
            return Ref("session", rest)
        if kind == "group" and rest.count("|") == 1:
            project, group = (urllib.parse.unquote(p) for p in rest.split("|"))
            if project:
                return Ref("group", project, group)
        if kind == "project" and rest:
            return Ref("project", urllib.parse.unquote(rest))
        raise RefError("bad_ref", "not a valid %s ref: %s" % (kind or "nav", text[:120]))
    if "#" in text and "g=" in text:                       # workspace "Copy link" URL
        q = urllib.parse.parse_qs(text.split("#", 1)[1])
        key = (q.get("g") or [""])[0]
        if key.startswith(GROUP_PREFIX):
            return _group_key(key[len(GROUP_PREFIX):])
        raise RefError("bad_ref", "workspace link without a g:<project>|<group> key")
    if DESKTOP_RE.match(text):
        sid = (desktop_ids or {}).get(text)
        if not sid:
            raise RefError("unknown_desktop_id", "no Claude desktop session %s" % text)
        return Ref("session", "claude:" + sid)
    if text.startswith(GROUP_PREFIX):
        return _group_key(text[len(GROUP_PREFIX):])
    if text.startswith(PROJECT_PREFIX) and len(text) > len(PROJECT_PREFIX):
        return Ref("project", text[len(PROJECT_PREFIX):])
    if SESSION_KEY_RE.match(text):
        return Ref("session", text)
    raise RefError("bad_ref", "not a navigator ref: %s" % text[:120])


def _target(kind, repo, group=None, item=None):
    t = {"kind": kind, "project": repo["key"], "project_name": repo.get("name", "")}
    if group is not None:
        t["group"] = group.get("name", "")
    if item is not None:
        t.update(key=item["key"], name=item.get("nick") or item.get("original") or "")
    ref = Ref(kind, t["key"] if item is not None else repo["key"], t.get("group", ""))
    t["ref"] = format_ref(ref)
    return t


def resolve(ref, tree):
    """Exact matches for a Ref in a navigator tree (normally 0 or 1)."""
    out = []
    for repo in tree.get("repos") or []:
        if ref.kind == "project":
            if repo.get("key") == ref.key:
                out.append(_target("project", repo))
            continue
        for g in repo.get("groups") or []:
            if ref.kind == "group":
                if repo.get("key") == ref.key and g.get("name", "") == ref.group:
                    out.append(_target("group", repo, g))
                continue
            for it in g.get("items") or []:
                if it.get("key") == ref.key:
                    out.append(_target("session", repo, g, it))
    return out


def find(query, tree, kinds=KINDS, limit=10):
    """Substring search over names, keys and paths. Read-only: returns candidates only."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    out = []
    for repo in tree.get("repos") or []:
        hay = " ".join(str(repo.get(k) or "") for k in ("key", "name", "orig_name", "path")).lower()
        if "project" in kinds and q in hay:
            out.append(_target("project", repo))
        for g in repo.get("groups") or []:
            if "group" in kinds and q in str(g.get("name") or "").lower():
                out.append(_target("group", repo, g))
            if "session" not in kinds:
                continue
            for it in g.get("items") or []:
                text = " ".join(str(it.get(k) or "") for k in ("key", "nick", "original")).lower()
                if q in text:
                    out.append(_target("session", repo, g, it))
    return out[:max(1, int(limit))]
