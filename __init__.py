"""Exact-route, fail-open Slack Assistant status observer.

The observer is presentation-only.  It stores and emits generic lifecycle facts
only; it never retains delegation goals, tool arguments, paths, child summaries,
error text, or Slack credentials.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

PLUGIN_ID = "slack-delegation-status"
PLUGIN_VERSION = "0.7.1"
_LOG = logging.getLogger(__name__)
_TERMINAL = {"complete", "error", "stopped", "unknown"}
_WEB = {"web_search", "web_extract", "browser_navigate", "browser_click", "browser_snapshot", "browser"}
_FILE = {"read_file", "search_files", "file_read", "file_search", "patch"}
_STATUS_DEFAULTS = {
    "processing": "요청을 분석 중…", "routing": "위임 경로를 결정 중…", "delegating": "작업을 준비 중…",
    "worker": "비동기 위임 작업 중…", "multiple": "{count}개 작업을 처리 중…",
    "partial": "{done}/{total}개 완료 · {active}개 처리 중…", "verifier": "결과를 검증 중…",
    "synthesis": "결과를 정리 중…", "writing": "답변을 작성 중…", "stalled": "작업 응답 지연을 처리 중…",
    "web": "자료를 확인 중…", "file": "파일을 확인 중…", "fallback": "Delegation status — Working",
}
_PHASES = frozenset(_STATUS_DEFAULTS)


def _s(value: Any, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


class DelegationStatus:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._lock = threading.RLock()
        # Candidates exist before delegate_task returns.  They are intentionally
        # publishable, so a saturated pool's synchronous fallback is visible.
        self._candidates: dict[tuple[str, str], dict[str, Any]] = {}
        self._active: dict[str, dict[str, Any]] = {}
        self._transient: dict[tuple[str, str], dict[str, Any]] = {}
        self._child_to_lane: dict[str, tuple[str, str, int]] = {}
        self._refresh_armed: dict[tuple[str, str, str], Any] = {}
        # Completion ownership comes from delegate-task-routing's authenticated
        # per-turn contract, never from user-controlled marker text.
        self._completion_turns: dict[tuple[str, str], set[str]] = {}
        self._current_turn_by_session: dict[str, str] = {}
        # Per-output removal plan. A synchronous result must never remove a
        # detached delegation that happens to share its parent session.
        self._finalizing_turns: dict[tuple[str, str], dict[str, Any]] = {}
        self._route_fallback_sent: set[tuple[str, str, str]] = set()
        self._bridge_owner = object()
        self._bridged_adapter: Any = None
        self._generation = 0
        self._closed = False
        self._sequence = 0
        self._load_recovered_rows()

    def _setting(self, key: str, default: Any) -> Any:
        try:
            return self.ctx.get_config(key, default)
        except Exception:
            return default

    @staticmethod
    def _key(session_id: Any, turn_id: Any) -> tuple[str, str]:
        return _s(session_id), _s(turn_id)

    @staticmethod
    def _route(origin: Mapping[str, Any]) -> tuple[str, str, str]:
        return _s(origin.get("scope_id")), _s(origin.get("chat_id")), _s(origin.get("thread_id"))

    def _scope(self) -> dict[str, str]:
        raw = self._setting("scope", {})
        raw = raw if isinstance(raw, Mapping) else {}
        return {
            "scope_id": _s(raw.get("team_id") or raw.get("scope_id")),
            "chat_id": _s(raw.get("chat_id")), "thread_id": _s(raw.get("thread_id")),
        }

    def _in_scope(self, origin: Mapping[str, Any]) -> bool:
        scope = self._scope()
        thread = _s(origin.get("thread_id"))
        thread_matches = (scope["thread_id"] == "*" and bool(thread)) or thread == scope["thread_id"]
        return bool(scope["chat_id"] and scope["thread_id"] and _s(origin.get("chat_id")) == scope["chat_id"]
                    and thread_matches and (not scope["scope_id"] or _s(origin.get("scope_id")) == scope["scope_id"]))

    def _origin(self) -> dict[str, str] | None:
        try:
            from gateway.session_context import get_session_env
            if _s(get_session_env("HERMES_SESSION_PLATFORM", "")).lower() != "slack":
                return None
            origin = {
                "scope_id": _s(get_session_env("HERMES_SESSION_SCOPE_ID", "")),
                "chat_id": _s(get_session_env("HERMES_SESSION_CHAT_ID", "")),
                "thread_id": _s(get_session_env("HERMES_SESSION_THREAD_ID", ""))
                or _s(get_session_env("HERMES_SESSION_MESSAGE_ID", "")),
            }
            return origin if self._in_scope(origin) else None
        except Exception:
            return None

    def _seconds(self, key: str, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(float(self._setting(key, default)), high))
        except (TypeError, ValueError):
            return default

    def _refresh(self) -> float: return self._seconds("refresh_seconds", 90, 30, 110)
    def _max_age(self) -> float: return self._seconds("max_age_seconds", 1800, 60, 1800)
    def _throttle(self) -> float: return self._seconds("change_throttle_seconds", 15, 3, 60)
    def _sample(self) -> float: return self._seconds("sample_seconds", 30, 15, 60)

    def _status(self, name: str, **values: Any) -> str:
        """Structured mapping with v0.6 legacy keys retained for deployed configs."""
        configured = self._setting("status_texts", {})
        template = configured.get(name) if isinstance(configured, Mapping) else None
        legacy = {"worker": "status_text", "multiple": "multiple_status_text", "verifier": "verifier_status_text"}
        if not isinstance(template, str) or not template.strip():
            template = self._setting(legacy[name], "") if name in legacy else ""
        template = template if isinstance(template, str) and template.strip() else _STATUS_DEFAULTS[name]
        try:
            return template.format(**values)[:160]
        except (KeyError, ValueError, IndexError):
            return _STATUS_DEFAULTS[name].format(**values)[:160]

    def _phase(self, session_id: Any, turn_id: Any) -> str:
        try:
            from hermes_plugins import delegate_task_routing
            phase = delegate_task_routing.delegation_phase_for_turn(session_id, turn_id)
            return phase if phase in {"verifier", "synthesis", "writing"} else "worker"
        except Exception:
            return "worker"

    @staticmethod
    def _routing_lifecycle(session_id: Any, turn_id: Any) -> dict[str, Any]:
        """Read the routing plugin's authenticated, non-sensitive turn view."""
        try:
            from hermes_plugins import delegate_task_routing
            value = delegate_task_routing.delegation_lifecycle_for_turn(session_id, turn_id)
            return dict(value) if isinstance(value, Mapping) else {"mode": "unrouted", "delegation_ids": []}
        except Exception as exc:
            _LOG.debug("routing lifecycle unavailable error_class=%s", type(exc).__name__)
            return {"mode": "unrouted", "delegation_ids": []}

    _delegation_phase = _phase

    def _observe(self, event: str, records: list[dict[str, Any]] | None = None, **facts: Any) -> None:
        """Non-secret structured lifecycle telemetry; no identifiers or model content."""
        rows = records or []
        fields: dict[str, Any] = {
            "event": event, "records": len(rows), "lanes": sum(len(x.get("lanes", ())) for x in rows),
        }
        fields.update({key: value for key, value in facts.items() if isinstance(value, (bool, int, float, str))})
        _LOG.info("delegation_status %s", json.dumps(fields, sort_keys=True, separators=(",", ":")))

    def _new_record(self, session_id: str, turn_id: str, origin: Mapping[str, Any], *, kind: str, phase: str,
                    lanes: int = 0) -> dict[str, Any]:
        now = time.time()
        return {
            "parent_session_id": session_id, "parent_turn_id": turn_id, "origin": dict(origin), "kind": kind,
            "mode": "", "lanes": [{"id": f"lane-{i + 1}", "status": "pending", "phase": phase,
                                        "started_at": 0.0, "ended_at": 0.0} for i in range(lanes)],
            "started": 0, "created_at": now, "last_progress": now, "samples": [], "last_text": "",
            "last_text_at": 0.0, "presentation_phase": phase,
        }

    def _records(self, route: tuple[str, str, str]) -> list[dict[str, Any]]:
        return [row for row in (*self._transient.values(), *self._candidates.values(), *self._active.values())
                if self._route(row["origin"]) == route]

    def _find(self, session_id: Any, turn_id: Any) -> dict[str, Any] | None:
        key = self._key(session_id, turn_id)
        return next((row for row in self._active.values()
                     if self._key(row.get("parent_session_id"), row.get("parent_turn_id")) == key),
                    self._candidates.get(key))

    def _persist(self) -> None:
        try:
            self.ctx.state.set("active", [{
                "delegation_id": did, "parent_session_id": row.get("parent_session_id", ""),
                "parent_turn_id": row.get("parent_turn_id", ""), "origin": row["origin"],
                "created_at": row["created_at"], "mode": row.get("mode", ""),
                "lanes": [{"id": lane["id"], "status": lane["status"], "phase": lane.get("phase", "worker")}
                          for lane in row["lanes"]],
            } for did, row in self._active.items()])
        except Exception as exc:
            _LOG.debug("status persistence failed error_class=%s", type(exc).__name__)

    def _load_recovered_rows(self) -> None:
        try:
            rows = self.ctx.state.get("active", [])
        except Exception:
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping) and isinstance(row.get("origin"), Mapping) and self._in_scope(row["origin"]):
                self._queue_route(self._route(row["origin"]), delay=0.0, force_clear=True)
        if rows:
            try:
                self.ctx.state.set("active", [])
            except Exception:
                pass

    def on_pre_gateway_dispatch(self, event: Any = None, **_: Any) -> None:
        if bool(getattr(event, "internal", False)):
            return
        source = getattr(event, "source", None)
        raw_platform = getattr(source, "platform", "")
        platform = _s(getattr(raw_platform, "value", raw_platform)).lower()
        origin = {"scope_id": _s(getattr(source, "scope_id", "")), "chat_id": _s(getattr(source, "chat_id", "")),
                  "thread_id": _s(getattr(source, "thread_id", ""))}
        if platform != "slack" or not self._in_scope(origin):
            return
        # This hook runs before gateway authorization. It must never mutate
        # lifecycle ownership; admitted turns create/clear only their own rows
        # in pre_llm_call/on_session_end.
        self._observe("inbound_observed", route_scoped=True)

    def on_pre_llm_call(self, session_id: Any = None, turn_id: Any = None, user_message: Any = "",
                        parent_session_id: Any = None, **_: Any) -> None:
        # Child LLM calls must never claim the parent Slack thread.
        if _s(parent_session_id):
            return
        origin = self._origin()
        if not origin:
            return
        key = self._key(session_id, turn_id)
        self._current_turn_by_session[key[0]] = key[1]
        lifecycle = self._routing_lifecycle(*key)
        lifecycle_mode = _s(lifecycle.get("mode")).lower()
        completion_ids = {_s(value) for value in lifecycle.get("delegation_ids", []) if _s(value)}
        with self._lock:
            transient = self._transient.get(key)
            if transient is None:
                transient = self._new_record(*key, origin, kind="request", phase="processing")
                self._transient[key] = transient
            if lifecycle_mode == "completion" and completion_ids:
                self._completion_turns[key] = completion_ids
                for did, row in self._active.items():
                    if did in completion_ids:
                        row["presentation_phase"] = "synthesis"
                        row["last_progress"] = time.time()
            elif lifecycle_mode == "verification":
                for did, row in self._active.items():
                    if did in completion_ids:
                        row["presentation_phase"] = "synthesis"
                        row["last_progress"] = time.time()
            route = self._route(origin)
        phase = "synthesis" if lifecycle_mode in {"completion", "verification"} else "processing"
        self._observe("llm_start", [transient], completion=(lifecycle_mode == "completion"), phase=phase)
        self._queue_route(route, delay=0.0)

    def on_pre_tool_call(self, tool_name: str, args: Any = None, **kw: Any) -> None:
        key = self._key(kw.get("session_id"), kw.get("turn_id"))
        if tool_name == "route_turn":
            origin = self._origin()
            if origin:
                with self._lock:
                    request = self._transient.get(key)
                    if request is None:
                        request = self._new_record(*key, origin, kind="request", phase="routing")
                        self._transient[key] = request
                    request["presentation_phase"] = "routing"
                    request["last_progress"] = time.time()
                self._observe("route_start", [request], phase="routing")
                self._queue_route(self._route(origin), delay=0.0)
            return
        if tool_name == "delegate_task" and isinstance(args, Mapping) and _s(args.get("action")).lower() not in {"list", "steer", "stop"}:
            count = len(args.get("tasks")) if isinstance(args.get("tasks"), list) else int(bool(args.get("goal")))
            origin = self._origin()
            if origin and self._in_scope(origin) and count:
                phase = self._delegation_phase(*key)
                with self._lock:
                    # The current request row provides an explicit, safe route-decision inference.
                    request = self._transient.get(key)
                    if request:
                        request["presentation_phase"] = "routing"
                    candidate = self._new_record(*key, origin, kind="delegation", phase=("verifier" if phase == "verifier" else "routing"), lanes=count)
                    candidate["lane_phase"] = phase
                    self._candidates[key] = candidate
                self._observe("delegation_prepare", [candidate], mode="candidate", phase=candidate["presentation_phase"])
                self._queue_route(self._route(origin), delay=0.0)
            return
        family = self._tool_family(tool_name)
        if not family:
            return
        with self._lock:
            row = self._find(*key)
            if row:
                samples = row.setdefault("samples", [])
                samples.append(family)
                del samples[:-2]
                row["last_progress"] = time.time()

    @staticmethod
    def _result_state(value: Any) -> str:
        raw = _s(value).lower()
        if raw in {"completed", "complete", "succeeded", "success"}:
            return "complete"
        if raw in {"interrupted", "stopped", "cancelled", "canceled"}:
            return "stopped"
        if raw in {"unknown", ""}:
            return "unknown"
        return "error"

    def _apply_result_lanes(self, row: dict[str, Any], data: Mapping[str, Any]) -> None:
        results = data.get("results")
        if not isinstance(results, list):
            return
        for index, result in enumerate(results[:len(row["lanes"])]):
            if isinstance(result, Mapping):
                lane = row["lanes"][index]
                lane.update(status=self._result_state(result.get("status")), ended_at=time.time())
                if not lane.get("started_at"):
                    lane["started_at"] = row["created_at"]
        if row["lanes"] and all(lane["status"] in _TERMINAL for lane in row["lanes"]):
            row["presentation_phase"] = "synthesis"

    def on_post_tool_call(self, tool_name: str, result: Any = None, status: str = "", **kw: Any) -> None:
        key = self._key(kw.get("session_id"), kw.get("turn_id"))
        if tool_name == "route_turn":
            data = _obj(result)
            accepted = _s(status).lower() in {"", "ok", "success", "completed"} and _s(data.get("status")).lower() in {"", "accepted", "ok"}
            mode = _s(data.get("mode")).lower()
            with self._lock:
                request = self._transient.get(key)
                if request:
                    request["presentation_phase"] = "delegating" if accepted and mode not in {"", "direct"} else "processing"
                    request["last_progress"] = time.time()
                    route = self._route(request["origin"])
                else:
                    route = None
            if request and route:
                self._observe("route_complete", [request], accepted=accepted, delegated=(mode not in {"", "direct"}), phase=request["presentation_phase"])
                self._queue_route(route, delay=0.0)
            return
        if tool_name != "delegate_task":
            return
        with self._lock:
            row = self._candidates.pop(key, None)
        if not row:
            return
        data = _obj(result)
        ok = _s(status).lower() in {"", "ok", "success", "completed"}
        if not ok:
            self._observe("delegation_rejected", [row], outcome="tool_error")
            self._queue_route(self._route(row["origin"]), delay=0.0)
            return
        background = data.get("mode") == "background" and bool(_s(data.get("delegation_id")))
        with self._lock:
            self._sequence += 1
            delegation_id = _s(data.get("delegation_id")) if background else f"sync:{key[0]}:{key[1]}:{self._sequence}"
            row["delegation_id"] = delegation_id
            row["mode"] = "background" if background else "sync"
            if background:
                row["presentation_phase"] = "verifier" if row.get("lane_phase") == "verifier" else "worker"
            else:
                self._apply_result_lanes(row, data)
                if row["presentation_phase"] not in {"synthesis", "verifier"}:
                    row["presentation_phase"] = "worker"
            self._active[delegation_id] = row
            self._persist()
        self._observe("delegation_promoted", [row], mode=row["mode"], phase=row["presentation_phase"])
        self._queue_route(self._route(row["origin"]), delay=0.0)

    def on_subagent_start(self, parent_session_id: Any = None, parent_turn_id: Any = None,
                          child_session_id: Any = None, child_role: Any = None, **_: Any) -> None:
        with self._lock:
            row = self._find(parent_session_id, parent_turn_id)
            if not row:
                return
            index = int(row["started"])
            row["started"] = index + 1
            if index >= len(row["lanes"]):
                return
            lane = row["lanes"][index]
            phase = "verifier" if _s(child_role).lower() in {"verifier", "reviewer", "review"} else row.get("lane_phase", "worker")
            lane.update(status="in_progress", phase=phase, started_at=time.time())
            row["presentation_phase"] = "verifier" if phase == "verifier" else ("delegating" if not row.get("mode") else "worker")
            row["last_progress"] = time.time()
            child_id = _s(child_session_id)
            if child_id:
                self._child_to_lane[child_id] = (_s(parent_session_id), _s(parent_turn_id), index)
            route = self._route(row["origin"])
            active = row in self._active.values()
            if active:
                self._persist()
        self._observe("subagent_start", [row], phase=row["presentation_phase"])
        self._queue_route(route, delay=0.0)

    def on_subagent_stop(self, child_session_id: Any = None, child_status: Any = None, **_: Any) -> None:
        with self._lock:
            mapping = self._child_to_lane.pop(_s(child_session_id), None)
            if not mapping:
                return
            row = self._find(*mapping[:2])
            if not row:
                return
            row["lanes"][mapping[2]].update(status=self._result_state(child_status), ended_at=time.time())
            row["last_progress"] = time.time()
            if all(lane["status"] in _TERMINAL for lane in row["lanes"]):
                row["presentation_phase"] = "synthesis"
            route = self._route(row["origin"])
            if row in self._active.values():
                self._persist()
        self._observe("subagent_stop", [row], phase=row["presentation_phase"])
        self._queue_route(route, delay=0.0)

    def on_transform_llm_output(self, response_text: Any = "", session_id: Any = None,
                                turn_id: Any = None, **_: Any) -> None:
        """Mark only the current turn's owned rows as response-writing."""
        key = self._key(session_id, turn_id or self._current_turn_by_session.get(_s(session_id), ""))
        routes: set[tuple[str, str, str]] = set()
        with self._lock:
            completion_ids = set(self._completion_turns.get(key, set()))
            targets = [row for row_key, row in self._transient.items() if row_key == key]
            active_targets = [(did, row) for did, row in self._active.items()
                              if did in completion_ids or
                              (row.get("mode") == "sync" and
                               self._key(row.get("parent_session_id"), row.get("parent_turn_id")) == key)]
            targets.extend(row for _, row in active_targets)
            candidate_keys = {row_key for row_key in self._candidates if row_key == key}
            targets.extend(self._candidates[row_key] for row_key in candidate_keys)
            if not targets:
                return None
            for row in targets:
                row["presentation_phase"] = "writing"
                row["last_progress"] = time.time()
                routes.add(self._route(row["origin"]))
            self._finalizing_turns[key] = {
                "active_ids": {did for did, _ in active_targets},
                "candidate_keys": candidate_keys,
            }
        self._observe("response_writing", targets, completion=bool(completion_ids))
        for route in routes:
            self._queue_route(route, delay=0.0)
        return None

    def _remove_session(self, session_id: str, *, remove_delegations: bool) -> set[tuple[str, str, str]]:
        routes: set[tuple[str, str, str]] = set()
        with self._lock:
            for key, row in list(self._transient.items()):
                if key[0] == session_id:
                    routes.add(self._route(row["origin"])); self._transient.pop(key, None)
            if remove_delegations:
                for did, row in list(self._active.items()):
                    if _s(row.get("parent_session_id")) == session_id:
                        routes.add(self._route(row["origin"])); self._active.pop(did, None)
                for key, row in list(self._candidates.items()):
                    if key[0] == session_id:
                        routes.add(self._route(row["origin"])); self._candidates.pop(key, None)
                self._child_to_lane = {cid: lane for cid, lane in self._child_to_lane.items() if lane[0] != session_id}
            self._persist()
        return routes

    def _remove_finalizing(self, key: tuple[str, str], plan: Mapping[str, Any]) -> set[tuple[str, str, str]]:
        """Remove only rows selected at the output boundary, preserving siblings."""
        routes: set[tuple[str, str, str]] = set()
        active_ids = set(plan.get("active_ids") or ())
        candidate_keys = set(plan.get("candidate_keys") or ())
        with self._lock:
            row = self._transient.pop(key, None)
            if row:
                routes.add(self._route(row["origin"]))
            for did, row in list(self._active.items()):
                if did in active_ids:
                    routes.add(self._route(row["origin"])); self._active.pop(did, None)
            for candidate_key in candidate_keys:
                row = self._candidates.pop(candidate_key, None)
                if row:
                    routes.add(self._route(row["origin"]))
            self._child_to_lane = {cid: lane for cid, lane in self._child_to_lane.items()
                                   if not (lane[0] == key[0] and lane[1] == key[1])}
            self._persist()
        return routes

    def on_post_llm_call(self, session_id: Any = None, turn_id: Any = None, **_: Any) -> None:
        key = self._key(session_id, turn_id)
        with self._lock:
            finalizing = self._finalizing_turns.pop(key, None)
            if finalizing is not None:
                self._completion_turns.pop(key, None)
        if finalizing is not None:
            routes = self._remove_finalizing(key, finalizing)
            self._observe("response_complete", outcome="clear", finalizing=bool(finalizing.get("active_ids")))
            for route in routes:
                self._after_parent_delivery(route)
            return None
        with self._lock:
            routes = {self._route(row["origin"]) for row in self._active.values()
                      if row.get("mode") == "background" and
                      self._key(row.get("parent_session_id"), row.get("parent_turn_id")) == key}
        for route in routes:
            self._after_parent_delivery(route)
        return None

    def on_session_end(self, session_id: Any = None, turn_id: Any = None, **_: Any) -> None:
        """Hermes emits this after every turn; preserve detached background rows."""
        key = self._key(session_id, turn_id)
        routes: set[tuple[str, str, str]] = set()
        with self._lock:
            if self._current_turn_by_session.get(key[0]) == key[1]:
                self._current_turn_by_session.pop(key[0], None)
            self._completion_turns.pop(key, None)
            self._finalizing_turns.pop(key, None)
            for row in (self._transient.pop(key, None), self._candidates.pop(key, None)):
                if row:
                    routes.add(self._route(row["origin"]))
            for did, row in list(self._active.items()):
                if row.get("mode") == "sync" and self._key(row.get("parent_session_id"), row.get("parent_turn_id")) == key:
                    routes.add(self._route(row["origin"])); self._active.pop(did, None)
            self._child_to_lane = {cid: lane for cid, lane in self._child_to_lane.items()
                                   if not (lane[0] == key[0] and lane[1] == key[1])}
            self._persist()
        for route in routes:
            self._queue_route(route, delay=0.0)

    def on_session_finalize(self, session_id: Any = None, **_: Any) -> None:
        """Release all owned rows only at the durable conversation boundary."""
        sid = _s(session_id)
        with self._lock:
            self._current_turn_by_session.pop(sid, None)
            for key in [key for key in self._completion_turns if key[0] == sid]:
                self._completion_turns.pop(key, None)
            for key in [key for key in self._finalizing_turns if key[0] == sid]:
                self._finalizing_turns.pop(key, None)
        routes = self._remove_session(sid, remove_delegations=True)
        for route in routes:
            self._queue_route(route, delay=0.0)

    def _text(self, records: list[dict[str, Any]]) -> str:
        if not records:
            return ""
        # A generic request row may coexist with an active delegation from the
        # same turn.  Delegation state is more specific and must not be masked
        # by the request's earlier routing/processing inference.
        delegated = [row for row in records if row.get("kind") == "delegation"]
        display_rows = delegated or records
        lanes = [lane for row in display_rows for lane in row.get("lanes", []) if lane["status"] not in _TERMINAL]
        if any(row.get("registry_status") in {"stalling", "stalled"} for row in display_rows):
            return self._status("stalled")
        if any((lane.get("phase") == "verifier" and lane.get("status") not in _TERMINAL)
               or row.get("presentation_phase") == "verifier"
               for row in display_rows for lane in (row.get("lanes") or [{}])):
            return self._status("verifier")
        phases = {row.get("presentation_phase") for row in display_rows}
        for phase in ("writing", "synthesis", "delegating", "routing", "processing"):
            if phase in phases:
                return self._status(phase)
        total = sum(len(row.get("lanes", [])) for row in display_rows)
        done = sum(1 for row in display_rows for lane in row.get("lanes", []) if lane["status"] == "complete")
        active = len(lanes)
        suffix = self._progress_suffix(display_rows, done, total, active)
        if done and active:
            return self._status("partial", done=done, total=total, active=active) + suffix
        stable = next((row["samples"][-1] for row in display_rows if len(row.get("samples", [])) >= 2
                       and row["samples"][-1] == row["samples"][-2]), "")
        if stable:
            return self._status(stable) + suffix
        if active > 1:
            return self._status("multiple", count=active) + suffix
        return self._status("worker") + suffix

    def _progress_suffix(self, records: list[dict[str, Any]], done: int, total: int, active: int) -> str:
        if not total:
            return ""
        percent = round(done * 100 / total)
        durations = [lane["ended_at"] - lane["started_at"] for row in records for lane in row.get("lanes", [])
                     if lane.get("status") == "complete" and lane.get("started_at", 0) > 0
                     and lane.get("ended_at", 0) > lane.get("started_at", 0)]
        if active and len(durations) >= 2:
            mean = sum(durations) / len(durations)
            now = time.time()
            remaining = [max(0.0, mean - (now - float(lane.get("started_at") or now))) for row in records
                         for lane in row.get("lanes", []) if lane["status"] not in _TERMINAL]
            return f" · {percent}% · 예상 약 {max(1, round(max(remaining or [mean])))}초"
        return f" · {percent}%" if done else ""

    @staticmethod
    def _tool_family(tool: Any) -> str:
        name = _s(tool).lower()
        if name in _WEB or name.startswith(("web_", "browser_")):
            return "web"
        if name in _FILE or name.startswith(("read_", "search_", "file_")):
            return "file"
        return ""

    def _sync_registry(self, records: list[dict[str, Any]]) -> None:
        try:
            from tools.async_delegation import list_async_delegations
            live = {_s(item.get("delegation_id")): item for item in list_async_delegations() if isinstance(item, Mapping)}
        except Exception:
            return
        with self._lock:
            for row in records:
                if row.get("mode") != "background":
                    continue
                item = live.get(_s(row.get("delegation_id")))
                if not item:
                    continue
                row["registry_status"] = _s(item.get("status")).lower()
                families = {self._tool_family(item.get("current_tool")) for item in item.get("children_activity", [])
                            if isinstance(item, Mapping)} - {""}
                sample = next(iter(families)) if len(families) == 1 else ""
                samples = row.setdefault("samples", [])
                samples.append(sample)
                del samples[:-2]

    @staticmethod
    def _adapter() -> Any:
        try:
            from gateway.config import Platform
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            adapter = getattr(runner, "adapters", {}).get(Platform.SLACK) if runner else None
            return adapter if adapter and bool(adapter.is_connected) else None
        except Exception:
            return None

    @staticmethod
    def _metadata(route: tuple[str, str, str]) -> dict[str, str]:
        metadata = {"thread_id": route[2], "thread_ts": route[2]}
        if route[0]:
            metadata["team_id"] = route[0]
        return metadata

    @staticmethod
    def _session_key() -> str:
        try:
            from gateway.session_context import get_session_env
            return _s(get_session_env("HERMES_SESSION_KEY", ""), 512)
        except Exception:
            return ""

    def _after_parent_delivery(self, route: tuple[str, str, str]) -> bool:
        adapter = self._adapter()
        session_key = self._session_key()
        register = getattr(adapter, "register_post_delivery_callback", None) if adapter else None
        if callable(register) and session_key:
            try:
                active = getattr(adapter, "_active_sessions", {}).get(session_key)
                generation = getattr(active, "_hermes_run_generation", None) if active is not None else None
                async def publish() -> None:
                    await self._publish_route(route)
                register(session_key, publish, generation=generation)
                return True
            except Exception as exc:
                _LOG.debug("post-delivery registration failed error_class=%s", type(exc).__name__)
        return self._queue_route(route, delay=self._seconds("reassert_delay_seconds", 2, .25, 10))

    def _route_status_store(self, adapter: Any) -> dict[tuple[str, str, str], str] | None:
        """Install a per-thread bridge so Hermes heartbeats preserve plugin phases."""
        if not callable(getattr(adapter, "send_typing", None)) or not callable(getattr(adapter, "_get_client", None)):
            return None
        existing = getattr(adapter, "_delegation_route_status", None)
        owner = getattr(adapter, "_delegation_status_bridge_owner", None)
        if owner is self._bridge_owner and isinstance(existing, dict):
            return existing
        if owner is not None:
            return None
        store: dict[tuple[str, str, str], str] = {}
        original = adapter.send_typing

        async def bridged_send_typing(chat_id: str, metadata: Any = None) -> None:
            await original(chat_id, metadata=metadata)
            values = metadata if isinstance(metadata, Mapping) else {}
            route = (_s(values.get("team_id")), _s(chat_id),
                     _s(values.get("thread_ts") or values.get("thread_id")))
            text = store.get(route)
            if not text or not route[2]:
                return
            try:
                client = adapter._get_client(route[1], team_id=route[0] or None)
                await client.assistant_threads_setStatus(channel_id=route[1], thread_ts=route[2], status=text)
            except Exception as exc:
                _LOG.debug("status heartbeat bridge failed error_class=%s", type(exc).__name__)

        try:
            setattr(adapter, "_delegation_route_status", store)
            setattr(adapter, "_delegation_status_bridge_owner", self._bridge_owner)
            setattr(adapter, "_delegation_status_original_send_typing", original)
            setattr(adapter, "send_typing", bridged_send_typing)
            self._bridged_adapter = adapter
        except Exception as exc:
            _LOG.debug("status heartbeat bridge unavailable error_class=%s", type(exc).__name__)
            return None
        return store

    def _set_route_status(self, adapter: Any, route: tuple[str, str, str], text: str | None) -> None:
        store = self._route_status_store(adapter)
        if store is None:
            return
        if text:
            store[route] = text
        else:
            store.pop(route, None)

    async def _checked_native_status(self, adapter: Any, route: tuple[str, str, str], text: str) -> bool | None:
        get_client = getattr(adapter, "_get_client", None)
        if not callable(get_client):
            return None
        self._set_route_status(adapter, route, text)
        try:
            client = get_client(route[1], team_id=route[0] or None)
            send_status = getattr(client, "assistant_threads_setStatus", None)
            if not callable(send_status):
                return None
            await send_status(channel_id=route[1], thread_ts=route[2], status=text)
            return True
        except Exception as exc:
            _LOG.debug("native Slack Assistant status failed error_class=%s", type(exc).__name__)
            return False

    async def _send_fallback_once(self, adapter: Any, route: tuple[str, str, str], rows: list[dict[str, Any]], text: str) -> None:
        with self._lock:
            if route in self._route_fallback_sent:
                return
            self._route_fallback_sent.add(route)
        send = getattr(adapter, "send", None)
        if not callable(send):
            with self._lock:
                self._route_fallback_sent.discard(route)
            return
        try:
            await send(route[1], self._status("fallback"), reply_to=route[2], metadata=self._metadata(route))
        except Exception:
            with self._lock:
                self._route_fallback_sent.discard(route)
            return
        with self._lock:
            for row in rows:
                row["last_text"] = text
                row["last_text_at"] = time.time()
        self._observe("fallback_sent", rows, outcome="native_status_failed")

    def _queue_route(self, route: tuple[str, str, str], delay: float = 0.0, force_clear: bool = False) -> bool:
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            loop = getattr(runner, "_gateway_loop", None) if runner else None
            if loop is None or loop.is_closed():
                raise RuntimeError
            if delay <= 0:
                asyncio.run_coroutine_threadsafe(self._publish_route(route, force_clear), loop)
            else:
                loop.call_soon_threadsafe(lambda: loop.call_later(delay, lambda: asyncio.create_task(self._publish_route(route, force_clear))))
            return True
        except Exception:
            return False

    def _publication_current(self, generation: int) -> bool:
        with self._lock:
            return not self._closed and self._generation == generation

    async def _clear_stale_publication(self, adapter: Any, route: tuple[str, str, str]) -> None:
        store = getattr(adapter, "_delegation_route_status", None)
        if isinstance(store, dict):
            store.pop(route, None)
        if not callable(getattr(adapter, "stop_typing", None)):
            return
        try:
            await adapter.stop_typing(route[1], metadata=self._metadata(route))
        except Exception as exc:
            _LOG.debug("stale status clear failed error_class=%s", type(exc).__name__)

    async def _publish_route(self, route: tuple[str, str, str], force_clear: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            generation = self._generation
            rows = self._records(route)
            expired = bool(rows) and min(row["created_at"] for row in rows) + self._max_age() <= time.time()
            if expired:
                self._active = {did: row for did, row in self._active.items() if self._route(row["origin"]) != route}
                self._candidates = {key: row for key, row in self._candidates.items() if self._route(row["origin"]) != route}
                self._transient = {key: row for key, row in self._transient.items() if self._route(row["origin"]) != route}
                self._persist()
                rows = []
        adapter = self._adapter()
        if not adapter:
            return
        if not self._publication_current(generation):
            return
        if force_clear or expired or not rows:
            self._set_route_status(adapter, route, None)
            if not rows:
                with self._lock:
                    self._route_fallback_sent.discard(route)
            set_text = getattr(adapter, "set_status_text", None)
            if callable(set_text):
                try:
                    set_text(route[1], None)
                except Exception:
                    pass
            stop = getattr(adapter, "stop_typing", None)
            if callable(stop):
                try:
                    await stop(route[1], metadata=self._metadata(route))
                except Exception:
                    pass
            self._observe("status_clear", outcome=("forced" if force_clear else "empty"))
            return
        self._sync_registry(rows)
        text = self._text(rows)
        now = time.time()
        emergency = text in {self._status("stalled"), self._status("verifier"), self._status("writing")}
        if not emergency and all(row.get("last_text") and row.get("last_text") != text
                                 and now - row.get("last_text_at", 0) < self._throttle() for row in rows):
            self._arm(route, rows)
            return
        if all(row.get("last_text") == text and now - row.get("last_text_at", 0) < self._refresh() for row in rows):
            self._arm(route, rows)
            return
        try:
            checked = await self._checked_native_status(adapter, route, text)
            if checked is None:
                set_text, send = getattr(adapter, "set_status_text", None), getattr(adapter, "send_typing", None)
                if not callable(set_text) or not callable(send):
                    await self._send_fallback_once(adapter, route, rows, text)
                else:
                    set_text(route[1], text)
                    await send(route[1], metadata=self._metadata(route))
            elif checked is False:
                await self._send_fallback_once(adapter, route, rows, text)
            if not self._publication_current(generation):
                await self._clear_stale_publication(adapter, route)
                return
            with self._lock:
                for row in rows:
                    row["last_text"] = text
                    row["last_text_at"] = now
            self._observe("status_publish", rows, phase=next(iter({row.get("presentation_phase") for row in rows}), ""))
        except Exception as exc:
            _LOG.debug("status publication failed error_class=%s", type(exc).__name__)
        if self._publication_current(generation):
            self._arm(route, rows)

    def _arm(self, route: tuple[str, str, str], rows: list[dict[str, Any]]) -> None:
        if route in self._refresh_armed:
            return
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            loop = getattr(runner, "_gateway_loop", None) if runner else None
            if not loop or loop.is_closed():
                return
            delay = max(.1, min(self._sample(), min(row["created_at"] + self._max_age() - time.time() for row in rows)))
            def tick() -> None:
                self._refresh_armed.pop(route, None)
                asyncio.create_task(self._publish_route(route))
            self._refresh_armed[route] = loop.call_later(delay, tick)
        except Exception as exc:
            _LOG.debug("status refresh scheduling failed error_class=%s", type(exc).__name__)

    def shutdown(self) -> None:
        """Cancel owned work and restore an adapter bridge installed by this instance."""
        with self._lock:
            self._closed = True
            self._generation += 1
            routes = {self._route(row["origin"]) for row in
                      (*self._transient.values(), *self._candidates.values(), *self._active.values())}
            handles = list(self._refresh_armed.values())
            self._refresh_armed.clear()
            self._transient.clear(); self._candidates.clear(); self._active.clear()
            self._child_to_lane.clear(); self._completion_turns.clear(); self._finalizing_turns.clear()
            self._current_turn_by_session.clear(); self._route_fallback_sent.clear()
            self._persist()
        for handle in handles:
            try:
                handle.cancel()
            except Exception:
                pass
        # Publication ignores disconnected transports, but unload must still
        # restore a bridge installed before a disconnect.
        adapter = self._bridged_adapter or self._adapter()
        if adapter and getattr(adapter, "_delegation_status_bridge_owner", None) is self._bridge_owner:
            original = getattr(adapter, "_delegation_status_original_send_typing", None)
            if callable(original):
                adapter.send_typing = original
            for name in ("_delegation_route_status", "_delegation_status_bridge_owner", "_delegation_status_original_send_typing"):
                try:
                    delattr(adapter, name)
                except Exception:
                    pass
        self._bridged_adapter = None
        if adapter:
            try:
                from gateway.run import _gateway_runner_ref
                runner = _gateway_runner_ref()
                loop = getattr(runner, "_gateway_loop", None) if runner else None
                if loop and not loop.is_closed():
                    for route in routes:
                        asyncio.run_coroutine_threadsafe(adapter.stop_typing(route[1], metadata=self._metadata(route)), loop)
            except Exception as exc:
                _LOG.debug("status unload clear failed error_class=%s", type(exc).__name__)


_SERVICE = None


def register(ctx: Any) -> None:
    global _SERVICE
    _SERVICE = DelegationStatus(ctx)
    for name, callback in [
        ("pre_gateway_dispatch", _SERVICE.on_pre_gateway_dispatch), ("pre_tool_call", _SERVICE.on_pre_tool_call),
        ("post_tool_call", _SERVICE.on_post_tool_call), ("pre_llm_call", _SERVICE.on_pre_llm_call),
        ("post_llm_call", _SERVICE.on_post_llm_call), ("transform_llm_output", _SERVICE.on_transform_llm_output),
        ("on_session_end", _SERVICE.on_session_end), ("on_session_finalize", _SERVICE.on_session_finalize),
        ("subagent_start", _SERVICE.on_subagent_start),
        ("subagent_stop", _SERVICE.on_subagent_stop),
    ]:
        ctx.register_hook(name, callback)
    ctx.on_unload(_SERVICE.shutdown)
    scope = _SERVICE._scope()
    _LOG.info("Enabled %s v%s scope_configured=%s", PLUGIN_ID, PLUGIN_VERSION, bool(scope["chat_id"] and scope["thread_id"]))
