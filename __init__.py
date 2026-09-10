"""Truthful, exact-route Slack Assistant status for async delegation.

This observer never changes delegation or Slack result delivery.  It derives
only generic lifecycle, completion, and sampled tool-family signals; it never
stores or displays goals, labels, arguments, paths, errors, or raw tool names.
"""
from __future__ import annotations
import asyncio, json, logging, threading, time
from collections.abc import Mapping
from typing import Any

PLUGIN_ID = "slack-delegation-status"
PLUGIN_VERSION = "0.6.3"
_LOG = logging.getLogger(__name__)
_TERMINAL = {"complete", "error", "stopped", "unknown"}
_WEB = {"web_search", "web_extract", "browser_navigate", "browser_click", "browser_snapshot", "browser"}
_FILE = {"read_file", "search_files", "file_read", "file_search", "patch"}


def _s(v: Any, limit: int = 160) -> str: return str(v or "").strip()[:limit]
def _obj(v: Any) -> dict[str, Any]:
    if isinstance(v, Mapping): return dict(v)
    if isinstance(v, str):
        try: v = json.loads(v)
        except (TypeError, ValueError): return {}
    return dict(v) if isinstance(v, Mapping) else {}

class DelegationStatus:
    def __init__(self, ctx: Any) -> None:
        self.ctx=ctx; self._lock=threading.RLock()
        self._candidates: dict[tuple[str,str],dict[str,Any]]={}
        self._active: dict[str,dict[str,Any]]={}; self._child_to_lane: dict[str,tuple[str,str,int]]={}
        self._refresh_armed:set[tuple[str,str,str]]=set(); self._final_turns:set[tuple[str,str]]=set()
        self._load_recovered_rows()
    def _setting(self,k:str,d:Any)->Any:
        try:return self.ctx.get_config(k,d)
        except Exception:return d
    @staticmethod
    def _key(s:Any,t:Any)->tuple[str,str]: return (_s(s),_s(t))
    @staticmethod
    def _route(origin:Mapping[str,Any])->tuple[str,str,str]: return (_s(origin.get('scope_id')),_s(origin.get('chat_id')),_s(origin.get('thread_id')))
    def _scope(self)->dict[str,str]:
        raw=self._setting('scope',{}); raw=raw if isinstance(raw,Mapping) else {}
        return {'scope_id':_s(raw.get('team_id') or raw.get('scope_id')),'chat_id':_s(raw.get('chat_id')),'thread_id':_s(raw.get('thread_id'))}
    def _in_scope(self,o:Mapping[str,Any])->bool:
        s=self._scope()
        origin_thread=_s(o.get('thread_id'))
        thread_matches=s['thread_id']=='*' and bool(origin_thread) or origin_thread==s['thread_id']
        return bool(s['chat_id'] and s['thread_id'] and _s(o.get('chat_id'))==s['chat_id'] and thread_matches and (not s['scope_id'] or _s(o.get('scope_id'))==s['scope_id']))
    def _origin(self)->dict[str,str]|None:
        try:
            from gateway.session_context import get_session_env
            if _s(get_session_env('HERMES_SESSION_PLATFORM','')).lower()!='slack': return None
            o={'scope_id':_s(get_session_env('HERMES_SESSION_SCOPE_ID','')),'chat_id':_s(get_session_env('HERMES_SESSION_CHAT_ID','')),'thread_id':_s(get_session_env('HERMES_SESSION_THREAD_ID','')) or _s(get_session_env('HERMES_SESSION_MESSAGE_ID',''))}
            return o if self._in_scope(o) else None
        except Exception:return None
    def _seconds(self,key:str,default:float,lo:float,hi:float)->float:
        try:return max(lo,min(float(self._setting(key,default)),hi))
        except (TypeError,ValueError):return default
    def _refresh(self)->float:return self._seconds('refresh_seconds',90,30,110)
    def _max_age(self)->float:return self._seconds('max_age_seconds',1800,60,1800)
    def _stall(self)->float:return self._seconds('stall_seconds',300,60,900)
    def _throttle(self)->float:return self._seconds('change_throttle_seconds',15,3,60)
    def _sample(self)->float:return self._seconds('sample_seconds',30,15,60)
    def _phase(self,s:Any,t:Any)->str:
        try:
            from hermes_plugins import delegate_task_routing
            p=delegate_task_routing.delegation_phase_for_turn(s,t)
            return p if p in {'verifier','synthesis','writing'} else 'worker'
        except Exception:return 'worker'
    _delegation_phase = _phase
    def _records(self,r:tuple[str,str,str])->list[dict[str,Any]]: return [x for x in self._active.values() if self._route(x['origin'])==r]
    def _persist(self)->None:
        try:self.ctx.state.set('active',[{'delegation_id':d,'parent_session_id':r.get('parent_session_id',''),'parent_turn_id':r.get('parent_turn_id',''),'origin':r['origin'],'created_at':r['created_at'],'lanes':[{'id':x['id'],'status':x['status'],'phase':x.get('phase','worker')} for x in r['lanes']]} for d,r in self._active.items()])
        except Exception:_LOG.debug('status persistence failed',exc_info=True)
    def _load_recovered_rows(self)->None:
        try: rows=self.ctx.state.get('active',[])
        except Exception: rows=[]
        for row in rows if isinstance(rows,list) else []:
            if isinstance(row,Mapping) and isinstance(row.get('origin'),Mapping) and self._in_scope(row['origin']): self._queue_route(self._route(row['origin']),delay=0.0,force_clear=True)
        if rows:
            try:self.ctx.state.set('active',[])
            except Exception:pass
    def on_pre_gateway_dispatch(self,event:Any=None,**_:Any)->None:
        # Completion/process wakeups are internal lifecycle events, not a new
        # user request. Clearing here would erase synthesis/writing status.
        if bool(getattr(event,'internal',False)):return
        src=getattr(event,'source',None); platform=_s(getattr(src,'platform','')).lower().removesuffix('.slack')
        o={'scope_id':_s(getattr(src,'scope_id','')),'chat_id':_s(getattr(src,'chat_id','')),'thread_id':_s(getattr(src,'thread_id',''))}
        if platform=='slack' and self._in_scope(o):
            r=self._route(o)
            with self._lock:
                ids=[d for d,x in self._active.items() if self._route(x['origin'])==r]
                for d in ids:self._active.pop(d,None)
                if ids:self._persist()
            if ids:self._queue_route(r,delay=0.0,force_clear=True)
    def on_pre_tool_call(self,tool_name:str,args:Any=None,**kw:Any)->None:
        if tool_name=='delegate_task' and isinstance(args,Mapping) and _s(args.get('action')).lower() not in {'list','steer','stop'}:
            count=len(args.get('tasks')) if isinstance(args.get('tasks'),list) else int(bool(args.get('goal'))); o=self._origin()
            if o and count:
                key=self._key(kw.get('session_id'),kw.get('turn_id')); phase=self._delegation_phase(*key)
                with self._lock:self._candidates[key]={'parent_session_id':key[0],'parent_turn_id':key[1],'origin':o,'lanes':[{'id':f'lane-{i+1}','status':'pending','phase':phase,'started_at':0.0,'ended_at':0.0} for i in range(count)],'started':0,'created_at':time.time(),'last_progress':time.time(),'samples':[],'last_text':'','last_text_at':0.0,'presentation_phase':phase}
            return
        # Generic tool observations are deliberately normalized to two families.
        family='web' if tool_name in _WEB or tool_name.startswith(('web_','browser_')) else 'file' if tool_name in _FILE or tool_name.startswith(('read_','search_','file_')) else ''
        if not family:return
        key=self._key(kw.get('session_id'),kw.get('turn_id'))
        with self._lock:
            rec=self._find(*key)
            if rec:
                samples=rec.setdefault('samples',[]); samples.append(family); del samples[:-2]
                rec['last_progress']=time.time()
    def on_post_tool_call(self,tool_name:str,result:Any=None,status:str='',**kw:Any)->None:
        if tool_name!='delegate_task':return
        key=self._key(kw.get('session_id'),kw.get('turn_id'))
        with self._lock: rec=self._candidates.pop(key,None)
        data=_obj(result)
        if not rec or status not in {'','ok'} or data.get('mode')!='background' or not _s(data.get('delegation_id')):return
        did=_s(data['delegation_id'])
        with self._lock:self._active[did]=rec; rec['delegation_id']=did; self._persist()
        # Show progress while the parent prepares its short dispatch reply.  A
        # second publication is registered at the real post-delivery boundary
        # in on_post_llm_call because the gateway deliberately clears typing
        # when the parent turn ends and Slack also clears status on a reply.
        self._queue_route(self._route(rec['origin']),delay=0.0)
    def _find(self,s:Any,t:Any)->dict[str,Any]|None:
        k=self._key(s,t)
        return next((x for x in self._active.values() if self._key(x.get('parent_session_id'),x.get('parent_turn_id'))==k),self._candidates.get(k))
    def on_subagent_start(self,parent_session_id:Any=None,parent_turn_id:Any=None,child_session_id:Any=None,**_:Any)->None:
        with self._lock:
            rec=self._find(parent_session_id,parent_turn_id)
            if not rec:return
            i=int(rec['started']);rec['started']=i+1
            if i>=len(rec['lanes']):return
            rec['lanes'][i].update(status='in_progress',started_at=time.time());rec['last_progress']=time.time();cid=_s(child_session_id)
            if cid:self._child_to_lane[cid]=(_s(parent_session_id),_s(parent_turn_id),i)
    def on_subagent_stop(self,child_session_id:Any=None,child_status:Any=None,**_:Any)->None:
        with self._lock:
            m=self._child_to_lane.pop(_s(child_session_id),None)
            if not m:return
            rec=self._find(*m[:2])
            if not rec:return
            raw=_s(child_status).lower(); state='complete' if raw in {'completed','complete','succeeded','success'} else 'stopped' if raw in {'interrupted','stopped','cancelled','canceled'} else 'unknown' if raw=='unknown' else 'error'
            rec['lanes'][m[2]].update(status=state,ended_at=time.time());rec['last_progress']=time.time(); r=self._route(rec['origin'])
            terminal=all(x['status'] in _TERMINAL for x in rec['lanes'])
            if terminal:rec['presentation_phase']='synthesis'
            self._persist()
        # Keep the exact-route record until the completion turn ends. This
        # makes the real child-terminal -> synthesis -> writing phases visible.
        self._queue_route(r,delay=0.0,force_clear=False)
    def on_pre_llm_call(self,session_id:Any=None,turn_id:Any=None,user_message:Any='',**_:Any)->None:
        # Completion injections are a real synthesis signal; ordinary final sends are not.
        if '[ASYNC DELEGATION' not in _s(user_message,1000):return
        key=self._key(session_id,turn_id); self._final_turns.add(key)
        routes=set()
        with self._lock:
            for rec in self._active.values():
                if _s(rec.get('parent_session_id'))==key[0]:
                    rec['presentation_phase']='writing';rec['last_progress']=time.time();routes.add(self._route(rec['origin']))
        for route in routes:self._queue_route(route,delay=0.0)
    def on_post_llm_call(self,session_id:Any=None,turn_id:Any=None,**_:Any)->None:
        # The normal gateway lifecycle clears typing immediately after the
        # parent agent returns, before sending its final reply.  Register the
        # reassertion on the adapter's generation-owned post-delivery callback
        # so it cannot be cleared by that dispatch reply.  Completion turns do
        # not match the original parent turn and therefore cannot reassert.
        key=self._key(session_id,turn_id)
        with self._lock:
            routes={self._route(x['origin']) for x in self._active.values() if self._key(x.get('parent_session_id'),x.get('parent_turn_id'))==key}
        for route in routes:self._after_parent_delivery(route)
        return None
    def on_transform_llm_output(self,response_text:Any='',session_id:Any=None,**_:Any)->None:
        """Release finalizing rows at the actual turn-final output boundary."""
        sid=_s(session_id);keys={k for k in self._final_turns if k[0]==sid}
        if not keys:return None
        self._final_turns.difference_update(keys)
        with self._lock:
            routes={self._route(x['origin']) for x in self._active.values() if _s(x.get('parent_session_id'))==sid}
            for did in [d for d,x in self._active.items() if _s(x.get('parent_session_id'))==sid]:self._active.pop(did,None)
            self._persist()
        # The outgoing bot reply clears Slack automatically. This delayed clear
        # is only a bounded safety net if delivery fails; nothing can reassert.
        for route in routes:self._queue_route(route,delay=10.0,force_clear=True)
        return None
    def on_session_end(self,session_id:Any=None,turn_id:Any=None,completed:Any=False,**_:Any)->None:
        key=self._key(session_id,turn_id)
        if not completed or key not in self._final_turns:return
        self._final_turns.discard(key)
        with self._lock:
            routes={self._route(x['origin']) for x in self._active.values() if _s(x.get('parent_session_id'))==key[0]}
            for did in [d for d,x in self._active.items() if _s(x.get('parent_session_id'))==key[0]]:self._active.pop(did,None)
            self._persist()
        for r in routes:self._queue_route(r,delay=0.0,force_clear=True)
    def _text(self,records:list[dict[str,Any]])->str:
        lanes=[x for r in records for x in r['lanes'] if x['status'] not in _TERMINAL]; now=time.time()
        # Required precedence: stalled > verifier > final synthesis/writing > partial > stable tool > multi > single.
        if any(r.get('registry_status') in {'stalling','stalled'} for r in records):return '작업 응답 지연을 처리 중…'
        if any(x.get('phase')=='verifier' or r.get('presentation_phase')=='verifier' for r in records for x in lanes):return '결과를 검증 중…'
        phase=next((r.get('presentation_phase') for r in records if r.get('presentation_phase') in {'synthesis','writing'}),'')
        if phase=='writing':return '답변을 작성 중…'
        if phase=='synthesis':return '결과를 정리 중…'
        total=sum(len(r['lanes']) for r in records); done=sum(1 for r in records for x in r['lanes'] if x['status']=='complete'); active=len(lanes)
        suffix=self._progress_suffix(records,done,total,active)
        if done and active:return f'{done}/{total}개 완료 · {active}개 처리 중…{suffix}'
        stable=next((r['samples'][-1] for r in records if len(r.get('samples',[]))>=2 and r['samples'][-1]==r['samples'][-2]),'')
        if stable:return ('자료를 확인 중…' if stable=='web' else '파일을 확인 중…')+suffix
        if active>1:return f'{active}개 작업을 처리 중…{suffix}'
        return '비동기 위임 작업 중…'+suffix
    def _progress_suffix(self,records:list[dict[str,Any]],done:int,total:int,active:int)->str:
        if not total:return ''
        pct=round(done*100/total)
        # ETA is shown only after at least two completed lanes give a measured mean duration.
        durations=[x['ended_at']-x['started_at'] for r in records for x in r['lanes'] if x.get('status')=='complete' and x.get('started_at',0)>0 and x.get('ended_at',0)>x.get('started_at',0)]
        if active and len(durations)>=2:
            mean=sum(durations)/len(durations); now=time.time()
            remaining=[max(0.0,mean-(now-float(x.get('started_at') or now))) for r in records for x in r['lanes'] if x['status'] not in _TERMINAL]
            estimate=max(1,round(max(remaining or [mean]))); return f' · {pct}% · 예상 약 {estimate}초'
        return f' · {pct}%' if done else ''
    @staticmethod
    def _tool_family(tool:Any)->str:
        name=_s(tool).lower()
        if name in _WEB or name.startswith(('web_','browser_')):return 'web'
        if name in _FILE or name.startswith(('read_','search_','file_')):return 'file'
        return ''
    def _sync_registry(self,records:list[dict[str,Any]])->None:
        """Sample public async activity by exact delegation id, fail-open."""
        try:
            from tools.async_delegation import list_async_delegations
            live={_s(x.get('delegation_id')):x for x in list_async_delegations() if isinstance(x,Mapping)}
        except Exception:return
        with self._lock:
            for rec in records:
                item=live.get(_s(rec.get('delegation_id')))
                if not item:continue
                rec['registry_status']=_s(item.get('status')).lower()
                families={self._tool_family(x.get('current_tool')) for x in item.get('children_activity',[]) if isinstance(x,Mapping)}-{''}
                sample=next(iter(families)) if len(families)==1 else ''
                samples=rec.setdefault('samples',[]);samples.append(sample);del samples[:-2]
    @staticmethod
    def _adapter()->Any:
        try:
            from gateway.config import Platform
            from gateway.run import _gateway_runner_ref
            r=_gateway_runner_ref(); a=getattr(r,'adapters',{}).get(Platform.SLACK) if r else None
            return a if a and bool(a.is_connected) else None
        except Exception:return None
    @staticmethod
    def _metadata(r:tuple[str,str,str])->dict[str,str]:
        m={'thread_id':r[2],'thread_ts':r[2]}
        if r[0]:m['team_id']=r[0]
        return m
    @staticmethod
    def _session_key()->str:
        try:
            from gateway.session_context import get_session_env
            return _s(get_session_env('HERMES_SESSION_KEY',''),512)
        except Exception:return ''
    def _after_parent_delivery(self,r:tuple[str,str,str])->bool:
        """Publish after the gateway has delivered the parent dispatch reply."""
        a=self._adapter(); session_key=self._session_key()
        register=getattr(a,'register_post_delivery_callback',None) if a else None
        if callable(register) and session_key:
            try:
                active=getattr(a,'_active_sessions',{}).get(session_key)
                generation=getattr(active,'_hermes_run_generation',None) if active is not None else None
                async def publish()->None:await self._publish_route(r)
                register(session_key,publish,generation=generation)
                return True
            except Exception:
                _LOG.debug('post-delivery status registration failed; using delayed fallback',exc_info=True)
        # Compatibility fallback for adapters predating the post-delivery
        # callback contract.  It is deliberately secondary, not the primary
        # timing mechanism.
        return self._queue_route(r,delay=self._seconds('reassert_delay_seconds',2,.25,10))
    async def _checked_native_status(self,a:Any,r:tuple[str,str,str],text:str)->bool|None:
        """Use Slack's status endpoint directly when the adapter exposes it.

        SlackAdapter.send_typing deliberately absorbs API failures (including a
        missing ``assistant:write`` grant), which makes a method-exists check
        indistinguishable from a visible status.  A checked request lets this
        plugin take its documented one-shot fail-open fallback without exposing
        Slack error text.  Other adapters keep the normal generic path.
        """
        get_client=getattr(a,'_get_client',None)
        if not callable(get_client):return None
        try:
            client=get_client(r[1],team_id=r[0] or None)
            send_status=getattr(client,'assistant_threads_setStatus',None)
            if not callable(send_status):return None
            await send_status(channel_id=r[1],thread_ts=r[2],status=text)
            return True
        except Exception:
            _LOG.debug('native Slack Assistant status request failed; using bounded fallback',exc_info=True)
            return False
    async def _send_fallback_once(self,a:Any,r:tuple[str,str,str],rows:list[dict[str,Any]],text:str)->None:
        """Deliver at most one generic status card per active route/lifecycle."""
        if any(x.get('fallback_sent') for x in rows):return
        legacy=getattr(a,'send',None)
        if not callable(legacy):return
        try:
            await legacy(r[1], 'Delegation status — Working', reply_to=r[2], metadata=self._metadata(r))
        except Exception:
            return
        now=time.time()
        with self._lock:
            for x in rows:
                x['fallback_sent']=True;x['last_text']=text;x['last_text_at']=now
    # Compatibility spelling retained for older test/adapter integrations.
    def _queue_route(self,r:tuple[str,str,str],delay:float=0.0,force_clear:bool=False)->bool:
        try:
            from gateway.run import _gateway_runner_ref
            runner=_gateway_runner_ref(); loop=getattr(runner,'_gateway_loop',None) if runner else None
            if loop is None or loop.is_closed():raise RuntimeError
            if delay<=0:asyncio.run_coroutine_threadsafe(self._publish_route(r,force_clear),loop)
            else:loop.call_soon_threadsafe(lambda:loop.call_later(delay,lambda:asyncio.create_task(self._publish_route(r,force_clear))))
            return True
        except Exception:return False
    async def _publish_route(self,r:tuple[str,str,str],force_clear:bool=False)->None:
        clear = force_clear
        with self._lock:
            rows=self._records(r); expired=bool(rows) and min(x['created_at'] for x in rows)+self._max_age()<=time.time()
            if expired:
                for d,x in list(self._active.items()):
                    if self._route(x['origin'])==r:self._active.pop(d)
                self._persist();rows=[]
        a=self._adapter()
        if not a:return
        if clear or expired or not rows:
            set_text=getattr(a,'set_status_text',None)
            if callable(set_text):
                try:set_text(r[1],None)
                except Exception:pass
            stop=getattr(a,'stop_typing',None)
            if callable(stop):
                try:await stop(r[1],metadata=self._metadata(r))
                except Exception:pass
            return
        self._sync_registry(rows)
        text=self._text(rows)
        # Change throttling prevents sampled-family/status flicker; stalling/verification bypass it.
        emergency=text in {'작업 응답 지연을 처리 중…','결과를 검증 중…'}
        now=time.time()
        if not emergency and all(x.get('last_text') and x.get('last_text')!=text and now-x.get('last_text_at',0)<self._throttle() for x in rows):
            self._arm(r,rows);return
        if all(x.get('last_text')==text and now-x.get('last_text_at',0)<self._refresh() for x in rows):
            self._arm(r,rows);return
        set_text=getattr(a,'set_status_text',None); send=getattr(a,'send_typing',None)
        try:
            checked=await self._checked_native_status(a,r,text)
            if checked is None:
                # Non-Slack/generic adapter: retain its established status path.
                if not callable(set_text) or not callable(send):
                    await self._send_fallback_once(a,r,rows,text)
                else:
                    set_text(r[1],text)
                    await send(r[1],metadata=self._metadata(r))
            elif checked is False:
                # SlackAdapter normally swallows this failure, so make the
                # documented fallback real but bounded to one card per run.
                await self._send_fallback_once(a,r,rows,text)
            with self._lock:
                for x in rows:x['last_text']=text;x['last_text_at']=now
        except Exception:pass
        self._arm(r,rows)
    def _arm(self,r:tuple[str,str,str],rows:list[dict[str,Any]])->None:
        if r in self._refresh_armed:return
        try:
            from gateway.run import _gateway_runner_ref
            runner=_gateway_runner_ref();loop=getattr(runner,'_gateway_loop',None) if runner else None
            if not loop or loop.is_closed():return
            self._refresh_armed.add(r);delay=max(.1,min(self._sample(),min(x['created_at']+self._max_age()-time.time() for x in rows)))
            def tick():self._refresh_armed.discard(r);asyncio.create_task(self._publish_route(r))
            loop.call_later(delay,tick)
        except Exception:pass
_SERVICE=None
def register(ctx:Any)->None:
    global _SERVICE
    _SERVICE=DelegationStatus(ctx)
    for n,fn in [('pre_gateway_dispatch',_SERVICE.on_pre_gateway_dispatch),('pre_tool_call',_SERVICE.on_pre_tool_call),('post_tool_call',_SERVICE.on_post_tool_call),('pre_llm_call',_SERVICE.on_pre_llm_call),('post_llm_call',_SERVICE.on_post_llm_call),('transform_llm_output',_SERVICE.on_transform_llm_output),('on_session_end',_SERVICE.on_session_end),('subagent_start',_SERVICE.on_subagent_start),('subagent_stop',_SERVICE.on_subagent_stop)]:ctx.register_hook(n,fn)
    scope=_SERVICE._scope()
    _LOG.info('Enabled %s v%s scope_configured=%s',PLUGIN_ID,PLUGIN_VERSION,bool(scope['chat_id'] and scope['thread_id']))
