"""Finite pre-generated events and bounded, reusable idle clips.

Only event synthesis invokes ARDY. Idle replay never feeds its output into ARDY.
An event starts from a separately generated posture anchor, not the old chain.
"""
import math
import time
import threading
import numpy as np
import torch
from scipy.spatial.transform import Rotation

IDLES = {
    'stand': 'A person stands upright facing forward, arms relaxed at their sides, staying still.',
    'sit': 'A person remains seated upright on the ground, facing forward, resting still.',
    'kneel': 'A person remains kneeling upright facing forward, resting still.',
    'lie': 'A person remains lying on their back, resting still.',
}


from pose_protection import PoseProtection


class EventMotion(PoseProtection):
    def init_events(self):
        self.clear_pose_guard()
        self.idle_cache = {}
        self.idle_clip = None
        self.idle_kind = 'stand'
        self.anchor_kind = 'stand'
        self.event_end = None
        self.event_serial = 0
        self.gaze_mode = 'user'
        self.last_interaction=time.monotonic()
        self.policy_enabled=False
        self.continuous_live=False
        self.event_build_lock=threading.Lock()

    def note_interaction(self):
        self.last_interaction=time.monotonic();self.policy_enabled=True

    def event_mode_active(self):
        mode=self.params.get('behavior_mode')
        if mode is None:return self.params.get('event_mode',False)
        return mode=='event' or (mode=='hybrid' and time.monotonic()-getattr(self,'last_interaction',time.monotonic())>=self.params.get('inactive_minutes',30)*60)

    @torch.inference_mode()
    def synthesize_clip(self, prompt, frames, history, valid):
        self.record(stage='event_text_encoding',prompt=prompt,text_cache_hit=self.encoder.cache.contains(prompt))
        self.emit({'type':'motion_preparing','prompt':prompt})
        try:feat, _ = self.encode_prompt(prompt,valid) if hasattr(self,'encode_prompt') else self.encoder([prompt])
        except RuntimeError:
            if not valid():return None
            raise
        self.record(stage='event_synthesis')
        device = next(self.model.parameters()).device
        feat = feat.to(device)
        generated = []
        remaining = frames
        while remaining > 0:
            if not valid(): return None
            if history is not None:
                budget=int(self.fps*10)-self.horizon
                length=min(history.shape[1],budget)//4*4
                history=history[:,-length:] if length else None
            length = 0 if history is None else history.shape[1]
            with self.generation_lock:
                if not valid():return None
                result = (self.inference_step if hasattr(self,'inference_step') else self.model.autoregressive_step)(
                    num_frames=length+self.horizon, num_denoising_steps=self.params['denoising_steps'],
                    motion_mask=None, observed_motion=None,
                    cfg_weight=(self.params['cfg_text'], self.params['cfg_constraint']), texts=None,
                    text_feat=feat, text_pad_mask=torch.ones(feat.shape[:2], dtype=torch.bool, device=device),
                    init_history_sequence=history,
                    init_global_translation=torch.tensor([[0., 0., 1.35]],device=device) if history is None else None)
            self.metrics['event_inference_calls']=self.metrics.get('event_inference_calls',0)+1
            new = result[:,length:].detach()
            if not torch.isfinite(new).all(): raise RuntimeError('Non-finite event motion')
            generated.append(new[:,:min(remaining,self.horizon)])
            keep=min(self.params.get('history_frames',16),result.shape[1],int(self.fps*10)-self.horizon)//4*4
            history=result[:,-keep:].clone() if keep else None
            remaining -= self.horizon
        return torch.cat(generated,dim=1)

    @torch.inference_mode()
    def static_history(self, pose, frames=4):
        rep = self.model.motion_rep
        data = rep.inverse(pose[:,-1:], is_normalized=True)
        return rep(local_joint_rots=data['local_rot_mats'].repeat(1,frames,1,1,1),
                   root_positions=data['root_positions'].repeat(1,frames,1), to_normalize=True)

    @torch.inference_mode()
    def transition(self, source, target, frames=12):
        """Shortest-arc quaternion interpolation, then rebuild all motion features."""
        rep = self.model.motion_rep
        a,b = (rep.inverse(p[:,-1:],is_normalized=True) for p in (source,target))
        shape = a['local_rot_mats'].shape
        qa,qb = (Rotation.from_matrix(d['local_rot_mats'].float().cpu().numpy().reshape(-1,3,3)).as_quat() for d in (a,b))
        qb = np.where((qa*qb).sum(-1,keepdims=True)<0,-qb,qb)
        t = np.linspace(0,1,frames+1,dtype=np.float32)[1:]
        t = t*t*(3-2*t)
        q = qa[None]*(1-t[:,None,None])+qb[None]*t[:,None,None]
        q /= np.linalg.norm(q,axis=-1,keepdims=True)
        mats = Rotation.from_quat(q.reshape(-1,4)).as_matrix().reshape(1,frames,shape[2],3,3)
        rots = torch.as_tensor(mats,device=source.device,dtype=source.dtype)
        weight = torch.as_tensor(t,device=source.device,dtype=source.dtype)[None,:,None]
        roots = a['root_positions']*(1-weight)+b['root_positions']*weight
        return rep(local_joint_rots=rots,root_positions=roots,to_normalize=True)

    @torch.inference_mode()
    def idle_for(self, kind, valid):
        # Four cache entries at most, invalidated when generation settings change.
        if kind in self.idle_cache: return self.idle_cache[kind]
        clip = self.synthesize_clip(IDLES[kind],80,None,valid)
        if clip is None:return None
        # A short reversible tail has an identical seam and cannot accumulate drift.
        tail = clip[:,-16:].clone()
        data = self.model.motion_rep.inverse(tail,is_normalized=True)
        roots = data['root_positions'].clone()
        roots[:,:,0] = roots[:,0:1,0]; roots[:,:,2] = roots[:,0:1,2]
        rots = torch.cat([data['local_rot_mats'],data['local_rot_mats'][:,1:-1].flip(1)],dim=1)
        roots = torch.cat([roots,roots[:,1:-1].flip(1)],dim=1)
        idle = self.model.motion_rep(local_joint_rots=rots,root_positions=roots,to_normalize=True).detach()
        if valid():self.idle_cache[kind]=idle
        return idle

    @torch.inference_mode()
    def begin_cached_idle(self,kind,step,valid):
        self.transport_paused=False
        clip=self.idle_for(kind,valid)
        if clip is None or not valid():return None
        with self.generation_lock:
            with self.control_lock:
                if not valid():return None
                with self.lock:
                    anchor=min(self.end,self.cursor+4) if self.motion is not None else -1
                    prefix=self.motion[:,:anchor-self.base+1] if self.motion is not None else None
                    bridge=self.transition(prefix[:,-1:],clip[:,:1],20) if prefix is not None else clip[:,:0]
                    chunk=torch.cat([bridge,clip,clip,clip],dim=1)
                    self.epoch+=1;self.clear_pose_guard()
                    self.features=None;self.idle_clip=clip;self.idle_kind=kind;self.anchor_kind=kind
                    self.event_end=anchor+bridge.shape[1];self.continuous_live=False
                    self.prompt='Cached idle: '+kind
                    self.motion=chunk if prefix is None else torch.cat([prefix,chunk],dim=1)
                    self.playing=not self.transport_paused
                    self.emit(self._packet(anchor+1,chunk))
                    self.expression(step.emotion or 'neutral',step.look_at,scope='step',face_text=getattr(step,'face_text',None))
                    self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})
                    self.record(stage='cached_idle_ready',idle_kind=kind)
                    return anchor+1

    def pump_idle(self):
        with self.lock:
            if not self.ready or not self.owner or not self.playing or self.idle_clip is None:return False
            if self.end-self.cursor>=60:return True
            start=self.end+1
            clip=self.idle_clip
            self.motion=torch.cat([self.motion,clip],dim=1)
            if self.cursor-self.base>160:
                cut=self.cursor-self.base-40
                self.motion=self.motion[:,cut:].clone();self.base+=cut
            packet=self._packet(start,clip)
            packet['cached_replay']=True
            if self.event_end is not None and self.cursor>=self.event_end:
                self.prompt='Held pose snapshot' if self.pose_guard=='freeze' else 'Cached idle: '+self.idle_kind
            self.emit(packet)
            return True

    def motion_is_settled(self, clip):
        data=self.model.motion_rep.inverse(clip[:,-min(16,clip.shape[1]):],is_normalized=True)
        joints=data['posed_joints']
        speed=(joints[:,1:]-joints[:,:-1]).norm(dim=-1).mean().item()*self.fps
        self.metrics['settle_speed_m_s']=round(speed,4)
        return speed<.06

    def nearest_idle(self, pose, default):
        if not self.idle_cache:return default
        rep=self.model.motion_rep
        source=rep.inverse(pose[:,-1:],is_normalized=True)
        matches=[]
        for kind,clip in self.idle_cache.items():
            other=rep.inverse(clip[:,:1],is_normalized=True)
            height=(source['root_positions'][...,1]-other['root_positions'][...,1]).abs().item()
            if height>.25:continue
            score=(source['local_rot_mats']-other['local_rot_mats']).square().mean().item()+height
            matches.append((score,kind))
        return min(matches)[1] if matches else default

    @torch.inference_mode()
    def begin_event(self, step, valid):
        self.transport_paused=False
        started=time.monotonic()
        with self.event_build_lock:
            if not valid():return None
            origin=self.anchor_kind
            destination=getattr(step,'after_idle','keep')
            if destination=='keep':
                destination=step.action if step.action in IDLES else self.idle_kind
            base=self.idle_for(origin,valid)
            if base is None:return None
            # Ensure a standing/reset request does not inherit a sitting event history.
            prompt=step.motion_prompt
            frames=math.ceil(getattr(step,'duration_s',3)*self.fps/4)*4
            clip=self.synthesize_clip(prompt,frames,self.static_history(base[:,:1]),valid)
            if clip is None:return None
            copies=[clip]
            for _ in range(getattr(step,'repeat',1)-1):
                copies.extend([self.transition(clip[:,-1:],clip[:,:1],8),clip])
            clip=torch.cat(copies,dim=1)
            protect=self.static_request(step)
            buffer_frames=round(self.params.get('settle_buffer_s',0)*self.fps/4)*4
            if protect:buffer_frames=max(buffer_frames,self.fps*10)
            extended=0;settled=False
            while extended<buffer_frames:
                count=min(self.horizon,buffer_frames-extended)
                count=math.ceil(count/4)*4
                tail=self.synthesize_clip(prompt,count,clip[:,-16:],valid)
                if tail is None:return None
                clip=torch.cat([clip,tail],dim=1);extended+=count
                settled=self.stable_pose_window(clip) if protect else self.motion_is_settled(clip)
                if settled and extended>=self.fps*(5 if protect else 2):break
            if getattr(step,'after_idle','keep')=='keep' and step.action not in IDLES and destination!='hold':
                destination=self.nearest_idle(clip,destination)
            if not protect and buffer_frames and not settled and destination!='hold':
                # Use ARDY to slow/settle into the chosen resting condition before replay.
                tail=self.synthesize_clip(IDLES[destination],self.horizon,clip[:,-16:],valid)
                if tail is None:return None
                clip=torch.cat([clip,tail],dim=1)
            # No snapshot while a static request is still moving. Continue its own condition.
            live=protect and (not settled or self.params.get('static_protection','freeze')=='anchor')
            live_feat=self.encode_prompt(prompt,valid)[0] if live else None
            idle=(self.static_history(clip[:,-1:],32) if protect or destination=='hold' else self.idle_for(destination,valid))
            if idle is None or not valid():return None
            bridge=self.transition(clip[:,-1:],idle[:,:1],16)
            with self.control_lock:
                if not valid():return None
                with self.lock:
                    # Playback continued throughout synthesis. Join from NOW only at commit.
                    anchor=min(self.end,max(self.base,self.cursor+4)) if self.motion is not None else -1
                    prefix=self.motion[:,:anchor-self.base+1] if self.motion is not None else None
                    entrance=self.transition(prefix[:,-1:],clip[:,:1],12) if prefix is not None else clip[:,:0]
                    event=torch.cat([entrance,clip],dim=1)
                    chunk=event if live else torch.cat([event,bridge,idle,idle,idle],dim=1)
                    self.epoch+=1;self.event_serial+=1
                    self.clear_pose_guard()
                    self.prompt=prompt;self.features=live_feat
                    self.continuous_live=live
                    self.event_end=anchor+event.shape[1]
                    self.idle_kind=destination;self.idle_clip=None if live else idle
                    if protect:
                        if settled:self.capture_pose(event,self.event_end)
                        else:self.pose_pending={'earliest_frame':self.event_end,'posture':destination}
                    if live and not settled:self.event_end=None
                    if destination in IDLES:self.anchor_kind=destination
                    self.motion=chunk if prefix is None else torch.cat([prefix,chunk],dim=1)
                    self.playing=not self.transport_paused
                    self.emit(self._packet(anchor+1,chunk))
                    self.expression(getattr(step,'emotion',None) or 'neutral',getattr(step,'look_at','auto'),scope='step')
                    self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})
                    self.timeline=[{'start':anchor+1,'end':self.event_end,'label':step.action,'prompt':prompt,'epoch':self.epoch}]
                    self.record(stage='event_ready',event_serial=self.event_serial,event_frames=event.shape[1],
                                repeat=getattr(step,'repeat',1),idle_kind=destination,
                                motion_prepare_ms=round((time.monotonic()-started)*1000,2),
                                settle_buffer_frames=extended,settled_before_idle=settled,
                                reanchor='independent_cached_posture')
                    return anchor+1

    def event_state(self):
        return {'enabled':getattr(self,'event_end',None) is not None and getattr(self,'idle_clip',None) is not None,
                'configured_mode':self.params.get('behavior_mode','event' if self.params.get('event_mode') else 'stream'),
                'effective_mode':'event' if self.event_mode_active() else 'stream',
                'inactive_seconds':round(time.monotonic()-getattr(self,'last_interaction',time.monotonic()),1),
                'inactive_minutes':self.params.get('inactive_minutes',30),
                'idle':getattr(self,'idle_kind','stand'),
                'anchor':getattr(self,'anchor_kind','stand'),
                'event_end_frame':getattr(self,'event_end',None),'gaze':getattr(self,'gaze_mode','none'),
                'retained_frames':0 if self.motion is None else self.motion.shape[1],
                'cached_idle_clips':len(getattr(self,'idle_cache',{})),
                'static_protection':getattr(self,'pose_guard','off'),
                'allow_cached_idle_loops':self.params.get('allow_cached_idle_loops',False),
                'motion_lookahead':self.lookahead_state() if hasattr(self,'lookahead_state') else {},
                'waiting_for_next':getattr(self,'waiting_for_next',False),
                'waiting_motion_live':getattr(self,'waiting_motion_live',False),
                'static_pending':getattr(self,'pose_pending',None) is not None,
                'snapshot_frame':getattr(self,'pose_guard_frame',None)}
