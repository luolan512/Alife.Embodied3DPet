from context import compact_world
"""Purposeful look/capture/resume in the originating character-model context."""
import base64
import copy
import json
import threading
import time
import uuid


class ActiveLook:
    def __init__(self, emit, world, enabled, trace=None,scheduler=None,on_observed=None):
        self.emit=emit;self.world=world;self.enabled=enabled;self.trace=trace
        self.lock=threading.RLock();self.pending=None;self.result=None
        self.status=dict(phase='idle');self.closed=False
        self.scheduler=scheduler
        self.on_observed=on_observed

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.status)

    def cancel(self,reason='cancelled'):
        with self.lock:
            if self.pending:
                self.emit(dict(type='look_cancel',request_id=self.pending))
                self.result=dict(status='CANCELLED',reason=reason)
                self.status['phase']='cancelled';self.pending=None

    def receive(self,data):
        with self.lock:
            if not self.pending or data.get('request_id')!=self.pending:return False
            if data.get('status')=='CAPTURED':
                image=data.get('image')
                if not isinstance(image,str) or not image.startswith('data:image/jpeg;base64,') or len(image)>240000:return False
                raw=base64.b64decode(image.split(',',1)[1],validate=True)
                if not raw.startswith(b'\xff\xd8'):return False
                camera=data.get('camera',{})
                if not isinstance(camera,dict) or camera.get('source')!='eye':return False
                stamp=data.get('captured_at_unix')
                if type(stamp) not in (int,float) or abs(time.time()-stamp)>3:return False
                if data.get('target_in_view') is not True or data.get('stable_ms',0)<500:return False
            elif data.get('status') not in ('TARGET_NOT_FOUND','NOT_VISIBLE','UNSTABLE','TIMEOUT'):return False
            self.result=copy.deepcopy(data)
            self.status['phase']='captured' if data.get('status')=='CAPTURED' else 'not_seen'
            return True

    def request(self,look,valid):
        if not self.enabled():return dict(status='VISION_DISABLED',reason='Enable scene-image perception before requesting a look.')
        rid=uuid.uuid4().hex
        if self.closed or not valid():return dict(status='CANCELLED')
        with self.lock:
            if self.closed:return dict(status='CANCELLED')
            if self.pending:return dict(status='BUSY',reason='Another look owns the camera.')
            self.pending=rid;self.result=None;self.status=dict(phase='turning_and_stabilizing',request_id=rid,**look.model_dump())
        leased=False;keep_lease=False
        if self.scheduler:
            self.status['phase']='waiting_for_image_slot'
            leased=self.scheduler.claim_active(rid,lambda:valid() and self.enabled())
            if not leased:
                with self.lock:
                    if self.pending==rid:self.pending=None
                return dict(status='BUSY' if valid() and self.enabled() else 'CANCELLED',request_id=rid,reason='Shared perception scheduler did not grant an image slot.')
            self.status['phase']='turning_and_stabilizing'
        self.emit(dict(type='look_request',request_id=rid,**look.model_dump()))
        if self.trace:self.trace.record('active_look_requested',self.snapshot())
        deadline=time.monotonic()+look.timeout_s+2
        try:
            while time.monotonic()<deadline:
                if not valid() or not self.enabled():return dict(status='CANCELLED',request_id=rid)
                with self.lock:
                    if self.result is not None:
                        value=copy.deepcopy(self.result)
                        if leased and value.get('status')=='CAPTURED':value['_lease']=rid;keep_lease=True
                        return value
                time.sleep(.05)
            return dict(status='TIMEOUT',request_id=rid,reason='Runtime did not produce a stable visible frame.')
        finally:
            with self.lock:
                if self.pending==rid:self.pending=None;self.emit(dict(type='look_release',request_id=rid))
            if leased and not keep_lease:self.scheduler.release(rid)

    def decide(self,cfg,messages,planner,valid,debug):
        leases=[]
        try:return self._decide(cfg,messages,planner,valid,debug,leases)
        finally:
            if self.scheduler:
                for lease in leases:self.scheduler.release(lease)

    def _decide(self,cfg,messages,planner,valid,debug,leases):
        context=copy.deepcopy(messages);paths={};last_capture=None
        for round_index in range(3):
            if not valid():raise ValueError('decision_cancelled')
            if self.scheduler and leases:self.scheduler.mark_image_started(leases[-1])
            plan=planner({**cfg,'_image_paths':paths},context,on_debug=lambda **data:debug(look_round=round_index,**data))
            if self.scheduler:
                for lease in leases:self.scheduler.release(lease)
                leases.clear()
            if not valid():raise ValueError('decision_cancelled')
            if plan.look is None:
                if last_capture and self.on_observed:self.on_observed(last_capture)
                if last_capture and plan.observation:
                    from pathlib import Path
                    # Loaded lazily: offline scheduler tests do not need the perception package.
                    import sys
                    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'perception'))
                    from vision import normalize
                    try:
                        report=normalize(plan.observation)
                        plan.observation=copy.deepcopy(report)
                        report.update(source='character_model_image',observation_id=last_capture['request_id'],
                                      question=last_capture['question'],camera=last_capture['camera'],
                                      captured_at_unix=last_capture['captured_at_unix'])
                        if valid():self.world.observe_vision(report,last_capture['received_at'],120)
                    except ValueError:
                        if self.trace:self.trace.record('active_observation_invalid',dict(request_id=last_capture['request_id']))
                with self.lock:self.status['phase']='completed'
                return plan
            if round_index==2:raise ValueError('最多两次主动观察，请完成当前决策')
            capture=self.request(plan.look,valid)
            if capture.get('_lease'):leases.append(capture.pop('_lease'))
            last_capture=None
            # Keep the originating thought/action context; remove old image bytes.
            for message in context:
                if isinstance(message.get('content'),list):
                    message['content']=[part for part in message['content'] if part.get('type')!='image_url']
                    # Once a new image arrives, retain the older look's outcome, not its
                    # full duplicate world snapshot. The latest geometry is appended below.
                    for part in message['content']:
                        if part.get('type')!='text':continue
                        try:
                            value=json.loads(part.get('text',''))
                            old=value.get('look_result')
                            if isinstance(old,dict):
                                value['look_result']={k:old[k] for k in ('request_id','status','target','question','reason') if k in old}
                                part['text']=json.dumps(value,ensure_ascii=False)
                        except (ValueError,AttributeError):pass
            context.append(dict(role='assistant',content=plan.model_dump_json()))
            metadata={k:v for k,v in capture.items() if k not in ('image','signature')}
            metadata.update(question=plan.look.question,target=plan.look.target,world_state=compact_world(self.world.snapshot().get('world_state')))
            content=[dict(type='text',text=json.dumps(dict(look_result=metadata),ensure_ascii=False))]
            if capture.get('status')=='CAPTURED':
                received=time.monotonic();path=None
                if self.trace:
                    folder=self.trace.run_dir/'vision';folder.mkdir(exist_ok=True)
                    file=folder/(capture['request_id']+'.jpg');file.write_bytes(base64.b64decode(capture['image'].split(',',1)[1]));path=str(file)
                paths[capture['image']]=path
                content.append(dict(type='image_url',image_url=dict(url=capture['image'],detail='low')))
                last_capture=dict(**metadata,received_at=received,signature=capture.get('signature'))
                with self.lock:self.status['phase']='character_model_looking'
            context.append(dict(role='user',content=content))
            if self.trace:self.trace.record('active_look_result',dict(**metadata,image_path=paths.get(capture.get('image'))))
        raise ValueError('active_look_limit')
