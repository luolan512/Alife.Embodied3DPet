"""Playback-aware plan scheduling, independent of provider and renderer."""
import json
import math
import threading
import time
from core import SYSTEM, completion, load_config
from persona import character_system
from body_loop import BodyLoop
from context import compact_state, dialogue_history
from plan_timeline import PlanTimeline

class AgentController:
    """Motor adapter owns ARDY; this class owns plans, epochs and conversation."""
    def __init__(self, motor, config_loader=load_config, *, start_threads=True, trace=None):
        self.motor = motor
        self.trace = trace
        self.perception = None
        self.motion_observer = None
        self.face_generator = None
        self.reply_followup = None
        self.config_loader = config_loader
        self.lock = threading.RLock()
        self.motor.control_lock = self.lock
        self.request_id = 0
        self.pending_request = False
        self.request_automatic = False
        self.autonomy_enabled = False
        self.autonomy_interval_s = 20
        self.autonomy_next = 0
        self.motion_id = 0
        self.closed = False
        self.phase = "未连接"
        self.current = None
        self.queue = []
        self.events = []
        self.history = []
        self.chat = []
        self.on_chat=None
        self.conversation_id=None
        self.auto_rounds = 0
        self.body_constraints = []
        self.plan_timeline=PlanTimeline()
        self.segment_start_frame = None
        self.segment_started_at = None
        self.segment_request_id = None
        self.request_started_at = None
        self.request_first_played = False
        self.body_loop = BodyLoop(self)
        self.worker = threading.Thread(target=self._loop, daemon=True)
        if start_threads:
            threading.Thread(target=self._body_loop, daemon=True).start()
            threading.Thread(target=self._publish_loop,daemon=True).start()
            self.worker.start()

    def snapshot(self):
        with self.lock:
            state=self.motor.state()
            return {"phase": self.phase, "current": self.current.model_dump() if self.current else None,
                    "scheduled_start_frame": self.segment_start_frame,
                    "next": [s.model_dump() for s in self.queue], "events": self.events[-12:],
                    "motion_id": self.motion_id, "request_id": self.request_id,
                    "pending_request":self.pending_request,
                    "body_loop": self.body_loop.snapshot(),
                    "autonomy": {"enabled":self.autonomy_enabled,"interval_s":self.autonomy_interval_s},
                    "active_look": self.perception.snapshot() if self.perception else None,
                    "motion_observer": self.motion_observer.snapshot() if self.motion_observer else None,
                    "face_state": self.face_generator.affect.snapshot() if self.face_generator and hasattr(self.face_generator,'affect') else None,
                    "conversation_context":{'conversation_id':self.conversation_id,'recent_messages':len(self.history),'recent_limit_messages':8,
                                             'display_messages':len(self.chat)},
                    **state,'plan_progress':self.plan_timeline.snapshot(self.current,state,self.segment_start_frame),
                    'behavior_state':{**state.get('behavior_state',{}),'constraints':list(self.body_constraints)}}

    def _event(self, kind, detail):
        if self.trace:self.trace.record(kind, {'detail': detail, 'request_id': self.request_id, 'motion_id': self.motion_id})
        self.events.append({"event": kind, "detail": detail})
        self.events = self.events[-30:]

    def mark_current(self,status,outcome):
        if not self.current:return
        state=self.motor.state();start=self.segment_start_frame
        elapsed=0 if start is None else max(0,state['frame']-start)/state.get('fps',20)
        self.plan_timeline.mark(self.current,status,outcome=outcome,end_frame=state['frame'],elapsed_s=round(elapsed,1),
                                continuation='cached_idle' if state.get('behavior_state',{}).get('enabled') else 'ardy')

    def auto_plan_steps(self,steps):
        """Remove an echoed prefix without replacing its live step objects."""
        def key(step):
            return (step.kind,' '.join(step.motion_prompt.lower().split()),step.repeat,
                    step.pose_intent,step.after_idle,step.target,step.speed)
        queued=[s for s in self.queue if s.action not in ('terminal-hold','terminal-idle')]
        scheduled=([self.current] if self.current and self.current.action not in ('terminal-hold','terminal-idle') else [])+queued
        incoming=list(steps)
        # Models may echo either current+queue, or just the waiting queue. Only
        # exact motion prefixes are reused; explicit append can still repeat tasks.
        consumed=0
        for existing in (scheduled,queued):
            count=0
            for old,new in zip(existing,incoming):
                if key(old)!=key(new):break
                count+=1
            consumed=max(consumed,count)
        return incoming[consumed:]

    def warm_next_motion(self):
        # The current step's foreground preparation owns the first cold encode.
        # Only after its first segment is committed may we warm one queued action.
        if self.current is None or self.segment_start_frame is None:return
        if self.queue and self.queue[0].action not in ('terminal-hold','terminal-idle'):
            if hasattr(self.motor,'prefetch_prompts'):self.motor.prefetch_prompts(self.queue[:1])

    def configure_autonomy(self,enabled):
        if type(enabled) is not bool:raise ValueError('enabled must be boolean')
        with self.lock:
            self.autonomy_enabled=enabled;self.autonomy_next=time.monotonic()
            if enabled and hasattr(self.motor,'note_interaction'):self.motor.note_interaction()
            if not enabled and self.pending_request and self.request_automatic:
                self.request_id+=1;self.pending_request=False
            return {"enabled":enabled,"interval_s":self.autonomy_interval_s}

    def tick_autonomy(self):
        with self.lock:
            if (not self.autonomy_enabled or self.closed or self.pending_request or (self.current and getattr(self.current,'kind','one_shot')!='loop') or self.queue
                or time.monotonic()<self.autonomy_next or not getattr(self.motor,'owner',False)):
                return
            state=self.motor.state()
            if not state.get('backend_ready'):return
            behavior=state.get('behavior_state',{})
            if behavior.get('configured_mode')=='hybrid' and behavior.get('effective_mode')=='event':return
            interval=120 if behavior.get('effective_mode')=='event' else self.autonomy_interval_s
            self.autonomy_next=time.monotonic()+interval
            self.submit('根据最近对话和当前姿态，自主规划下一段自然的桌宠动作。可以保持当前动作；需要变化时明确过渡、部位、方向和节奏，不虚构已经完成。',automatic=True)

    def add_chat(self,role,content):
        """Persist immediately; the visible in-memory chat is a bounded window."""
        self.chat.append((role,content))
        self.chat=self.chat[-100:]
        if self.on_chat:self.on_chat(role,content)

    def remember(self,messages):
        self.history=[*self.history,*messages][-8:]

    def reset_conversation(self):
        with self.lock:
            self.cancel()  # Old network replies must not cross into the new conversation.
            self.chat=[];self.history=[]
            self.body_constraints=[]
            saver=self.plan_timeline.on_save;self.plan_timeline=PlanTimeline();self.plan_timeline.on_save=saver
            loop=self.body_loop
            loop.history=[];loop.observations=[];loop.previous=None
            loop.last_action_feedback=None;loop.last_decision=None
            loop.sequence=0;loop.decisions=0
            self.phase='新对话已开始；身体暂停，发送消息或手动恢复后继续'

    def submit(self, text, *, automatic=False, probe=False, face_dialogue=False):
        text = text.strip()
        if not text or len(text) > 4000:
            raise ValueError("消息需要1到4000个字符")
        # Validate connection before changing any motion.
        cfg = self.config_loader()
        if not cfg.get("base_url") or not cfg.get("model"):
            raise ValueError("接口未配置：先保存 Base URL / Model，并在本地配置文件填写密钥")
        with self.lock:
            if self.perception:self.perception.cancel('new_user_request')
            if not automatic and not probe and hasattr(self.motor,'note_interaction'):self.motor.note_interaction()
            self.body_loop.invalidate()
            if not probe:
                self.body_loop.suspended = False
            self.request_id += 1
            self.reply_followup=None
            if self.face_generator and hasattr(self.face_generator,'stop_speech'):self.face_generator.stop_speech()
            self.pending_request = True
            self.request_automatic = automatic
            rid = self.request_id
            self.request_started_at=time.monotonic()
            self.request_first_played=False
            # A dialogue request does not invalidate body generation or its timing.
            self.motor.record(stage='llm_request',request_id=rid,automatic=automatic,
                              llm_http_ms=None,llm_plan_ms=None,validation_error=None)
            if not automatic and not probe:
                self.auto_rounds = 0
                self.add_chat("你", text)
                self.remember([{'role':'user','content':text}])
            self.phase = "连接测试中" if probe else "AI 思考中（已有动作继续播放）"
            context = compact_state(self.snapshot())
            for key in ('debug','debug_events','timeline','motion_parameters'):context.pop(key,None)
            user = {"role": "user", "content": json.dumps({"message": text, "state": context}, ensure_ascii=False)}
            prior=self.history if automatic or probe else self.history[:-1]
            messages = [{"role": "system", "content": character_system(SYSTEM)}, *dialogue_history(prior), user]
            if face_dialogue:
                messages[0]['content']+='\n控制范围约束：ARDY不能控制独立手指或脸部。纯面部要求必须steps=[]、after_reply=[]、mode="keep"；张嘴/闭嘴必须带face_action={"mouth_open":0到1,"hold_s":0到120}；闭眼/睁眼必须带face_action={"eyes_closed":1闭眼或0睁眼,"hold_s":0到120}，不得用身体步骤实现。下面的小动作建议仅在用户没有要求只做面部动作时适用。'
                messages[0]['content']+='\n当前是中文表情对话验证模式。reply简短自然中文，必须带英文face_text和face_state。steps可按交流情绪主动安排说话期间的小动作，无需用户逐一点名；当前动作合适时可复用，steps为空并立即回答，不必等新动作生成。回复结束后才做的点头、调整姿态和等待必须放after_reply，不能放steps，也不要用占位站立步骤计时：服务按回复时长延后执行after_reply。after_reply每步必须有英文prompt和duration_s，可用action="nod"；用户要求不动时允许为空。'
        threading.Thread(target=self._request, args=(cfg, messages, user, rid, probe), daemon=True).start()

    def _request(self, cfg, messages, user, rid, probe):
        try:
            started=time.monotonic()
            def debug(**data):
                look_round=data.pop('look_round',0)
                if self.trace:self.trace.llm('dialogue', str(rid)+('/look-'+str(look_round) if look_round else ''), **data)
                if rid==self.request_id and data.get('stage') not in ('llm_payload', 'llm_output'):
                    self.motor.record(**data)
            valid=lambda:not self.closed and rid==self.request_id
            plan = self.perception.decide(cfg,messages,completion,valid,debug) if self.perception and not probe else completion(cfg, messages,on_debug=debug)
            if plan.inspect_motion and not probe:
                with self.lock:
                    if not valid():return
                    inspection=compact_state(self.snapshot())
                    self._event('motion_inspected',{'playing':inspection.get('playing'),'current':inspection.get('current'),'next':inspection.get('next')})
                # One bounded read-only round; it never calls the motor or edits the queue.
                messages=[*messages,{'role':'assistant','content':plan.model_dump_json(exclude_none=True)},
                          {'role':'user','content':json.dumps({'motion_inspection':inspection,'instruction':'这是最新只读状态，原计划未改变。返回最终 JSON，inspect_motion=false。检查不要求mode=observe；可mode=auto复用当前动作立即交流，或按意图安排新动作。仅用户不希望身体动作时用observe。'},ensure_ascii=False)}]
                plan=completion(cfg,messages,on_debug=debug)
                if plan.inspect_motion:raise ValueError('Only one motion inspection is allowed per reply')
            with self.lock:
                if self.closed or rid != self.request_id:
                    if self.trace:self.trace.record('plan_discarded', {'role': 'dialogue', 'request_id': rid, 'plan': plan.model_dump()})
                    return
                if self.trace:self.trace.record('plan_accepted', {'role': 'dialogue', 'request_id': rid, 'plan': plan.model_dump()})
                self.pending_request = False
                self.motor.record(stage='plan_ready',llm_plan_ms=round((time.monotonic()-started)*1000,2))
                if probe:
                    self.phase = "接口连接成功（测试回复未执行动作）"
                    return
                if plan.mode=='auto' and plan.steps:
                    active=(self.current is not None and self.current.action not in ('terminal-hold','terminal-idle'))
                    active=active or any(s.action not in ('terminal-hold','terminal-idle') for s in self.queue)
                    if active:
                        incoming=self.auto_plan_steps(plan.steps)
                        plan=plan.model_copy(update={'mode':'append' if incoming else 'keep','steps':incoming})
                        self._event('chat_motion_preserved',{'added_actions':[s.action for s in incoming],
                                                           'current_preparing':self.current is not None and self.segment_start_frame is None})
                remembered=[{"role": "assistant", "content": plan.reply}]
                self.remember(remembered)
                self.add_chat("AI", plan.reply)
                speech_s=min(8,max(1.2,len(plan.reply)/8.0))
                if self.face_generator:
                    if plan.face_action is not None:
                        self.face_generator.set_action(plan.face_action.model_dump())
                    text=plan.face_text or (plan.reply if plan.reply.isascii() else None)
                    if not text:
                        from core import Step
                        text={'happy':'I feel happy and pleased.','sad':'I feel sad and disappointed.','angry':'I feel upset and annoyed.','relaxed':'I feel calm and comfortable.','neutral':'I am listening attentively.'}[plan.emotion]
                        self._event('face_description_fallback','AI omitted English face_text')
                    try:self.face_generator.request(text[:300],plan.emotion,speech_s,state=plan.face_state.model_dump(exclude_unset=True) if plan.face_state else None)
                    except ValueError as exc:self._event('face_request_invalid',str(exc))
                    if plan.mode not in ('clear','observe') and plan.after_reply:
                        from core import Step
                        followups=list(plan.after_reply)
                        self.reply_followup={'request_id':rid,'deadline':time.monotonic()+speech_s+.6,'steps':followups}
                if plan.body_constraints is not None:self.body_constraints=list(plan.body_constraints)
                if plan.mode!='observe' and plan.steps and hasattr(self.motor,'transport_paused'):self.motor.transport_paused=False
                if plan.gaze is None:self.motor.expression(plan.emotion)
                else:self.motor.expression(plan.emotion,plan.gaze,plan.gaze_target)
                if plan.mode == "replace" or (plan.mode == "auto" and plan.steps):
                    if hasattr(self.motor,'cancel_text_prefetch'):self.motor.cancel_text_prefetch()
                    self.motor.record(first_motion_encode_ms=None,first_motion_cache_hit=None,
                                      request_to_first_motion_ms=None,kimodo_encode_ms=None)
                    if self.current:
                        self.mark_current('interrupted','plan_replaced')
                        self.body_loop.terminal(self.current.action, 'INTERRUPTED', self.motor.state()['frame'],
                                                self.segment_start_frame, 'character_plan_replaced')
                    self.motion_id += 1
                    if not plan.steps:
                        self.motor.invalidate()
                        self.motor.halt()
                    self.current = None
                    self.segment_start_frame = None
                    self.queue = self.with_terminal_loop(list(plan.steps)) if plan.steps else []
                    self._event("plan_replaced", [s.action for s in self.queue])
                elif plan.mode == "append":
                    if len(self.queue) + len(plan.steps) > 16:
                        raise ValueError("动作队列已满")
                    if plan.steps and self.queue and self.queue[-1].action in ('terminal-hold','terminal-idle'):self.queue.pop()
                    self.queue=self.with_terminal_loop([*self.queue,*plan.steps]) if plan.steps else self.queue
                elif plan.mode in ('revise','clear'):
                    self.queue=self.with_terminal_loop(list(plan.steps)) if plan.mode=='revise' and plan.steps else []
                    self._event('queue_revised',[s.action for s in self.queue])
                elif plan.mode=='insert':
                    if len(self.queue)+len(plan.steps)>16:raise ValueError('Plan queue exceeds 16 steps')
                    incoming=list(plan.steps)
                    if self.queue and incoming and incoming[-1].kind=='loop':incoming.pop()
                    self.queue=self.with_terminal_loop([*incoming,*self.queue])
                unknown=[s for s in self.queue if id(s) not in self.plan_timeline.records]
                if plan.mode!='observe':
                    if unknown:self.plan_timeline.bind(unknown,plan.goal or plan.reply,source='autonomous' if self.request_automatic else 'user')
                    self.plan_timeline.reconcile(self.current,self.queue)
                if plan.mode!='observe' and plan.steps:self.warm_next_motion()
                self.phase = "准备动作" if self.queue else ("执行中" if self.current else "等待交互")
                # auto without steps leaves streaming generation untouched.
                # Legacy keep is still accepted but no longer requested in prompts.
        except Exception as exc:
            with self.lock:
                if rid == self.request_id and not self.closed:
                    if self.trace:self.trace.record('llm_failed', {'role': 'dialogue', 'request_id': rid, 'error': type(exc).__name__})
                    self.pending_request = False
                    # Never display response text or secrets from exception reprs.
                    if isinstance(exc, (ValueError, RuntimeError)) and not hasattr(exc, "errors"):
                        self.phase = str(exc)[:240]
                    else:
                        self.phase = f"接口/计划失败（{type(exc).__name__}），已有计划保持不变"
                    self._event("llm_error", type(exc).__name__)
                    self.motor.record(stage='llm_error',error=self.phase)

    def cancel(self, *, preserve_motion=False):
        with self.lock:
            if self.face_generator and hasattr(self.face_generator,'stop_speech'):self.face_generator.stop_speech()
            self.reply_followup=None
            self.autonomy_enabled=False
            if not preserve_motion and hasattr(self.motor,'policy_enabled'):self.motor.policy_enabled=False
            if self.perception:self.perception.cancel('character_cancelled')
            if self.current:
                self.mark_current('cancelled','stopped')
                self.body_loop.terminal(self.current.action, "CANCELLED", self.motor.state()["frame"], self.segment_start_frame, "user_or_runtime_cancel")
            self.body_loop.invalidate(suspend=True)
            self.pending_request = False
            self.request_id += 1
            self.motion_id += 1
            self.queue.clear()
            self.current = None
            self.plan_timeline.reconcile(self.current,self.queue)
            if not preserve_motion:
                self.motor.invalidate()
                self.motor.halt()
                if hasattr(self.motor,'transport_paused'):self.motor.transport_paused=True
            self.phase = "已取消，当前姿势暂停"
            self._event("cancelled", "用户取消")

    def close(self):
        self.cancel()
        self.closed = True

    def _body_loop(self):
        while not self.closed:
            try:
                self.body_loop.tick()
            except Exception as exc:
                with self.lock:
                    self.body_loop.phase = "feedback_error:" + type(exc).__name__
            time.sleep(.2)

    def _loop(self):
        while not self.closed:
            try:
                self.tick_policy()
                self.tick_reply_followup()
                self.tick()
                self.tick_autonomy()
            except Exception as exc:
                import traceback
                traceback.print_tb(exc.__traceback__)
                with self.lock:
                    if not self.closed:
                        if self.current:
                            self.mark_current('error',type(exc).__name__)
                            self.body_loop.terminal(self.current.action, 'ERROR', self.motor.state()['frame'],
                                                    self.segment_start_frame, type(exc).__name__)
                        self.phase = f"执行失败（{type(exc).__name__}）"
                        self.queue.clear()
                        self.current = None
                        self.plan_timeline.reconcile(self.current,self.queue)
                        if not getattr(self.motor,'params',{}).get('event_mode',False) or getattr(self.motor,'idle_clip',None) is None:
                            self.motor.halt()
                        self._event("execution_error", type(exc).__name__)
            time.sleep(0.15)

    def _publish_loop(self):
        while not self.closed:
            try:self.motor.publish(self.snapshot(),self.chat[-20:])
            except Exception:pass
            time.sleep(.15)

    def tick_reply_followup(self):
        with self.lock:
            item=self.reply_followup
            if not item or self.closed or item['request_id']!=self.request_id:return
            if self.pending_request or time.monotonic()<max(item['deadline'],getattr(self.face_generator,'speaking_until',0)+.25):return
            if not getattr(self.motor,'owner',False) or not self.motor.state().get('backend_ready'):return
            passive=('terminal-idle','terminal-hold','stand','sit','kneel','lie','idle','look_user','idle_stand','idle_sit','idle_kneel','idle_lie')
            if self.current and not (self.current.kind=='loop' and self.current.action in passive):return
            if self.current and (self.segment_start_frame is None or self.motor.state()['frame']<self.segment_start_frame):return
            if any(s.action not in ('terminal-idle','terminal-hold') for s in self.queue):return
            if self.current:self.mark_current('completed','reply_followup');self.current=None;self.segment_start_frame=None
            self.motion_id+=1;self.install_plan(item['steps'],'说完后的反应',source='reply-followup')
            self.reply_followup=None;self.phase='准备说完后的反应';self._event('reply_followup_started',[s.action for s in item['steps']])

    def tick_policy(self):
        """No network calls: activity only changes how the motor generates/replays."""
        from core import Step
        with self.lock:
            m=self.motor
            if (not getattr(m,'policy_enabled',False) or not getattr(m,'owner',False)
                or not getattr(m,'ready',False) or getattr(m,'transport_paused',False)
                or (self.current and self.current.kind!='loop') or self.queue or self.pending_request):return
            finite=m.event_mode_active()
            if finite and getattr(m,'features',None) is not None:
                if getattr(m,'continuous_live',False) and m.params.get('behavior_mode')=='event':return
                posture=getattr(m,'anchor_kind','stand')
                from event_motion import IDLES
                if self.current:self.mark_current('interrupted','inactive_rest');self.current=None;self.segment_start_frame=None
                self.install_plan([Step(action='policy-rest',label='休息循环',kind='loop',prompt=IDLES[posture],duration_s=2,after_idle=posture)],'无交互，进入稳定循环',source='system')
                self.phase='活跃期结束，准备稳定待机'
            elif not finite and getattr(m,'features',None) is None:
                from event_motion import IDLES
                posture=getattr(m,'anchor_kind','stand')
                prompt=self.current.motion_prompt if self.current else IDLES[posture]
                if self.current:self.mark_current('interrupted','active_wake');self.current=None;self.segment_start_frame=None
                self.install_plan([Step(action='policy-active',label='活跃循环',kind='loop',prompt=prompt,duration_s=3)],'恢复活跃续帧',source='system')
                self.phase='恢复活跃续生成'

    def with_terminal_loop(self,steps):
        if not getattr(self.motor,'params',{}).get('allow_cached_idle_loops',False):
            steps=[s.model_copy(update={'action':s.action[5:],'label':s.label+'（生成循环）'}) if s.action in ('idle_stand','idle_sit','idle_kneel','idle_lie') else s for s in steps]
        if not steps or steps[-1].kind=='loop':return steps
        if len(steps)>=16:raise ValueError('Reserve one of the 16 queue slots for the terminal Loop')
        from core import Step,MOTIONS
        if getattr(self.motor,'params',{}).get('behavior_mode')=='event':
            return [*steps,Step(action='terminal-hold',label='原姿态保持',kind='loop',
                prompt='A person stops moving and holds their current pose, remaining still.',
                pose_intent='static',duration_s=2,after_idle='hold',emotion=steps[-1].emotion)]
        last=steps[-1];posture=last.after_idle
        if posture=='keep':posture=last.action if last.action in ('stand','sit','kneel','lie') else getattr(self.motor,'idle_kind','stand')
        if posture=='hold':prompt='A person holds the same pose, remaining still.'
        else:prompt=MOTIONS.get(posture,MOTIONS['stand'])
        return [*steps,Step(action='terminal-idle',label='持续姿态循环',kind='loop',prompt=prompt,duration_s=4,after_idle=posture,emotion=last.emotion)]

    def install_plan(self,steps,goal,source='manual'):
        self.queue=self.with_terminal_loop(list(steps));self.plan_timeline.bind(self.queue,goal,source)
        self.plan_timeline.reconcile(self.current,self.queue)
        self.warm_next_motion()

    def tick(self):
        with self.lock:
            if self.closed:
                return
            if self.current is None:
                if not self.queue:
                    return
                self.queue=self.with_terminal_loop(self.queue)
                unknown=[s for s in self.queue if id(s) not in self.plan_timeline.records]
                if unknown:self.plan_timeline.bind(unknown,'动作计划',source='system')
                self.current = self.queue.pop(0)
                self.plan_timeline.mark(self.current,'preparing')
                self.segment_start_frame = None
                self.segment_started_at = time.monotonic()
                self.segment_request_id = self.request_id
                step = self.current
                mid = self.motion_id
                self.phase = f"准备 {step.action}（文字缓存/运动生成）"
            else:
                step = None
        if step is not None:
            # Slow text encoding and CUDA work must not hold the controller lock.
            try:
                start = self.motor.begin(step, lambda: not self.closed and mid == self.motion_id)
            except Exception:
                with self.lock:
                    if mid != self.motion_id or self.closed:
                        return
                raise
            with self.lock:
                if mid != self.motion_id or self.closed:
                    return
                self.segment_start_frame = start
                self.segment_started_at = time.monotonic()
                self.phase = "执行中"
                self.plan_timeline.mark(self.current,'looping' if self.current.kind=='loop' else 'running',start_frame=start)
                self._event("step_started", step.action)
                self.warm_next_motion()
                if self.queue and hasattr(self.motor,'prepare_next_step'):
                    target=self.queue[0]
                    self.motor.prepare_next_step(target,lambda:not self.closed and mid==self.motion_id and (self.current is target or any(s is target for s in tuple(self.queue))))
            return
        with self.lock:
            if self.segment_start_frame is None or self.current is None:
                return
            if self.queue and hasattr(self.motor,'buffer_queued_transition'):
                self.motor.buffer_queued_transition(self.queue[0])
            state = self.motor.state()
            if not state['playing']:
                self.segment_started_at=time.monotonic()
                return
            if state['frame'] < self.segment_start_frame:
                self.phase = f'预演完成，等待第 {self.segment_start_frame} 帧接入'
                return
            self.phase = '执行中'
            if (not self.pending_request and self.segment_request_id == self.request_id
                    and not self.request_first_played and self.request_started_at is not None):
                self.request_first_played=True
                self.motor.record(stage='playing',request_to_first_motion_ms=round((time.monotonic()-self.request_started_at)*1000,2))
            played = max(0, state["frame"] - self.segment_start_frame) / state["fps"]
            behavior=state.get('behavior_state',{})
            finite=behavior.get('enabled',False)
            if self.current.kind=='loop':
                if not self.queue:
                    self.phase='循环待机 · 缓存复用' if finite and state['frame']>=behavior.get('event_end_frame',state['frame']+1) else '循环动作 · ARDY续帧'
                    return
                if played<self.current.duration_s:return
                if hasattr(self.motor,'finish_event'):
                    self.motor.finish_event(next_step=self.queue[0])
                self.plan_timeline.mark(self.current,'completed',outcome='loop_replaced_by_next',end_frame=state['frame'],elapsed_s=round(played,1))
                self.current=None;self.segment_start_frame=None;return
            outcome = None
            if self.current.action == "move_to":
                dx = self.current.target[0] - state["position"][0]
                dz = self.current.target[1] - state["position"][2]
                distance = math.hypot(dx, dz)
                if distance <= 0.25:
                    outcome = "target_reached"
                elif played >= self.current.duration_s:
                    outcome = "target_timeout"
                else:
                    self.motor.velocity(dx / distance * self.current.speed, dz / distance * self.current.speed)
            elif (finite or behavior.get('configured_mode')=='event') and behavior.get('event_end_frame') is not None and state['frame']>=behavior['event_end_frame']:
                outcome = "playback_duration_finished_pose_unverified"
            elif not finite and not (behavior.get('configured_mode')=='event' and behavior.get('event_end_frame') is not None) and played >= self.current.duration_s*self.current.repeat:
                outcome = "playback_duration_finished_pose_unverified"
            elif (not state.get('motion_parameters', {}).get('auto_replan', True)
                  and state.get('motion_end_frame', -1) >= self.segment_start_frame
                  and state['frame'] >= state['motion_end_frame']):
                outcome = "playback_buffer_finished_pose_unverified"
            if time.monotonic() - self.segment_started_at > self.current.duration_s * getattr(self.current,'repeat',1) * 3 + 30:
                outcome = "playback_stalled"
            if outcome:
                if outcome!='playback_stalled' and hasattr(self.motor,'finish_event'):
                    self.motor.finish_event(next_step=self.queue[0] if self.queue else None)
                finished=self.current
                self.plan_timeline.mark(self.current,'error' if outcome=='playback_stalled' else 'completed',outcome=outcome,end_frame=state['frame'],elapsed_s=round(played,1))
                self.body_loop.terminal(self.current.action, "ERROR" if outcome == "playback_stalled" else "COMPLETED", state["frame"], self.segment_start_frame, outcome)
                self._event(outcome, self.current.action)
                self.current = None
                self.segment_start_frame = None
                if not self.queue:
                    if outcome!='playback_stalled':
                        self.install_plan([self.with_terminal_loop([finished])[-1]],'任务后的循环',source='system')
                        self.phase='准备任务结束后的循环';return
                    if finite:
                        self.phase='动作事件结束，播放缓存待机'
                        return
                    self.phase = ("计划窗口结束，沿用当前动作自动续帧" if state.get('motion_parameters', {}).get('auto_replan', True) else "已播放完生成帧，自动续生成关闭")
                    # Duration closes the scheduling window; it is not permission
                    # to override the character's condition with a standing prompt.
