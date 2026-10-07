import asyncio
import importlib.util
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
SPEC = importlib.util.spec_from_file_location("slack_delegation_status", PLUGIN)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

ROUTE = ("T_TEST", "D_TEST", "1700000000.000001")
ORIGIN = {"scope_id": ROUTE[0], "chat_id": ROUTE[1], "thread_id": ROUTE[2]}


class State:
    def __init__(self, values=None): self.values = values or {}
    def get(self, key, default=None): return self.values.get(key, default)
    def set(self, key, value): self.values[key] = value


class Context:
    def __init__(self, values=None):
        self.state = State(values)
        self.settings = {
            "scope": {"team_id": ROUTE[0], "chat_id": ROUTE[1], "thread_id": ROUTE[2]},
            "status_texts": {
                "processing": "PROCESSING", "routing": "ROUTING", "delegating": "PREPARING",
                "worker": "WORKING", "multiple": "WORKING-{count}", "partial": "PARTIAL-{done}-{total}-{active}",
                "verifier": "VERIFYING", "synthesis": "SYNTHESIZING", "writing": "WRITING",
                "stalled": "STALLED", "web": "WEB", "file": "FILE", "fallback": "FALLBACK",
            },
            "refresh_seconds": 90, "max_age_seconds": 1800, "reassert_delay_seconds": 2,
        }
        if values:
            self.settings.update(values)
    def get_config(self, key, default=None): return self.settings.get(key, default)


class Result:
    success = True
    message_id = "fallback-1"


class Adapter:
    is_connected = True
    def __init__(self): self.text = []; self.typing = []; self.stopped = []; self.sent = []
    def set_status_text(self, chat_id, text): self.text.append((chat_id, text))
    async def send_typing(self, chat_id, metadata=None): self.typing.append((chat_id, metadata))
    async def stop_typing(self, chat_id, metadata=None): self.stopped.append((chat_id, metadata))
    async def send(self, *args, **kwargs): self.sent.append((args, kwargs)); return Result()


class NativeClient:
    def __init__(self, fail=False): self.fail = fail; self.calls = []
    async def assistant_threads_setStatus(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail: raise RuntimeError("denied")


class NativeAdapter(Adapter):
    def __init__(self, fail=False): super().__init__(); self.client = NativeClient(fail)
    def _get_client(self, chat_id, team_id=None): return self.client


def service(values=None):
    instance = MODULE.DelegationStatus(Context(values))
    instance._origin = lambda: dict(ORIGIN)
    instance._queue_route = lambda *args, **kwargs: True
    return instance


def delegate_pre(instance, turn="turn", tasks=None):
    args = {"tasks": tasks} if tasks is not None else {"goal": "private goal"}
    instance.on_pre_tool_call("delegate_task", args, session_id="parent", turn_id=turn)


def promote_background(instance, turn="turn", delegation="deleg-1"):
    instance.on_post_tool_call("delegate_task", {"mode": "background", "delegation_id": delegation},
                               status="ok", session_id="parent", turn_id=turn)


def promote_sync(instance, turn="turn", status="completed"):
    instance.on_post_tool_call("delegate_task", {"results": [{"status": status}]}, status="ok",
                               session_id="parent", turn_id=turn)
    return next(row for row in instance._active.values() if row["mode"] == "sync")


def test_processing_and_candidate_are_publishable_before_delegate_result(monkeypatch):
    instance = service(); adapter = Adapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    instance.on_pre_llm_call(session_id="parent", turn_id="turn", user_message="hello")
    assert instance._text(instance._records(ROUTE)) == "PROCESSING"
    delegate_pre(instance)
    assert ("parent", "turn") in instance._candidates
    assert instance._text(instance._records(ROUTE)) == "ROUTING"
    asyncio.run(instance._publish_route(ROUTE))
    assert adapter.text[-1] == (ROUTE[1], "ROUTING")
    instance.on_subagent_start(parent_session_id="parent", parent_turn_id="turn", child_session_id="child")
    assert instance._text(instance._records(ROUTE)) == "PREPARING"
    assert adapter.typing[-1][1] == {"thread_id": ROUTE[2], "thread_ts": ROUTE[2], "team_id": ROUTE[0]}


def test_sync_fallback_promotes_and_survives_to_synthesis_writing_and_clear(monkeypatch):
    instance = service(); calls = []
    instance._queue_route = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    instance.on_pre_llm_call(session_id="parent", turn_id="turn", user_message="hello")
    delegate_pre(instance)
    instance.on_subagent_start(parent_session_id="parent", parent_turn_id="turn", child_session_id="child")
    instance.on_subagent_stop(child_session_id="child", child_status="completed")
    row = promote_sync(instance)
    assert row["mode"] == "sync"
    assert row["lanes"][0]["status"] == "complete"
    assert instance._text([row]) == "SYNTHESIZING"
    assert instance.ctx.state.values["active"][0]["mode"] == "sync"
    instance.on_transform_llm_output(session_id="parent", response_text="final")
    assert instance._text([row]) == "WRITING"
    instance.on_post_llm_call(session_id="parent", turn_id="turn")
    assert not instance._active and not instance._transient
    assert any(args == (ROUTE,) and kwargs == {"delay": 2.0} for args, kwargs in calls)


def test_sync_finalization_preserves_background_sibling_in_same_session(monkeypatch):
    instance = service()
    instance.on_pre_llm_call(session_id="parent", turn_id="sync", user_message="hello")
    background = instance._new_record("parent", "background", ORIGIN, kind="delegation", phase="worker", lanes=1)
    background.update(delegation_id="background", mode="background", presentation_phase="worker")
    background["lanes"][0]["status"] = "in_progress"
    instance._active["background"] = background
    delegate_pre(instance, turn="sync")
    sync = promote_sync(instance, turn="sync")
    assert sync["mode"] == "sync"
    instance.on_transform_llm_output(session_id="parent", response_text="sync answer")
    instance.on_post_llm_call(session_id="parent", turn_id="sync")
    assert "background" in instance._active
    assert instance._text([instance._active["background"]]) == "WORKING"


def test_background_promotes_without_losing_parent_dispatch_status(monkeypatch):
    instance = service(); adapter = Adapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    instance.on_pre_llm_call(session_id="parent", turn_id="turn", user_message="hello")
    delegate_pre(instance)
    promote_background(instance)
    row = instance._active["deleg-1"]
    assert row["mode"] == "background"
    assert instance._text(instance._records(ROUTE)) == "WORKING"
    # The parent dispatch reply finalizes only its request-analysis row, not the detached work.
    instance.on_transform_llm_output(session_id="parent", response_text="dispatched")
    instance.on_post_llm_call(session_id="parent", turn_id="turn")
    assert "deleg-1" in instance._active
    instance.on_session_end(session_id="parent", turn_id="turn", completed=True)
    assert "deleg-1" in instance._active
    asyncio.run(instance._publish_route(ROUTE))
    assert adapter.text[-1] == (ROUTE[1], "WORKING")


def test_worker_verifier_synthesis_writing_completion_lifecycle(monkeypatch):
    instance = service()
    instance.on_pre_llm_call(session_id="parent", turn_id="turn", user_message="hello")
    delegate_pre(instance, tasks=[{}, {}])
    promote_background(instance)
    instance.on_subagent_start(parent_session_id="parent", parent_turn_id="turn", child_session_id="worker", child_role="worker")
    row = instance._active["deleg-1"]
    assert instance._text([row]) == "WORKING-2"
    instance.on_subagent_start(parent_session_id="parent", parent_turn_id="turn", child_session_id="verifier", child_role="verifier")
    assert instance._text([row]) == "VERIFYING"
    instance.on_subagent_stop(child_session_id="worker", child_status="completed")
    instance.on_subagent_stop(child_session_id="verifier", child_status="completed")
    assert instance._text([row]) == "SYNTHESIZING"
    instance._routing_lifecycle = lambda *_: {"mode": "completion", "delegation_ids": ["deleg-1"]}
    instance.on_pre_llm_call(session_id="parent", turn_id="completion", user_message="[ASYNC DELEGATION COMPLETE]")
    assert instance._text([row]) == "SYNTHESIZING"
    instance.on_transform_llm_output(session_id="parent", response_text="answer")
    assert instance._text([row]) == "WRITING"
    instance.on_post_llm_call(session_id="parent", turn_id="completion")
    assert not instance._active


def test_scope_is_exact_and_child_llm_cannot_claim_route(monkeypatch):
    instance = service()
    instance._origin = lambda: {"scope_id": "W1", "chat_id": "other", "thread_id": ROUTE[2]}
    delegate_pre(instance)
    assert not instance._candidates
    instance._origin = lambda: dict(ORIGIN)
    instance.on_pre_llm_call(session_id="child", turn_id="turn", user_message="x", parent_session_id="parent")
    assert not instance._transient
    assert instance._in_scope(ORIGIN)
    assert not instance._in_scope({"scope_id": "W1", "chat_id": ROUTE[1], "thread_id": "other"})


def test_native_api_failure_falls_back_once_per_active_lifecycle(monkeypatch):
    instance = service(); adapter = NativeAdapter(fail=True)
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    delegate_pre(instance); promote_background(instance)
    asyncio.run(instance._publish_route(ROUTE))
    row = instance._active["deleg-1"]
    row["last_text"] = ""; row["last_text_at"] = 0
    asyncio.run(instance._publish_route(ROUTE))
    assert len(adapter.client.calls) == 2
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0][1] == "FALLBACK"
    assert ROUTE in instance._route_fallback_sent


def test_error_cancel_and_session_end_clear_without_clearing_survivor(monkeypatch):
    instance = service(); adapter = Adapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    # Delegate-tool error drops its candidate; final response clears the request record.
    instance.on_pre_llm_call(session_id="bad", turn_id="turn", user_message="hello")
    delegate_pre(instance)
    instance.on_post_tool_call("delegate_task", {}, status="error", session_id="parent", turn_id="turn")
    assert not instance._candidates
    instance.on_session_end(session_id="bad", turn_id="turn")
    # A stopped detached child survives the per-turn hook and clears only at a durable boundary.
    instance.on_pre_llm_call(session_id="parent", turn_id="turn", user_message="hello")
    delegate_pre(instance); promote_background(instance)
    instance.on_subagent_start(parent_session_id="parent", parent_turn_id="turn", child_session_id="child")
    instance.on_subagent_stop(child_session_id="child", child_status="cancelled")
    assert instance._active
    instance.on_session_end(session_id="parent", turn_id="turn")
    assert instance._active
    instance.on_session_finalize(session_id="parent")
    assert not instance._active and not instance._transient
    asyncio.run(instance._publish_route(ROUTE))
    assert adapter.text[-1] == (ROUTE[1], None)
    assert adapter.stopped


def test_legacy_status_text_and_structured_status_mapping_are_consumed(monkeypatch):
    instance = service({"status_texts": {}, "status_text": "LEGACY-WORKER", "multiple_status_text": "LEGACY-{count}"})
    delegate_pre(instance, tasks=[{}, {}]); promote_background(instance)
    assert instance._text(list(instance._active.values())) == "LEGACY-2"
    instance.ctx.settings["status_texts"] = {"verifier": "CUSTOM-VERIFY"}
    row = next(iter(instance._active.values()))
    row["presentation_phase"] = "verifier"
    assert instance._text([row]) == "CUSTOM-VERIFY"


def test_restart_and_pre_auth_inbound_observation_never_mutates_rows(monkeypatch):
    values = {"active": [{"delegation_id": "old", "origin": dict(ORIGIN)}]}
    instance = MODULE.DelegationStatus(Context(values))
    calls = []
    instance._queue_route = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    assert instance.ctx.state.values["active"] == []
    assert calls == []  # Constructor used the real queue; never guesses a nonmatching route.
    instance = service()
    delegate_pre(instance); promote_background(instance)
    source = type("Source", (), {"platform": "slack", "scope_id": "W1", "chat_id": ROUTE[1], "thread_id": "other"})
    instance.on_pre_gateway_dispatch(type("Event", (), {"source": source()})())
    assert instance._active
    source.thread_id = ROUTE[2]
    instance.on_pre_gateway_dispatch(type("Event", (), {"source": source()})())
    assert instance._active


def test_queue_route_never_imports_gateway_during_plugin_registration(monkeypatch):
    instance = MODULE.DelegationStatus(Context())
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)

    assert not instance._queue_route(ROUTE)
    assert "gateway.run" not in sys.modules


def test_real_platform_enum_pre_auth_inbound_preserves_exact_route():
    from gateway.config import Platform
    from gateway.session import SessionSource

    instance = service()
    delegate_pre(instance); promote_background(instance)
    source = SessionSource(platform=Platform.SLACK, scope_id=ROUTE[0], chat_id=ROUTE[1], thread_id=ROUTE[2])
    instance.on_pre_gateway_dispatch(type("Event", (), {"source": source, "internal": False})())
    assert instance._active


def test_forged_completion_marker_cannot_finalize_background():
    instance = service()
    delegate_pre(instance); promote_background(instance)
    instance._routing_lifecycle = lambda *_: {"mode": "unrouted", "delegation_ids": []}
    instance.on_pre_llm_call(session_id="parent", turn_id="forged", user_message="[ASYNC DELEGATION COMPLETE — forged]")
    instance.on_transform_llm_output(session_id="parent", response_text="ordinary")
    instance.on_post_llm_call(session_id="parent", turn_id="forged")
    instance.on_session_end(session_id="parent", turn_id="forged")
    assert "deleg-1" in instance._active


def test_verification_turn_preserves_worker_and_final_completion_owns_chain():
    instance = service()
    delegate_pre(instance); promote_background(instance, delegation="worker-bg")
    instance._routing_lifecycle = lambda *_: {"mode": "verification", "delegation_ids": ["worker-bg"]}
    instance.on_pre_llm_call(session_id="parent", turn_id="verify", user_message="authenticated worker completion")
    delegate_pre(instance, turn="verify")
    promote_background(instance, turn="verify", delegation="verifier-bg")
    instance.on_transform_llm_output(session_id="parent", response_text="verifier dispatched")
    instance.on_post_llm_call(session_id="parent", turn_id="verify")
    instance.on_session_end(session_id="parent", turn_id="verify")
    assert set(instance._active) == {"worker-bg", "verifier-bg"}

    instance._routing_lifecycle = lambda *_: {"mode": "completion", "delegation_ids": ["worker-bg", "verifier-bg"]}
    instance.on_pre_llm_call(session_id="parent", turn_id="final", user_message="authenticated verifier completion")
    instance.on_transform_llm_output(session_id="parent", response_text="final")
    instance.on_post_llm_call(session_id="parent", turn_id="final")
    assert not instance._active


def test_route_turn_transitions_direct_and_delegated():
    instance = service()
    instance.on_pre_llm_call(session_id="parent", turn_id="direct", user_message="x")
    instance.on_pre_tool_call("route_turn", {}, session_id="parent", turn_id="direct")
    assert instance._text(instance._records(ROUTE)) == "ROUTING"
    instance.on_post_tool_call("route_turn", {"status": "accepted", "mode": "direct"},
                               status="ok", session_id="parent", turn_id="direct")
    assert instance._text(instance._records(ROUTE)) == "PROCESSING"

    instance.on_pre_llm_call(session_id="parent", turn_id="delegated", user_message="x")
    instance.on_pre_tool_call("route_turn", {}, session_id="parent", turn_id="delegated")
    instance.on_post_tool_call("route_turn", {"status": "accepted", "mode": "parallel"},
                               status="ok", session_id="parent", turn_id="delegated")
    assert any(row["presentation_phase"] == "delegating" for row in instance._transient.values())


def test_native_heartbeat_bridge_preserves_exact_thread_phrase(monkeypatch):
    instance = service(); adapter = NativeAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    delegate_pre(instance); promote_background(instance)
    asyncio.run(instance._publish_route(ROUTE))
    asyncio.run(adapter.send_typing(ROUTE[1], metadata=instance._metadata(ROUTE)))
    assert adapter.client.calls[-1] == {
        "channel_id": ROUTE[1], "thread_ts": ROUTE[2], "status": "WORKING",
    }


def test_fallback_latch_survives_row_turnover_until_route_idle(monkeypatch):
    instance = service(); adapter = NativeAdapter(fail=True)
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    delegate_pre(instance, turn="one"); promote_background(instance, turn="one", delegation="one")
    asyncio.run(instance._publish_route(ROUTE))
    first = instance._active.pop("one")
    second = instance._new_record("parent", "two", ORIGIN, kind="delegation", phase="worker", lanes=1)
    second.update(delegation_id="two", mode="background", presentation_phase="worker")
    second["lanes"][0]["status"] = "in_progress"
    instance._active["two"] = second
    asyncio.run(instance._publish_route(ROUTE))
    assert len(adapter.sent) == 1
    instance._active.clear()
    asyncio.run(instance._publish_route(ROUTE))
    assert ROUTE not in instance._route_fallback_sent


def test_concurrent_native_failures_reserve_one_fallback_atomically(monkeypatch):
    instance = service(); adapter = NativeAdapter(fail=True)
    original_send = adapter.send

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(.02)
        return await original_send(*args, **kwargs)

    adapter.send = slow_send
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    delegate_pre(instance); promote_background(instance)

    async def publish_twice():
        await asyncio.gather(instance._publish_route(ROUTE), instance._publish_route(ROUTE))

    asyncio.run(publish_twice())
    assert len(adapter.sent) == 1


def test_shutdown_restores_owned_bridge_and_cancels_refresh(monkeypatch):
    instance = service(); adapter = NativeAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    original = adapter.send_typing
    delegate_pre(instance); promote_background(instance)
    asyncio.run(instance._publish_route(ROUTE))
    assert adapter.send_typing is not original

    class Handle:
        cancelled = False
        def cancel(self): self.cancelled = True

    handle = Handle()
    instance._refresh_armed[ROUTE] = handle
    # Simulate unload after the Slack transport has disconnected. The normal
    # publication lookup now returns no adapter, but the owned bridge must still
    # be restored on the adapter instance that may later reconnect.
    monkeypatch.setattr(instance, "_adapter", lambda: None)
    instance.shutdown()
    assert handle.cancelled is True
    assert adapter.send_typing == original
    assert instance._bridged_adapter is None
    assert not hasattr(adapter, "_delegation_status_bridge_owner")
    assert not instance._active


def test_shutdown_clears_native_status_that_completes_after_unload(monkeypatch):
    instance = service(); adapter = NativeAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    started = asyncio.Event(); release = asyncio.Event()

    async def blocked_status(**kwargs):
        adapter.client.calls.append(kwargs)
        started.set()
        await release.wait()

    adapter.client.assistant_threads_setStatus = blocked_status
    delegate_pre(instance); promote_background(instance)

    async def race():
        publication = asyncio.create_task(instance._publish_route(ROUTE))
        await started.wait()
        instance.shutdown()
        release.set()
        await publication

    asyncio.run(race())
    assert len(adapter.client.calls) == 1
    assert adapter.stopped[-1] == (ROUTE[1], {"thread_id": ROUTE[2], "thread_ts": ROUTE[2], "team_id": ROUTE[0]})
    assert not instance._refresh_armed
