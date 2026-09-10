import asyncio
import importlib.util
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
SPEC = importlib.util.spec_from_file_location("slack_delegation_status", PLUGIN)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class State:
    def __init__(self, values=None): self.values = values or {}
    def get(self, key, default=None): return self.values.get(key, default)
    def set(self, key, value): self.values[key] = value


class Context:
    def __init__(self, values=None):
        self.state = State(values)
        self.settings = {
            "scope": {"team_id": "W1", "chat_id": "D0B91EGBA56", "thread_id": "1788759304.795359"},
            "status_text": "비동기 위임 작업 중…",
            "multiple_status_text": "{count}개 작업을 처리 중…",
            "verifier_status_text": "결과를 검증 중…",
            "refresh_seconds": 90,
            "max_age_seconds": 1800,
            "reassert_delay_seconds": 2,
        }
    def get_config(self, key, default=None): return self.settings.get(key, default)


class Result:
    success = True
    message_id = "fallback-1"


class StatusAdapter:
    is_connected = True
    def __init__(self): self.text = []; self.typing = []; self.stopped = []; self.sent = []
    def set_status_text(self, chat_id, text): self.text.append((chat_id, text))
    async def send_typing(self, chat_id, metadata=None): self.typing.append((chat_id, metadata))
    async def stop_typing(self, chat_id, metadata=None): self.stopped.append((chat_id, metadata))
    async def send(self, *args, **kwargs): self.sent.append((args, kwargs)); return Result()


class NoStatusAdapter:
    is_connected = True
    def __init__(self): self.sent = []
    async def send(self, *args, **kwargs): self.sent.append((args, kwargs)); return Result()


class NativeClient:
    def __init__(self, fail=False): self.fail = fail; self.calls = []
    async def assistant_threads_setStatus(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail: raise RuntimeError("scope denied")
        return {"ok": True}


class CheckedNativeAdapter(StatusAdapter):
    def __init__(self, fail=False):
        super().__init__(); self.client = NativeClient(fail); self.client_requests = []
    def _get_client(self, chat_id, team_id=None):
        self.client_requests.append((chat_id, team_id)); return self.client


class PostDeliveryAdapter(StatusAdapter):
    def __init__(self):
        super().__init__(); self.callbacks = []
        event = type("RunEvent", (), {"_hermes_run_generation": 7})()
        self._active_sessions = {"gateway-session-key": event}
    def register_post_delivery_callback(self, session_key, callback, generation=None):
        self.callbacks.append((session_key, callback, generation))


def service(monkeypatch, values=None):
    instance = MODULE.DelegationStatus(Context(values))
    instance._origin = lambda: {"scope_id": "W1", "chat_id": "D0B91EGBA56", "thread_id": "1788759304.795359", "user_id": "U1"}
    instance._queue_route = lambda *args, **kwargs: True
    return instance


def dispatch(instance, turn="turn", delegation="deleg-1", tasks=None):
    args = {"tasks": tasks} if tasks is not None else {"goal": "private goal"}
    instance.on_pre_tool_call("delegate_task", args, session_id="parent", turn_id=turn)
    instance.on_subagent_start(parent_session_id="parent", parent_turn_id=turn, child_session_id=f"child-{delegation}")
    instance.on_post_tool_call("delegate_task", {"mode": "background", "delegation_id": delegation}, status="ok", session_id="parent", turn_id=turn)


def route(): return ("W1", "D0B91EGBA56", "1788759304.795359")


def test_accepts_background_single_goal_without_persisting_goal(monkeypatch):
    instance = service(monkeypatch)
    dispatch(instance)
    assert "deleg-1" in instance._active
    assert "private goal" not in str(instance.ctx.state.values)
    assert instance._active["deleg-1"]["lanes"][0]["status"] == "in_progress"


def test_primary_surface_uses_adapter_status_path_with_exact_thread(monkeypatch):
    instance = service(monkeypatch); adapter = StatusAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    dispatch(instance)
    asyncio.run(instance._publish_route(route()))
    assert adapter.text == [("D0B91EGBA56", "비동기 위임 작업 중…")]
    assert adapter.typing[0][1]["thread_id"] == "1788759304.795359"
    assert adapter.typing[0][1]["team_id"] == "W1"
    assert not adapter.sent


def test_checked_native_status_uses_exact_team_chat_and_thread(monkeypatch):
    instance = service(monkeypatch); adapter = CheckedNativeAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    dispatch(instance)
    asyncio.run(instance._publish_route(route()))
    assert adapter.client_requests == [("D0B91EGBA56", "W1")]
    assert adapter.client.calls == [{"channel_id": "D0B91EGBA56", "thread_ts": "1788759304.795359", "status": "비동기 위임 작업 중…"}]
    assert not adapter.text and not adapter.typing and not adapter.sent


def test_checked_native_failure_uses_one_bounded_fallback_card(monkeypatch):
    instance = service(monkeypatch); adapter = CheckedNativeAdapter(fail=True)
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    dispatch(instance)
    asyncio.run(instance._publish_route(route()))
    rec = instance._active["deleg-1"]
    rec["last_text"] = ""; rec["last_text_at"] = 0
    asyncio.run(instance._publish_route(route()))
    assert len(adapter.client.calls) == 2
    assert len(adapter.sent) == 1
    assert not adapter.text
    assert rec["fallback_sent"] is True


def test_editable_message_is_only_fallback_when_status_cannot_be_called(monkeypatch):
    instance = service(monkeypatch); adapter = NoStatusAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    dispatch(instance)
    asyncio.run(instance._publish_route(route()))
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1]["reply_to"] == "1788759304.795359"


def test_concurrent_same_thread_clears_only_after_last_child(monkeypatch):
    instance = service(monkeypatch); calls = []
    instance._queue_route = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    dispatch(instance, turn="one", delegation="one")
    dispatch(instance, turn="two", delegation="two")
    instance.on_subagent_stop(child_session_id="child-one", child_status="completed")
    assert "two" in instance._active
    assert calls[-1][1]["force_clear"] is False
    instance.on_subagent_stop(child_session_id="child-two", child_status="error")
    assert instance._active
    assert calls[-1][1]["force_clear"] is False
    instance.on_pre_llm_call(session_id="parent", turn_id="final", user_message="[ASYNC DELEGATION BATCH COMPLETE — two]")
    instance.on_transform_llm_output(response_text="done", session_id="parent")
    assert not instance._active
    assert calls[-1][1] == {"delay": 10.0, "force_clear": True}


def test_inbound_scoped_thread_clears_immediately_but_other_thread_is_inert(monkeypatch):
    instance = service(monkeypatch); calls = []
    instance._queue_route = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    dispatch(instance)
    Source = type("Source", (), {"platform": "slack", "scope_id": "W1", "chat_id": "D0B91EGBA56", "thread_id": "other"})
    instance.on_pre_gateway_dispatch(type("Event", (), {"source": Source()})())
    assert instance._active
    Source.thread_id = "1788759304.795359"
    instance.on_pre_gateway_dispatch(type("Event", (), {"source": Source()})())
    assert not instance._active
    assert calls[-1][1]["force_clear"] is True


def test_max_age_clears_exact_route_without_fallback(monkeypatch):
    instance = service(monkeypatch); adapter = StatusAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    dispatch(instance)
    instance._active["deleg-1"]["created_at"] = time.time() - 1801
    asyncio.run(instance._publish_route(route()))
    assert not instance._active
    assert adapter.text[-1] == ("D0B91EGBA56", None)
    assert adapter.stopped and not adapter.sent


def test_restart_discards_unscoped_rows_without_outbound_guess(monkeypatch):
    values = {"active": [{"delegation_id": "old", "origin": {"scope_id": "W1", "chat_id": "D-other", "thread_id": "old-thread"}}]}
    instance = MODULE.DelegationStatus(Context(values))
    assert instance.ctx.state.values["active"] == []


def test_parent_dispatch_registers_generation_owned_post_delivery_reassert(monkeypatch):
    instance = service(monkeypatch); adapter = PostDeliveryAdapter(); calls = []
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    monkeypatch.setattr(instance, "_session_key", lambda: "gateway-session-key")
    instance._queue_route = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    dispatch(instance)
    assert calls == [((route(),), {"delay": 0.0})]
    instance.on_post_llm_call(session_id="parent", turn_id="turn")
    assert len(adapter.callbacks) == 1
    session_key, callback, generation = adapter.callbacks[0]
    assert session_key == "gateway-session-key" and generation == 7
    assert not adapter.typing
    asyncio.run(callback())
    assert adapter.typing and adapter.typing[-1][1]["thread_id"] == route()[2]


def test_post_delivery_callback_cannot_reassert_after_completion_clear(monkeypatch):
    instance = service(monkeypatch); adapter = PostDeliveryAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    monkeypatch.setattr(instance, "_session_key", lambda: "gateway-session-key")
    dispatch(instance)
    instance.on_post_llm_call(session_id="parent", turn_id="turn")
    callback = adapter.callbacks[0][1]
    instance.on_subagent_stop(child_session_id="child-deleg-1", child_status="completed")
    instance.on_pre_llm_call(session_id="parent", turn_id="final", user_message="[ASYNC DELEGATION COMPLETE — deleg-1]")
    instance.on_transform_llm_output(response_text="done", session_id="parent")
    asyncio.run(callback())
    assert not adapter.typing
    assert adapter.stopped and adapter.text[-1] == ("D0B91EGBA56", None)


def test_unrelated_turn_cannot_reassert_and_old_adapter_uses_delay(monkeypatch):
    instance = service(monkeypatch); calls = []
    instance._queue_route = lambda *args, **kwargs: calls.append((args, kwargs)) or True
    dispatch(instance)
    instance.on_post_llm_call(session_id="parent", turn_id="unrelated")
    assert calls == [((route(),), {"delay": 0.0})]
    instance.on_post_llm_call(session_id="parent", turn_id="turn")
    assert calls[-1] == ((route(),), {"delay": 2.0})


def test_multiple_active_lanes_use_counted_status(monkeypatch):
    instance = service(monkeypatch); adapter = StatusAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    dispatch(instance, tasks=[{"label": "one"}, {"label": "two"}])
    asyncio.run(instance._publish_route(route()))
    assert adapter.text[-1] == ("D0B91EGBA56", "2개 작업을 처리 중…")


def test_verifier_phase_takes_precedence_over_count(monkeypatch):
    instance = service(monkeypatch); adapter = StatusAdapter()
    monkeypatch.setattr(instance, "_adapter", lambda: adapter)
    monkeypatch.setattr(instance, "_delegation_phase", lambda *_: "verifier")
    dispatch(instance, tasks=[{"label": "verify-one"}, {"label": "verify-two"}])
    asyncio.run(instance._publish_route(route()))
    assert adapter.text[-1] == ("D0B91EGBA56", "결과를 검증 중…")


def test_partial_completion_and_progress_are_truthful(monkeypatch):
    instance = service(monkeypatch)
    dispatch(instance, tasks=[{}, {}, {}])
    rec = instance._active["deleg-1"]
    rec["lanes"][0].update(status="complete", started_at=time.time()-20, ended_at=time.time()-10)
    rec["lanes"][1]["status"] = "in_progress"
    rec["lanes"][2]["status"] = "in_progress"
    assert instance._text([rec]) == "1/3개 완료 · 2개 처리 중… · 33%"


def test_failed_lane_is_not_counted_as_completed(monkeypatch):
    instance = service(monkeypatch)
    dispatch(instance, tasks=[{}, {}])
    rec = instance._active["deleg-1"]
    rec["lanes"][0]["status"] = "error"
    rec["lanes"][1]["status"] = "in_progress"
    assert "1/2개 완료" not in instance._text([rec])


def test_stable_tool_families_and_registry_stall(monkeypatch):
    instance = service(monkeypatch)
    dispatch(instance)
    rec = instance._active["deleg-1"]
    rec["samples"] = ["web", "web"]
    assert instance._text([rec]) == "자료를 확인 중…"
    rec["samples"] = ["file", "file"]
    assert instance._text([rec]) == "파일을 확인 중…"
    rec["registry_status"] = "stalling"
    assert instance._text([rec]) == "작업 응답 지연을 처리 중…"


def test_registry_sampling_is_correlated_by_delegation_id(monkeypatch):
    import tools.async_delegation as async_delegation
    instance = service(monkeypatch)
    dispatch(instance)
    rec = instance._active["deleg-1"]
    monkeypatch.setattr(async_delegation, "list_async_delegations", lambda: [
        {"delegation_id": "other", "status": "stalling", "children_activity": [{"current_tool": "read_file"}]},
        {"delegation_id": "deleg-1", "status": "running", "children_activity": [{"current_tool": "web_search"}]},
    ])
    instance._sync_registry([rec]); instance._sync_registry([rec])
    assert rec["registry_status"] == "running"
    assert rec["samples"] == ["web", "web"]


def test_synthesis_then_writing_then_final_clear(monkeypatch):
    instance = service(monkeypatch); calls=[]
    instance._queue_route=lambda *args,**kwargs: calls.append((args,kwargs)) or True
    dispatch(instance)
    instance.on_subagent_stop(child_session_id="child-deleg-1", child_status="completed")
    rec=instance._active["deleg-1"]
    assert instance._text([rec]) == "결과를 정리 중…"
    instance.on_pre_llm_call(session_id="parent",turn_id="final",user_message="[ASYNC DELEGATION COMPLETE — deleg-1]")
    assert instance._text([rec]) == "답변을 작성 중…"
    instance.on_transform_llm_output(response_text="final",session_id="parent")
    assert not instance._active
    assert calls[-1][1] == {"delay":10.0,"force_clear":True}


def test_eta_requires_two_successful_measured_lanes(monkeypatch):
    instance = service(monkeypatch); now=time.time()
    dispatch(instance,tasks=[{}, {}, {}])
    rec=instance._active["deleg-1"]
    rec["lanes"][0].update(status="complete",started_at=now-30,ended_at=now-20)
    rec["lanes"][1].update(status="complete",started_at=now-25,ended_at=now-15)
    rec["lanes"][2].update(status="in_progress",started_at=now-5)
    text=instance._text([rec])
    assert "67%" in text and "예상 약" in text
    rec["lanes"][1]["status"]="error"
    assert "예상 약" not in instance._text([rec])


def test_internal_completion_event_does_not_clear(monkeypatch):
    instance=service(monkeypatch);dispatch(instance)
    Source=type("Source",(),{"platform":"slack","scope_id":"W1","chat_id":"D0B91EGBA56","thread_id":"1788759304.795359"})
    event=type("Event",(),{"source":Source(),"internal":True,"text":"[ASYNC DELEGATION COMPLETE]"})()
    instance.on_pre_gateway_dispatch(event)
    assert instance._active


def test_wildcard_thread_scope_covers_all_threads_in_exact_dm(monkeypatch):
    instance=service(monkeypatch)
    instance.ctx.settings["scope"]["thread_id"]="*"
    assert instance._in_scope({"scope_id":"W1","chat_id":"D0B91EGBA56","thread_id":"1788925525.277219"})
    assert instance._in_scope({"scope_id":"W1","chat_id":"D0B91EGBA56","thread_id":"another-thread"})
    assert not instance._in_scope({"scope_id":"W1","chat_id":"D-other","thread_id":"another-thread"})
    assert not instance._in_scope({"scope_id":"W1","chat_id":"D0B91EGBA56","thread_id":""})
