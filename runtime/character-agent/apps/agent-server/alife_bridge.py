"""Alife owns dialogue; this adapter accepts explicit body jobs without an LLM."""
import collections, math, threading, time, uuid
from core import Step, MOTIONS

class AlifeBridge:
    def __init__(self, controller, face, emit):
        self.c=controller; self.face=face; self.emit=emit
        self.jobs={}; self.deferred=[]; self.events=collections.deque(maxlen=512)
        self.seq=0; self.event_lock=threading.RLock(); self.results={}; self.result_lock=threading.RLock()
        self.speaking=False; self.dialogue=[]
        controller.body_loop.configure(False)

    def event(self, kind, text):
        with self.event_lock:
            self.seq+=1
            self.events.append(dict(id=self.seq,type=kind,text=str(text)[:2000]))
            return dict(accepted=True,id=self.seq)

    def _steps(self,data):
        raw=data.get('steps')
        if raw is None:
            raw=[dict(action=data.get('action','motion'),prompt=data.get('prompt'),
                      duration_s=data.get('duration_s',3),repeat=data.get('repeat',1),
                      after_idle=data.get('after_idle','keep'),kind=data.get('kind','one_shot'),pose_intent=data.get('pose_intent','auto'),continuous=data.get('continuous',False))]
        if not isinstance(raw,list) or not 1<=len(raw)<=15:raise ValueError('steps needs 1..15 items')
        steps=[Step.model_validate(s) for s in raw]
        # Reject unknown named actions without a description before touching the queue.
        for s in steps:
            if not s.prompt and s.action not in MOTIONS:raise ValueError('Unknown action needs an English prompt')
            if s.prompt and any('\u3400'<=ch<='\u9fff' for ch in s.prompt):raise ValueError('Body prompt must be English')
        if any(s.kind=='loop' for s in steps[:-1]):raise ValueError('Only the final step may loop')
        last=steps[-1]
        static_actions={'stand','sit','kneel','lie','idle','look_user','cross_arms','angry','idle_stand','idle_sit','idle_kneel','idle_lie'}
        static_test=getattr(self.c.motor,'static_intent',None)
        if last.kind!='loop' and not last.continuous and last.after_idle!='hold' and last.pose_intent!='static' and last.action not in static_actions and not (static_test and static_test(last)):
            posture=last.after_idle if last.after_idle!='keep' else getattr(self.c.motor,'idle_kind','stand')
            if posture not in ('stand','sit','kneel','lie'):posture='stand'
            prompts={'stand':'A person lowers both arms to their sides and returns to a relaxed upright standing posture, facing forward.',
                     'sit':'A person lowers both arms and settles into a relaxed seated posture.',
                     'kneel':'A person lowers both arms and settles into a relaxed upright kneeling posture.',
                     'lie':'A person relaxes both arms and settles into a comfortable lying posture.'}
            names={'stand':'自然站姿','sit':'自然坐姿','kneel':'自然跪姿','lie':'休息躺姿'}
            if len(steps)>=15:raise ValueError('One-shot plan needs a reserved slot for returning to a natural posture')
            steps.append(Step(action=posture,prompt=prompts[posture],label='收尾归正：'+names[posture],duration_s=3,after_idle=posture,pose_intent='static',emotion=last.emotion))
        return steps

    def act(self,data):
        steps=self._steps(data); mode=data.get('mode','append'); timing=data.get('timing','now')
        if mode not in ('append','replace'):raise ValueError('mode must be append or replace')
        if timing not in ('now','after_speech'):raise ValueError('timing must be now or after_speech')
        c=self.c
        with c.lock:
            if not c.motor.owner or not c.motor.ready:raise ValueError('3D runtime is not ready')
            queued=sum(len(j['steps']) for j in self.deferred)+len(c.queue)
            if mode=='append' and queued+len(steps)>16:raise ValueError('Body queue limit is 16')
            if mode=='replace':
                for j in self.deferred:j['cancelled']=True
                self.deferred.clear()
            job=dict(id=uuid.uuid4().hex,steps=steps,mode=mode,timing=timing,
                     goal=str(data.get('goal','Alife body task'))[:160],status='waiting_for_speech' if timing=='after_speech' else 'queued',cancelled=False)
            self.jobs[job['id']]=job
            while len(self.jobs)>64:
                finished=next((k for k,j in self.jobs.items() if self._status(j) in ('completed','cancelled','interrupted','error')),None)
                if not finished:break
                self.jobs.pop(finished)
            if timing=='after_speech':self.deferred.append(job)
            else:self._install(job)
            return dict(accepted=True,task_id=job['id'],status=job['status'],recovery_planned=steps[-1].label.startswith('收尾归正：'))

    def _install(self,job):
        c=self.c
        if job['mode']=='replace':
            c.mark_current('interrupted','alife_replaced')
            c.motion_id+=1; c.current=None; c.segment_start_frame=None
            c.queue=[]
            if hasattr(c.motor,'cancel_text_prefetch'):c.motor.cancel_text_prefetch()
        # Terminal continuation is not a user job and must not block new work.
        if c.queue and c.queue[-1].action in ('terminal-hold','terminal-idle'):c.queue.pop()
        if c.current and c.current.action in ('terminal-hold','terminal-idle') and not c.queue:
            c.mark_current('completed','alife_next_task'); c.motion_id+=1
            c.current=None; c.segment_start_frame=None
        if c.current and c.current.kind=='loop' and job['mode']=='append':
            # A loop finishes at a generated segment boundary when queue becomes nonempty.
            pass
        c.queue=c.with_terminal_loop([*c.queue,*job['steps']])
        c.plan_timeline.bind(job['steps'],job['goal'],source='alife')
        c.plan_timeline.reconcile(c.current,c.queue)
        c.motor.transport_paused=False
        if hasattr(c.motor,'note_interaction'):c.motor.note_interaction()
        c.phase='Alife body task accepted'; c.warm_next_motion(); job['status']='queued'

    def _status(self,job):
        if job['cancelled']:return 'cancelled'
        statuses=[self.c.plan_timeline.records.get(id(s),(None,None,{'status':job['status']}))[2]['status'] for s in job['steps']]
        if all(s=='completed' for s in statuses):return 'completed'
        for source,target in (('error','error'),('running','playing'),('looping','looping'),('preparing','preparing'),('queued','queued'),('interrupted','interrupted'),('removed','interrupted'),('cancelled','cancelled')):
            if source in statuses:return target
        return job['status']

    def state(self):
        with self.c.lock:
            s=self.c.snapshot()
            return dict(connected=bool(self.c.motor.owner),ready=self.c.motor.ready,phase=s['phase'],
                        playing=s.get('playing'),frame=s.get('frame'),current=s.get('current'),
                        scheduled_start_frame=s.get('scheduled_start_frame'),next=s.get('next'),
                        plan_progress=s['plan_progress'],generation_timing=dict(getattr(self.c.motor,'metrics',{})),capabilities=getattr(self,'capabilities',{}),
                        face=self.face.snapshot(),speaking=self.speaking,gaze=getattr(self.c.motor,'gaze_mode','user'),rendered_face=getattr(self,'rendered_face',{}),rendered_face_age_s=time.monotonic()-getattr(self,'rendered_at',0),
                        tasks=[dict(task_id=j['id'],goal=j['goal'],status=self._status(j)) for j in self.jobs.values()])

    def stop(self,task_id=''):
        with self.c.lock:
            if not task_id:
                self.c.cancel()
                for j in self.jobs.values():
                    if self._status(j) not in ('completed','error','interrupted'):j['cancelled']=True
                self.deferred.clear()
            else:
                job=self.jobs.get(task_id)
                if not job:raise ValueError('Unknown task_id')
                if self._status(job) in ('completed','cancelled','interrupted','error'):return dict(cancelled=False,task_id=task_id,status=self._status(job))
                job['cancelled']=True
                self.deferred=[j for j in self.deferred if j is not job]
                owned={id(s) for s in job['steps']}
                for s in job['steps']:self.c.plan_timeline.mark(s,'cancelled',outcome='alife_stopped')
                self.c.queue=[s for s in self.c.queue if id(s) not in owned]
                if self.c.current and id(self.c.current) in owned:
                    self.c.motion_id+=1;self.c.current=None;self.c.segment_start_frame=None
                    self.c.motor.invalidate();self.c.motor.halt()
                self.c.plan_timeline.reconcile(self.c.current,self.c.queue)
            return dict(cancelled=True,task_id=task_id or 'all')

    def speech(self,data):
        speaking=data.get('speaking')
        if type(speaking) is not bool:raise ValueError('speaking must be boolean')
        duration=data.get('duration_s',.6)
        if type(duration) not in (float,int) or not math.isfinite(duration) or not 0<=duration<=30:raise ValueError('duration_s must be 0..30')
        with self.c.lock:
            self.speaking=speaking
            if data.get('cancel_pending'):
                for job in self.deferred:job['cancelled']=True
                self.deferred=[]
            with self.face.lock:
                self.face.speaking_until=time.monotonic()+duration if speaking else 0
                self.emit(dict(type='face_speech',id=self.face.sequence,duration_s=duration if speaking else 0))
            if not speaking and data.get('release',True):
                jobs=self.deferred;self.deferred=[]
                for job in jobs:
                    if not job['cancelled']:self._install(job)
        return dict(speaking=speaking)

    def runtime(self,command):
        request_id=uuid.uuid4().hex; done=threading.Event()
        slot=dict(done=done,result=None)
        with self.result_lock:self.results[request_id]=slot
        self.emit(dict(type='alife_control',request_id=request_id,**command))
        try:
            if not done.wait(12):raise ValueError('Desktop window did not acknowledge the command')
            result=slot['result']
            if result.get('error'):raise ValueError(result['error'])
            return result
        finally:
            with self.result_lock:self.results.pop(request_id,None)

    def get(self,path,query):
        if path=='state':return self.state()
        if path=='events':
            after=int(query.get('after',['0'])[0])
            with self.event_lock:return dict(cursor=self.seq,events=[e for e in self.events if e['id']>after])
        if path=='dialogue':
            with self.event_lock:return dict(messages=list(self.dialogue[-40:]))
        raise ValueError('Unknown Alife endpoint')

    def post(self,path,data):
        if path=='act':return self.act(data)
        if path=='stop':return self.stop(str(data.get('task_id','')))
        if path=='speech':return self.speech(data)
        if path=='express':
            emotion=data.get('emotion','neutral')
            # Validate all channels before applying any state.
            from emotion import EmotionState
            from core import FaceAction
            state=data.get('state',{})
            EmotionState.resolve(emotion,state)
            action=FaceAction.model_validate(data['action']).model_dump(exclude_none=True) if data.get('action') else None
            self.c.motor.expression(emotion)
            result=self.face.set_state(emotion,state)
            if action:self.face.set_action(action)
            return dict(accepted=True,emotion=emotion)
        if path=='gaze':
            mode=data.get('mode','user')
            if mode not in ('user','none'):raise ValueError('gaze mode must be user or none')
            self.c.motor.expression(self.c.motor.face,mode,scope='manual');return dict(mode=mode)
        if path=='runtime-result':
            with self.result_lock:
                slot=self.results.get(data.get('request_id'))
                if slot:slot['result']=data;slot['done'].set()
            return dict(accepted=True)
        if path=='runtime':
            action=data.get('action')
            if action not in ('position','move','subtitle','using'):raise ValueError('Unknown runtime action')
            if action=='move':
                for key,limit in (('x',10000),('y',10000),('seconds',10)):
                    value=data.get(key)
                    if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>limit or key=='seconds' and value<0:raise ValueError('Invalid move '+key)
            return self.runtime(data)
        if path in ('input','interaction'):
            text=str(data.get('text','')).strip()
            if not text or len(text)>2000:raise ValueError('text needs 1..2000 characters')
            if path=='input':
                with self.event_lock:self.dialogue.append(dict(role='user',text=text))
            return self.event(path,text)
        if path=='dialogue':
            text=str(data.get('text',''))[:4000]
            if text:
                with self.event_lock:
                    self.dialogue.append(dict(role='assistant',text=text));self.dialogue=self.dialogue[-40:]
            return dict(accepted=True)
        raise ValueError('Unknown Alife endpoint')
