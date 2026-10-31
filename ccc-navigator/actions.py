"""Action registry for WP6 (issue #14 R1)."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

import changes
import refs


class ActionError(Exception):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # "ref" | "refs" | "str" | "bool" | "int" | "enum"
    required: bool = True
    choices: Tuple = ()
    max_len: int = 200
    help: str = ""


@dataclass(frozen=True)
class Action:
    name: str
    summary: str
    params: Tuple[Param, ...]
    targets: Tuple[str, ...]
    mutates: bool
    undoable: bool
    run: Callable[[Any, Dict, str], Dict]
    undo: Callable[[Any, Dict], Dict] | None = None


REGISTRY: Dict[str, Action] = {}


def register(act: Action):
    REGISTRY[act.name] = act
    return act


def _get_actor_from_ctx(ctx):
    if hasattr(ctx, "_actor") and callable(getattr(ctx, "_actor")):
        try:
            return ctx._actor()
        except Exception as e:
            raise
    if hasattr(ctx, "actor"):
        return ctx.actor
    return "you"


def validate(action: Action, body: Dict) -> Dict:
    if not isinstance(body, dict):
        raise ActionError("bad_json", "body must be an object")
    body = dict(body)
    known = {p.name for p in action.params}
    for k in body:
        if k in ("_actor",):
            continue
        if k not in known:
            raise ActionError("unknown_param", "unknown parameter", field=k)
    out = {}
    for p in action.params:
        v = body.get(p.name)
        if p.kind in ("ref", "refs", "str", "enum", "int"):
            if v is None:
                if p.required:
                    raise ActionError("missing_param", "missing required parameter", field=p.name)
                continue
            if isinstance(v, str) and v.strip() == "" and p.required and p.kind in ("ref", "refs", "str", "enum"):
                raise ActionError("missing_param", "missing required parameter", field=p.name)
        if v is None and not p.required:
            continue
        if p.kind == "str":
            if not isinstance(v, str):
                v = str(v)
            v = v.strip()
            if p.required and v == "":
                raise ActionError("missing_param", "missing required parameter", field=p.name)
            if len(v) > p.max_len:
                v = v[:p.max_len]
            out[p.name] = v
            continue
        if p.kind == "bool":
            if isinstance(v, bool):
                out[p.name] = v
            elif isinstance(v, str):
                out[p.name] = v.strip().lower() in ("1", "true", "yes", "on")
            else:
                out[p.name] = bool(v)
            continue
        if p.kind == "int":
            try:
                out[p.name] = int(v)
            except Exception:
                raise ActionError("bad_param", "invalid integer", field=p.name)
            continue
        if p.kind == "enum":
            if v not in p.choices:
                raise ActionError("bad_param", "invalid choice", field=p.name)
            out[p.name] = v
            continue
        if p.kind == "ref":
            if not isinstance(v, str):
                raise ActionError("bad_param", "ref must be a string", field=p.name)
            out[p.name] = v.strip()
            continue
        if p.kind == "refs":
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list):
                raise ActionError("bad_param", "refs must be a list", field=p.name)
            if len(v) > 500:
                raise ActionError("bad_param", "too many refs", field=p.name)
            out[p.name] = [str(x).strip() for x in v if str(x).strip()]
            continue
    return out


def dispatch(ctx, name: str, body: dict) -> dict:
    if name not in REGISTRY:
        raise ActionError("unknown_route", "unknown action")
    act = REGISTRY[name]
    actor = _get_actor_from_ctx(ctx)
    try:
        actor = changes.check_actor(actor or "you")
    except changes.ChangeError as e:
        raise ActionError(e.code, str(e))
    try:
        params = validate(act, body)
    except ActionError:
        raise
    app = getattr(ctx, "app", ctx)
    try:
        if act.run is None:
            raise ActionError("error", "no runner")
        result = act.run(app, params, actor)
    except ActionError:
        raise
    except Exception as e:
        raise ActionError("error", str(e))
    return result


def _state(app):
    if hasattr(app, "state"):
        return app.state
    return app


def _require_key_or_ref(params, tree_getter=None, app=None, kind_filter=None):
    key = params.get("key")
    ref = params.get("ref")
    has_key = key is not None and str(key).strip() != ""
    has_ref = ref is not None and str(ref).strip() != ""
    if has_key and has_ref:
        raise ActionError("bad_param", "key and ref are mutually exclusive")
    if not has_key and not has_ref:
        raise ActionError("missing_param", "missing required parameter", field="key")
    if has_ref and app is not None and tree_getter is not None:
        try:
            tree = tree_getter()
        except Exception:
            tree = app.tree("all") if hasattr(app, "tree") else {}
        t, r = _resolve_targets(app, str(ref), tree, kind_filter)
        k = t.get("key") or ""
        if not k:
            raise ActionError("missing_param", "missing required parameter", field="key")
        return k, t
    if has_key:
        return str(key).strip(), None
    raise ActionError("missing_param", "missing required parameter", field="key")


def _record(app, actor, action, params, before, after, undoable=True):
    ch = getattr(app, "changes", None)
    if ch is None:
        return None
    try:
        return ch.record(actor, action, params, before or {}, after or {}, undoable=undoable)
    except Exception:
        raise


def _resolve_targets(app, ref_str, tree, kind_filter=None):
    try:
        r = refs.parse_ref(ref_str, getattr(app, "desktop_ids_map", None))
    except Exception as e:
        if isinstance(e, refs.RefError):
            code = getattr(e, "code", "")
            if code == "unknown_desktop_id":
                raise ActionError("unknown_ref", str(e))
            if code == "bad_ref":
                raise ActionError("bad_ref", str(e))
            raise ActionError(code or "bad_ref", str(e))
        raise
    if kind_filter and getattr(r, "kind", None) not in kind_filter:   # a project ref given to a session action, etc.
        raise ActionError("wrong_target", "wrong target kind")
    targets = refs.resolve(r, tree)
    if len(targets) == 0:
        raise ActionError("unknown_ref", "no match for ref")
    if len(targets) > 1:
        raise ActionError("ambiguous_ref", "ambiguous ref")
    t = targets[0]
    if kind_filter and t.get("kind") not in kind_filter:
        raise ActionError("wrong_target", "wrong target kind")
    return t, r


# Define all actions
def _session_rename(app, params, actor):
    st = _state(app)
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    name = params.get("name") if params.get("name") is not None else ""
    if not isinstance(name, str):
        name = str(name)
    name = name.strip()
    if name == "":
        raise ActionError("missing_param", "missing required parameter", field="name")
    before = {"nicknames": {str(key): st.snapshot().get("nicknames", {}).get(str(key))}}
    try:
        st.set_nickname(str(key), name)
    except Exception:
        raise
    after = {"nicknames": {str(key): name if name else None}}
    cid = _record(app, actor, "session.rename", {"key": key, "name": name}, before, after, undoable=True)
    return {"ok": True, "action": "session.rename", "change": cid, "result": {"applied": ["navigator"], "summary": "Navigator-only rename (nickname)"}}


register(Action(
    name="session.rename",
    summary="Rename a session",
    params=(Param("key", "str", required=False), Param("name", "str"), Param("ref", "ref", required=False)),
    targets=("session",),
    mutates=True,
    undoable=True,
    run=_session_rename,
))


def _session_star(app, params, actor):
    st = _state(app)
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    pinned = params.get("pinned", True)
    before = {"pinned": {str(key): st.snapshot().get("pinned", {}).get(str(key))}}
    try:
        st.set_pinned(str(key), bool(pinned))
    except Exception:
        raise
    after = {"pinned": {str(key): True if pinned else None}}
    cid = _record(app, actor, "session.star" if pinned else "session.unstar", {"key": key}, before, after, undoable=True)
    return {"ok": True, "action": "session.star" if pinned else "session.unstar", "change": cid, "result": {"ok": True}}


register(Action("session.star", "Star/pin a session", (Param("key", "str", required=False), Param("ref", "ref", required=False), Param("pinned", "bool", required=False)), ("session",), True, True, _session_star))
def _session_unstar(app, params, actor):
    p = dict(params)
    p["pinned"] = False
    return _session_star(app, p, actor)
register(Action("session.unstar", "Unstar/unpin a session", (Param("key", "str", required=False), Param("ref", "ref", required=False)), ("session",), True, True, _session_unstar))


def _session_hide(app, params, actor):
    st = _state(app)
    keys = []
    if params.get("keys"):
        keys = list(params["keys"])
    keyv = params.get("key")
    refv = params.get("ref")
    if keyv is not None and str(keyv).strip() != "" and refv is not None and str(refv).strip() != "":
        raise ActionError("bad_param", "key and ref are mutually exclusive")
    if keyv is not None and str(keyv).strip() != "":
        keys.append(keyv)
    if refv is not None and str(refv).strip() != "":
        try:
            tree = app.tree("all", show_hidden=True)
        except Exception:
            tree = app.tree("all") if hasattr(app, "tree") else {}
        try:
            t, r = _resolve_targets(app, str(refv), tree, None)
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
        if t.get("kind") == "session":
            keys = [t.get("key")]
        elif t.get("kind") == "group":
            keys = ["g:%s|%s" % (t["project"], t.get("group", ""))]
        elif t.get("kind") == "project":
            keys = ["r:" + t["project"]]
        else:
            raise ActionError("wrong_target", "wrong target kind")
    if not keys:
        raise ActionError("missing_param", "missing required parameter", field="key")
    keys = [str(k) for k in keys if k]
    if not keys:
        raise ActionError("missing_param", "missing required parameter", field="key")
    before = {"hidden": {k: st.snapshot().get("hidden", {}).get(k) for k in keys}}
    try:
        entry_id = st.set_hidden(keys, True, str(params.get("label") or ""))
    except Exception:
        raise
    after = {"hidden": {k: True for k in keys}}
    cid = _record(app, actor, "session.hide", {"keys": keys}, before, after, undoable=True)
    return {"ok": True, "action": "session.hide", "change": cid, "result": {"ok": True, "id": entry_id}}


def _session_unhide(app, params, actor):
    st = _state(app)
    keys = []
    if params.get("keys"):
        keys = list(params["keys"])
    keyv = params.get("key")
    refv = params.get("ref")
    if keyv is not None and str(keyv).strip() != "" and refv is not None and str(refv).strip() != "":
        raise ActionError("bad_param", "key and ref are mutually exclusive")
    if keyv is not None and str(keyv).strip() != "":
        keys.append(keyv)
    if refv is not None and str(refv).strip() != "":
        try:
            tree = app.tree("all", show_hidden=True)
        except Exception:
            tree = app.tree("all") if hasattr(app, "tree") else {}
        try:
            t, r = _resolve_targets(app, str(refv), tree, None)
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
        if t.get("kind") == "session":
            keys = [t.get("key")]
        elif t.get("kind") == "group":
            keys = ["g:%s|%s" % (t["project"], t.get("group", ""))]
        elif t.get("kind") == "project":
            keys = ["r:" + t["project"]]
        else:
            raise ActionError("wrong_target", "wrong target kind")
    if not keys:
        raise ActionError("missing_param", "missing required parameter", field="key")
    keys = [str(k) for k in keys if k]
    if not keys:
        raise ActionError("missing_param", "missing required parameter", field="key")
    before = {"hidden": {k: st.snapshot().get("hidden", {}).get(k) for k in keys}}
    try:
        st.set_hidden(keys, False)
    except Exception:
        raise
    after = {"hidden": {k: None for k in keys}}
    cid = _record(app, actor, "session.unhide", {"keys": keys}, before, after, undoable=True)
    return {"ok": True, "action": "session.unhide", "change": cid, "result": {"ok": True}}


register(Action("session.hide", "Hide a session", (Param("key", "str", required=False), Param("keys", "refs", required=False), Param("ref", "ref", required=False), Param("label", "str", required=False)), ("session", "group", "project"), True, True, _session_hide))
register(Action("session.unhide", "Unhide a session", (Param("key", "str", required=False), Param("keys", "refs", required=False), Param("ref", "ref", required=False)), ("session", "group", "project"), True, True, _session_unhide))


def _session_move_group(app, params, actor):
    st = _state(app)
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    repo = str(params.get("repo") or "")
    group = params.get("group") or ""
    before = {"assign": {str(key): st.snapshot().get("assign", {}).get(str(key))}}
    try:
        st.assign(str(key), repo, group)
    except Exception:
        raise
    after = {"assign": {str(key): {"repo": repo, "group": group} if group else None}}
    cid = _record(app, actor, "session.move-group", {"key": key, "repo": repo, "group": group}, before, after, undoable=True)
    return {"ok": True, "action": "session.move-group", "change": cid, "result": {"ok": True}}


def _session_move_project(app, params, actor):
    st = _state(app)
    keys = []
    if params.get("keys"):
        keys = list(params["keys"])
    keyv = params.get("key")
    refv = params.get("ref")
    if keyv is not None and str(keyv).strip() != "" and refv is not None and str(refv).strip() != "":
        raise ActionError("bad_param", "key and ref are mutually exclusive")
    if keyv is not None and str(keyv).strip() != "":
        keys.append(keyv)
    if refv is not None and str(refv).strip() != "":
        try:
            tree = app.tree("all")
        except Exception:
            tree = {}
        try:
            t, r = _resolve_targets(app, str(refv), tree, ("session",))
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
        if t.get("kind") == "session":
            keys = [t.get("key")]
        else:
            raise ActionError("wrong_target", "wrong target kind")
    if not keys:
        raise ActionError("missing_param", "missing required parameter", field="key")
    keys = [str(k) for k in keys if k and not str(k).startswith("batch:")]
    if not keys:
        raise ActionError("missing_param", "missing required parameter", field="key")
    repo = str(params.get("repo") or "")
    before = {"moved": {k: st.snapshot().get("moved", {}).get(k) for k in keys}}
    try:
        count = st.move_to_project(keys, repo)
    except Exception:
        raise
    after = {"moved": {k: repo if repo else None for k in keys}}
    cid = _record(app, actor, "session.move-project", {"keys": keys, "repo": repo}, before, after, undoable=True)
    return {"ok": True, "action": "session.move-project", "change": cid, "result": {"ok": True, "count": count, "repo": repo}}


register(Action("session.move-group", "Move session to group", (Param("key", "str", required=False), Param("ref", "ref", required=False), Param("repo", "str"), Param("group", "str", required=False)), ("session",), True, True, _session_move_group))
register(Action("session.move-project", "Move session to project", (Param("key", "str", required=False), Param("keys", "refs", required=False), Param("ref", "ref", required=False), Param("repo", "str", required=False)), ("session",), True, True, _session_move_project))


def _session_note(app, params, actor):
    st = _state(app)
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    text = params.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if text == "":
        raise ActionError("missing_param", "missing required parameter", field="text")
    before = {"notes": {str(key): list((st.snapshot().get("notes") or {}).get(str(key)) or [])}}
    try:
        note = st.add_note(str(key), text, by=actor)
    except Exception:
        raise
    if note is None:
        raise ActionError("bad_param", "key and text required")
    after = {"notes": {str(key): list((st.snapshot().get("notes") or {}).get(str(key)) or [])}}
    cid = _record(app, actor, "session.note", {"key": key}, before, after, undoable=True)
    return {"ok": True, "action": "session.note", "change": cid, "result": {"ok": True, "note": note}}


def _session_note_remove(app, params, actor):
    st = _state(app)
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    idx = params.get("remove")
    before = {"notes": {str(key): list((st.snapshot().get("notes") or {}).get(str(key)) or [])}}
    try:
        ok = st.remove_note(str(key), int(idx))
    except Exception:
        ok = False
    if not ok:
        raise ActionError("unknown_ref", "note not found")
    after = {"notes": {str(key): list((st.snapshot().get("notes") or {}).get(str(key)) or [])}}
    cid = _record(app, actor, "session.note-remove", {"key": key, "remove": idx}, before, after, undoable=True)
    return {"ok": True, "action": "session.note-remove", "change": cid, "result": {"ok": ok}}


def _session_mark(app, params, actor):
    st = _state(app)
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    before = {"triage": {str(key): copy.deepcopy(st.snapshot().get("triage", {}).get(str(key)))}}
    entries = {str(key): {"label": params.get("label") or "", "reason": params.get("reason") or "", "by": actor}}
    try:
        changed = st.set_triage(entries)
    except Exception:
        raise
    after = {"triage": {str(key): st.snapshot().get("triage", {}).get(str(key))}}
    cid = _record(app, actor, "session.mark", {"key": key, "label": params.get("label")}, before, after, undoable=True)
    return {"ok": True, "action": "session.mark", "change": cid, "result": {"ok": True, "changed": changed}}


def _session_send(app, params, actor):
    key, _ = _require_key_or_ref(params, lambda: app.tree("all"), app, ("session",))
    text = params.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if text == "":
        raise ActionError("missing_param", "missing required parameter", field="text")
    before = {}
    after = {}
    try:
        if hasattr(app, "deliver"):
            status = app.deliver(key, text)
        else:
            status = "sent"
    except Exception as e:
        status = str(e)
    if status != "sent":
        raise ActionError("send_failed", status)
    cid = _record(app, actor, "session.send", {"key": key, "text": text}, before, after, undoable=False)
    return {"ok": True, "action": "session.send", "change": cid, "result": {"status": status}}


register(Action("session.note", "Add note to session", (Param("key", "str", required=False), Param("ref", "ref", required=False), Param("text", "str")), ("session",), True, True, _session_note))
register(Action("session.note-remove", "Remove note from session", (Param("key", "str", required=False), Param("ref", "ref", required=False), Param("remove", "int")), ("session",), True, True, _session_note_remove))
register(Action("session.mark", "Mark/triage a session", (Param("key", "str", required=False), Param("ref", "ref", required=False), Param("label", "str", required=False), Param("reason", "str", required=False)), ("session",), True, True, _session_mark))
register(Action("session.send", "Send text to session", (Param("key", "str", required=False), Param("ref", "ref", required=False), Param("text", "str"), Param("sid", "str", required=False), Param("engine", "str", required=False)), ("session",), True, False, _session_send))


# group/project/decision/undo
def _group_rename(app, params, actor):
    st = _state(app)
    repo = str(params.get("repo") or "")
    old = params.get("name") or params.get("group") or params.get("old") or ""
    new = params.get("new_name") or params.get("new") or ""
    if params.get("ref"):
        if params.get("repo") is not None and str(params.get("repo")).strip() != "" or params.get("name") is not None and str(params.get("name")).strip() != "" or params.get("group") is not None and str(params.get("group")).strip() != "":
            pass  # allow but be strict? follow key-or-ref rule broadly
        try:
            tree = app.tree("all")
            t, r = _resolve_targets(app, params["ref"], tree, ("group",))
            if t.get("kind") == "group":
                repo = t.get("project") or repo
                old = t.get("group") if t.get("group") is not None else old
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or (old is None or str(old).strip() == ""):
        raise ActionError("missing_param", "missing required parameter", field="repo")
    if not isinstance(new, str) or new.strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="new_name")
    old_s = str(old)
    new_s = str(new).strip()
    before = {"groups": {repo: copy.deepcopy((st.snapshot().get("groups") or {}).get(repo))}, "group_order": {repo: copy.deepcopy((st.snapshot().get("group_order") or {}).get(repo))}}
    try:
        st.rename_group(repo, old_s, new_s)
    except Exception:
        raise
    after = {"groups": {repo: copy.deepcopy((st.snapshot().get("groups") or {}).get(repo))}, "group_order": {repo: copy.deepcopy((st.snapshot().get("group_order") or {}).get(repo))}}
    cid = _record(app, actor, "group.rename", {"repo": repo, "name": old_s, "new_name": new_s}, before, after, undoable=True)
    return {"ok": True, "action": "group.rename", "change": cid, "result": {"ok": True}}


def _group_hide(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or ""
    name = params.get("name") or params.get("group") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all", show_hidden=True)
            t, r = _resolve_targets(app, params["ref"], tree, ("group",))
            if t.get("kind") == "group":
                repo = t.get("project") or repo
                name = t.get("group") if t.get("group") is not None else name
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or (name is None or str(name).strip() == ""):
        raise ActionError("missing_param", "missing required parameter", field="repo")
    key = "g:%s|%s" % (repo, name)
    before = {"hidden": {key: st.snapshot().get("hidden", {}).get(key)}}
    try:
        st.set_hidden([key], True)
    except Exception:
        raise
    after = {"hidden": {key: True}}
    cid = _record(app, actor, "group.hide", {"repo": repo, "name": name}, before, after, undoable=True)
    return {"ok": True, "action": "group.hide", "change": cid, "result": {"ok": True}}


def _group_unhide(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or ""
    name = params.get("name") or params.get("group") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all", show_hidden=True)
            t, r = _resolve_targets(app, params["ref"], tree, ("group",))
            if t.get("kind") == "group":
                repo = t.get("project") or repo
                name = t.get("group") if t.get("group") is not None else name
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or (name is None or str(name).strip() == ""):
        raise ActionError("missing_param", "missing required parameter", field="repo")
    key = "g:%s|%s" % (repo, name)
    before = {"hidden": {key: st.snapshot().get("hidden", {}).get(key)}}
    try:
        st.set_hidden([key], False)
    except Exception:
        raise
    after = {"hidden": {key: None}}
    cid = _record(app, actor, "group.unhide", {"repo": repo, "name": name}, before, after, undoable=True)
    return {"ok": True, "action": "group.unhide", "change": cid, "result": {"ok": True}}


def _group_unhide_all(app, params, actor):
    st = _state(app)
    repo = str(params.get("repo") or "")
    group = params.get("group")
    if params.get("ref"):
        try:
            tree = app.tree("all", show_hidden=True)
            t, r = _resolve_targets(app, params["ref"], tree, ("group", "project"))
            if t.get("kind") == "project":
                repo = t.get("project") or repo
                group = None
            elif t.get("kind") == "group":
                repo = t.get("project") or repo
                group = t.get("group")
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo:
        raise ActionError("missing_param", "missing required parameter", field="repo")
    try:
        t = app.tree("all", show_hidden=True)
    except Exception:
        t = {"repos": []}
    r = next((x for x in t["repos"] if x["key"] == repo), None)
    if not r:
        raise ActionError("unknown_ref", "unknown project")
    groups = [g for g in r["groups"] if group is None or g["name"] == group]
    keys = [s["key"] for g in groups for s in g["items"] if s.get("hidden")]
    keys += ["r:" + repo] if group is None else []
    keys += ["g:%s|%s" % (repo, g["name"]) for g in groups]
    hidden = st.snapshot().get("hidden") or {}
    keys = [k for k in keys if k in hidden]
    before = {"hidden": {k: hidden.get(k) for k in keys}}
    try:
        st.set_hidden(keys, False)
    except Exception:
        raise
    after = {"hidden": {k: None for k in keys}}
    cid = _record(app, actor, "group.unhide-all", {"repo": repo, "group": group}, before, after, undoable=True)
    return {"ok": True, "action": "group.unhide-all", "change": cid, "result": {"ok": True, "count": len(keys)}}


def _group_icon(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or ""
    name = params.get("name") or params.get("group") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all")
            t, r = _resolve_targets(app, params["ref"], tree, ("group",))
            if t.get("kind") == "group":
                repo = t.get("project") or repo
                name = t.get("group") if t.get("group") is not None else name
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or (name is None or str(name).strip() == ""):
        raise ActionError("missing_param", "missing required parameter", field="repo")
    if params.get("icon") is None or (isinstance(params.get("icon"), str) and params.get("icon").strip() == ""):
        raise ActionError("missing_param", "missing required parameter", field="icon")
    key = "g:%s|%s" % (repo, name)
    before = {"icons": {key: (st.snapshot().get("icons") or {}).get(key)}}
    try:
        st.set_icon(key, params.get("icon"))
    except Exception:
        raise
    after = {"icons": {key: (st.snapshot().get("icons") or {}).get(key)}}
    cid = _record(app, actor, "group.icon", {"key": key, "icon": params.get("icon")}, before, after, undoable=True)
    return {"ok": True, "action": "group.icon", "change": cid, "result": {"ok": True}}


register(Action("group.rename", "Rename group", (Param("repo", "str", required=False), Param("name", "str", required=False), Param("new_name", "str", required=False), Param("group", "str", required=False), Param("old", "str", required=False), Param("ref", "ref", required=False)), ("group",), True, True, _group_rename))
register(Action("group.hide", "Hide group", (Param("repo", "str", required=False), Param("name", "str", required=False), Param("group", "str", required=False), Param("ref", "ref", required=False)), ("group",), True, True, _group_hide))
register(Action("group.unhide", "Unhide group", (Param("repo", "str", required=False), Param("name", "str", required=False), Param("group", "str", required=False), Param("ref", "ref", required=False)), ("group",), True, True, _group_unhide))
register(Action("group.unhide-all", "Unhide all in scope", (Param("repo", "str", required=False), Param("group", "str", required=False), Param("ref", "ref", required=False)), ("group", "project"), True, True, _group_unhide_all))
register(Action("group.icon", "Set group icon", (Param("repo", "str", required=False), Param("name", "str", required=False), Param("group", "str", required=False), Param("ref", "ref", required=False), Param("icon", "str")), ("group",), True, True, _group_icon))


def _project_rename(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or params.get("key") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all")
            t, r = _resolve_targets(app, params["ref"], tree, ("project",))
            if t.get("kind") == "project":
                repo = t.get("project") or repo
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or str(repo).strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="repo")
    name = params.get("name") if params.get("name") is not None else ""
    if not isinstance(name, str) or name.strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="name")
    name = name.strip()
    before = {"repo_names": {str(repo): (st.snapshot().get("repo_names") or {}).get(str(repo))}}
    try:
        st.set_repo_name(str(repo), name)
    except Exception:
        raise
    after = {"repo_names": {str(repo): name if name else None}}
    cid = _record(app, actor, "project.rename", {"repo": repo, "name": name}, before, after, undoable=True)
    return {"ok": True, "action": "project.rename", "change": cid, "result": {"ok": True}}


def _project_hide(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or params.get("key") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all", show_hidden=True)
            t, r = _resolve_targets(app, params["ref"], tree, ("project",))
            if t.get("kind") == "project":
                repo = t.get("project") or repo
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or str(repo).strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="repo")
    key = "r:" + str(repo)
    before = {"hidden": {key: st.snapshot().get("hidden", {}).get(key)}}
    try:
        st.set_hidden([key], True)
    except Exception:
        raise
    after = {"hidden": {key: True}}
    cid = _record(app, actor, "project.hide", {"repo": repo}, before, after, undoable=True)
    return {"ok": True, "action": "project.hide", "change": cid, "result": {"ok": True}}


def _project_unhide(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or params.get("key") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all", show_hidden=True)
            t, r = _resolve_targets(app, params["ref"], tree, ("project",))
            if t.get("kind") == "project":
                repo = t.get("project") or repo
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or str(repo).strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="repo")
    key = "r:" + str(repo)
    before = {"hidden": {key: st.snapshot().get("hidden", {}).get(key)}}
    try:
        st.set_hidden([key], False)
    except Exception:
        raise
    after = {"hidden": {key: None}}
    cid = _record(app, actor, "project.unhide", {"repo": repo}, before, after, undoable=True)
    return {"ok": True, "action": "project.unhide", "change": cid, "result": {"ok": True}}


def _project_unhide_all(app, params, actor):
    repo = params.get("repo") or params.get("key") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all", show_hidden=True)
            t, r = _resolve_targets(app, params["ref"], tree, ("project",))
            if t.get("kind") == "project":
                repo = t.get("project") or repo
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or str(repo).strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="repo")
    return _group_unhide_all(app, {"repo": repo}, actor)


def _project_icon(app, params, actor):
    st = _state(app)
    repo = params.get("repo") or params.get("key") or ""
    if params.get("ref"):
        try:
            tree = app.tree("all")
            t, r = _resolve_targets(app, params["ref"], tree, ("project",))
            if t.get("kind") == "project":
                repo = t.get("project") or repo
        except ActionError:
            raise
        except Exception as e:
            raise ActionError("bad_ref", str(e))
    if not repo or str(repo).strip() == "":
        raise ActionError("missing_param", "missing required parameter", field="repo")
    if params.get("icon") is None or (isinstance(params.get("icon"), str) and params.get("icon").strip() == ""):
        raise ActionError("missing_param", "missing required parameter", field="icon")
    key = "r:" + str(repo)
    before = {"icons": {key: (st.snapshot().get("icons") or {}).get(key)}}
    try:
        st.set_icon(key, params.get("icon"))
    except Exception:
        raise
    after = {"icons": {key: (st.snapshot().get("icons") or {}).get(key)}}
    cid = _record(app, actor, "project.icon", {"key": key, "icon": params.get("icon")}, before, after, undoable=True)
    return {"ok": True, "action": "project.icon", "change": cid, "result": {"ok": True}}


register(Action("project.rename", "Rename project", (Param("repo", "str", required=False), Param("key", "str", required=False), Param("name", "str"), Param("ref", "ref", required=False)), ("project",), True, True, _project_rename))
register(Action("project.hide", "Hide project", (Param("repo", "str", required=False), Param("key", "str", required=False), Param("ref", "ref", required=False)), ("project",), True, True, _project_hide))
register(Action("project.unhide", "Unhide project", (Param("repo", "str", required=False), Param("key", "str", required=False), Param("ref", "ref", required=False)), ("project",), True, True, _project_unhide))
register(Action("project.unhide-all", "Unhide all in project", (Param("repo", "str", required=False), Param("key", "str", required=False), Param("ref", "ref", required=False)), ("project",), True, True, _project_unhide_all))
register(Action("project.icon", "Set project icon", (Param("repo", "str", required=False), Param("key", "str", required=False), Param("ref", "ref", required=False), Param("icon", "str")), ("project",), True, True, _project_icon))


def _decision_answer(app, params, actor):
    try:
        res = app.answer_decision({"id": params.get("id"), "choice": params.get("choice"), "note": params.get("note")}, actor)
        return {"ok": True, "action": "decision.answer", "change": res.get("change"), "result": res}
    except Exception as e:
        raise


def _decision_reopen(app, params, actor):
    inbox = getattr(app, "inbox")
    did = str(params.get("id") or "")
    try:
        before = inbox.reopen(did)
        cid = app.changes.record(actor, "decision.reopen", {"id": did}, {"answers": {did: before}}, {"answers": {did: None}})
        return {"ok": True, "action": "decision.reopen", "change": cid, "result": {"ok": True, "id": did}}
    except Exception as e:
        raise


def _decision_rank(app, params, actor):
    inbox = getattr(app, "inbox")
    ids = list(params.get("ids") or [])
    why = params.get("why") or {}
    if isinstance(why, str):
        why = {}
    try:
        rec, before = inbox.set_ranks(ids, why=why, by=actor)
        cid = app.changes.record(actor, "decision.rank", {"ids": rec.get("ids")}, {"ranks": before}, {"ranks": rec}, undoable=False)
        return {"ok": True, "action": "decision.rank", "change": cid, "result": {"ok": True, "ranks": rec}}
    except Exception as e:
        raise


def _decision_rank_clear(app, params, actor):
    inbox = getattr(app, "inbox")
    try:
        rec, before = inbox.clear_ranks(by=actor)
        cid = app.changes.record(actor, "decision.rank_clear", {}, {"ranks": before}, {"ranks": rec}, undoable=False)
        return {"ok": True, "action": "decision.rank-clear", "change": cid, "result": {"ok": True, "ranks": rec}}
    except Exception as e:
        raise


def _decision_ask(app, params, actor):
    inbox = getattr(app, "inbox")
    try:
        d = inbox.ask(params.get("question"), params.get("options"), params.get("lane"), params.get("priority", 2),
                      params.get("deadline"), params.get("session"), params.get("source"), params.get("id") or "", by=actor)
        app.changes.record(actor, "decision.ask", {"id": d["id"]}, {}, {"questions": {d["id"]: d["question"]}}, undoable=False)
        return {"ok": True, "action": "decision.ask", "change": None, "result": {"ok": True, "decision": d}}
    except Exception as e:
        raise


register(Action("decision.answer", "Answer decision", (Param("id", "str"), Param("choice", "str"), Param("note", "str", required=False)), (), True, True, _decision_answer))
register(Action("decision.reopen", "Reopen decision", (Param("id", "str"),), (), True, True, _decision_reopen))
register(Action("decision.rank", "Rank decisions", (Param("ids", "refs"), Param("why", "str", required=False)), (), True, False, _decision_rank))
register(Action("decision.rank-clear", "Clear decision ranks", (), (), True, False, _decision_rank_clear))
register(Action("decision.ask", "Ask decision", (Param("question", "str"), Param("options", "refs"), Param("lane", "str", required=False), Param("priority", "int", required=False), Param("deadline", "str", required=False), Param("session", "str", required=False), Param("source", "str", required=False), Param("id", "str", required=False)), (), True, False, _decision_ask))


def _undo_action(app, params, actor):
    ch = getattr(app, "changes")
    cid = params.get("change") or ""

    def read(s, k):
        st = _state(app).snapshot()
        return st.get(s, {}).get(k)

    def write(fields):
        st = _state(app)
        for s, kv in fields.items():
            for k, v in kv.items():
                if s == "nicknames":
                    st.set_nickname(k, v if v is not None else "")
                elif s == "pinned":
                    if v is None:
                        st.set_pinned(k, False)
                    else:
                        st.set_pinned(k, bool(v))
                elif s == "hidden":
                    if v is None:
                        try:
                            st.set_hidden([k], False)
                        except Exception:
                            raise
                    else:
                        try:
                            st.set_hidden([k], bool(v))
                        except Exception:
                            raise
                elif s == "icons":
                    st.set_icon(k, v)
                elif s == "repo_names":
                    st.set_repo_name(k, v if v is not None else "")
                elif s == "notes":
                    try:
                        snap = st.snapshot()
                        if v is None:
                            if k in (snap.get("notes") or {}):
                                st.data["notes"].pop(k, None)
                                st._save()
                        else:
                            st.data["notes"][k] = list(v) if isinstance(v, list) else []
                            st._save()
                    except Exception:
                        raise
                elif s == "triage":
                    if v is None:
                        try:
                            st.set_triage({k: {"label": "", "by": actor}})
                        except Exception:
                            raise
                    else:
                        try:
                            st.set_triage({k: dict(v, by=v.get("by") or actor)})
                        except Exception:
                            raise
                elif s == "assign":
                    if v is None:
                        try:
                            cur = st.snapshot().get("assign", {}).get(k, {})
                            st.assign(k, cur.get("repo") or "", "")
                        except Exception:
                            raise
                    else:
                        try:
                            st.assign(k, v.get("repo") or "", v.get("group") or "")
                        except Exception:
                            raise
                elif s == "moved":
                    try:
                        st.move_to_project([k], v if v else "")
                    except Exception:
                        raise
                elif s == "groups":
                    try:
                        repo = k
                        if v is None:
                            st.data["groups"][repo] = []
                            st._save()
                        else:
                            st.data["groups"][repo] = list(v) if isinstance(v, list) else []
                            st._save()
                    except Exception:
                        raise
                elif s == "group_order":
                    try:
                        repo = k
                        if v is None or (isinstance(v, list) and len(v) == 0):
                            st.data.get("group_order", {}).pop(repo, None)
                            st._save()
                        else:
                            st.set_group_order(repo, v)
                    except Exception:
                        raise
                elif s == "copilot":
                    if isinstance(v, dict):
                        try:
                            st.set_copilot(v.get("key") or "")
                        except Exception:
                            raise
                elif s == "desktop_map":
                    if isinstance(v, dict):
                        for g, repo in v.items():
                            try:
                                st.map_desktop_group(g, repo or "")
                            except Exception:
                                raise
                else:
                    pass

    try:
        uid = ch.undo(cid, actor, read, write)
        return {"ok": True, "action": "undo", "change": uid, "result": {"ok": True, "undone": cid}}
    except changes.ChangeError as e:
        if e.code == "conflict":
            raise ActionError("conflict", str(e))
        raise ActionError(e.code, str(e))
    except Exception as e:
        raise


register(Action("undo", "Undo a change", (Param("change", "str"),), (), True, False, _undo_action))
