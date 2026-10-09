"""One bounded future motion segment, conditioned on the current event's predicted end."""
import threading
import time
import torch
from text_schedule import TextPreparationPreempted


class MotionLookahead:
    def init_lookahead(self):
        self.motion_prepare_serial=0
        self.prepared_motion=None
        self.motion_preparing=False
        self.motion_prepare_step=None

    def prepare_next_step(self,step,valid):
        if not self.params.get('motion_prefetch',True) or self.params.get('behavior_mode')!='event':return
        if step.action in ('terminal-hold','terminal-idle'):return
        boundary=getattr(self,'finite_boundary',None)
        # Infinite dynamic loops keep live continuation; no invented end pose.
        if boundary is None or step.action.startswith('idle_'):return
        self.motion_prepare_serial=getattr(self,'motion_prepare_serial',0)+1
        serial=self.motion_prepare_serial
        self.prepared_motion=None;self.motion_preparing=True;self.motion_prepare_step=step
        signature=dict(self.params)
        origin=self.prompt;epoch=self.epoch
        def eligible():
            return (not getattr(self,'closed',False) and serial==self.motion_prepare_serial
                    and valid() and self.params==signature)
        def prepare():
            started=time.monotonic()
            try:
                # Warm the next condition immediately; cold encoding must not start
                # only after the current playback buffer reaches its endpoint.
                while eligible():
                    try:
                        feat,_=self.encode_prompt(step.motion_prompt,eligible,priority=1)
                        break
                    except TextPreparationPreempted:continue
                if not eligible():return
                # Never retain a minute-long future chain. Wait until within 12 seconds.
                while eligible() and boundary-self.cursor>self.fps*12:time.sleep(.1)
                if not eligible():return
                while eligible() and self.end<boundary:
                    if self.epoch!=epoch or self.features is None:break
                    if not self.playing:time.sleep(.1);continue
                    with self.generation_lock:
                        if not eligible():return
                        if not self._generate(self.end,self.features,epoch):return
                with self.lock:
                    if not eligible() or self.motion is None:return
                    anchor=min(boundary,self.end)
                    if anchor<self.base:return
                    length=min(self.params.get('history_frames',16),anchor-self.base+1,int(self.fps*10)-self.horizon)//4*4
                    if not length:return
                    history=self.motion[:,anchor-self.base-length+1:anchor-self.base+1].clone()
                with self.generation_lock:
                    if not eligible():return
                    # A foreground generation might already have started this step.
                    if self.prompt!=origin and not getattr(self,'waiting_for_next',False):return
                    device=next(self.model.parameters()).device
                    text=feat.to(device)
                    with torch.inference_mode():
                        result=(self.inference_step if hasattr(self,'inference_step') else self.model.autoregressive_step)(
                            num_frames=length+self.horizon,num_denoising_steps=self.params['denoising_steps'],
                            motion_mask=None,observed_motion=None,cfg_weight=(self.params['cfg_text'],self.params['cfg_constraint']),
                            texts=None,text_feat=text,text_pad_mask=torch.ones(text.shape[:2],dtype=torch.bool,device=device),
                            init_history_sequence=history,init_global_translation=None)
                    clip=result[:,length:].detach()
                    if not torch.isfinite(clip).all():return
                    with self.lock:
                        if not eligible():return
                        self.prepared_motion={'step':step,'clip':clip,'feat':feat,'pose':history[:,-1:],
                                              'signature':signature,'valid':valid,'anchor':anchor}
                        self.metrics['ardy_inference_calls']=self.metrics.get('ardy_inference_calls',0)+1
                        self.record(stage='next_motion_ready',prepared_action=step.action,
                                    motion_lookahead_ms=round((time.monotonic()-started)*1000,2))
            except Exception as exc:self.record(motion_lookahead_error=type(exc).__name__)
            finally:
                if serial==self.motion_prepare_serial:self.motion_preparing=False
        threading.Thread(target=prepare,daemon=True).start()

    def buffer_queued_transition(self,next_step):
        """Keep task completion separate from playback while a real task waits."""
        if self.params.get('behavior_mode')!='event' or next_step is None:return
        with self.lock:
            boundary=getattr(self,'finite_boundary',None)
            if boundary is None or not self.playing:return
            # Even a ready segment needs buffered outgoing frames while the
            # scheduler commits and exports the transition packet.
            # Keep the logical completion frame; only allow extra frames to play
            # while the next condition is preparing. Respect explicit freeze guards.
            reserve=max(self.fps,round((getattr(self,'last_generation_s',0)+.5)*self.fps))
            if boundary-self.cursor>reserve:return
            if next_step.action in ('terminal-hold','terminal-idle'):
                if self.pose_snapshot is None:
                    self.pose_snapshot=self.motion[:,-1:].clone();self.pose_guard_frame=self.end
                    self.pose_condition_prompt=self.prompt;self.pose_live_features=self.features
                self.pose_guard='anchor';self.pose_pending=None;self.idle_clip=None
            self.arm_queued_transition(next_step)
            self.record(stage='queued_transition_buffer',waiting_action=next_step.action,
                        transition_reserve_frames=reserve)

    def arm_queued_transition(self,next_step):
        """Called with the worker lock held; preserve pose, prompt and epoch."""
        self.queued_transition_epoch=self.epoch
        self.queued_transition_action=next_step.action
        self.finite_boundary=None
        if self.features is None and self.pose_live_features is not None:
            self.features=self.pose_live_features
            self.prompt=self.pose_condition_prompt or self.prompt
        if self.pose_snapshot is not None and self.pose_guard=='freeze':
            self.pose_guard='anchor'
            self.idle_clip=None
        if getattr(self,'queued_transition_loop',None) is None and self.motion is not None:
            # Reuse only a short outgoing tail, never restart the one-shot or
            # synthesize an unrelated standing/sitting preset.
            self.queued_transition_loop=self.motion[:,-min(self.motion.shape[1],self.fps):].clone()

    @torch.inference_mode()
    def pump_queued_transition(self,allow_unqueued=False):
        """Small replay reserve when another inference owns the generation lock."""
        with self.lock:
            tail=getattr(self,'queued_transition_loop',None)
            queued=getattr(self,'queued_transition_epoch',None)==self.epoch and tail is not None
            if not self.playing or self.transport_paused or self.motion is None or not getattr(self,'owner',True):return False
            if not queued:
                if not allow_unqueued or self.features is None or self.idle_clip is not None or self.finite_boundary is not None:return False
                tail=self.motion[:,-min(self.motion.shape[1],self.fps):].clone()
            # Normal continuation gets priority; this is only a near-empty-buffer
            # fallback, including cold encoding or failed future prediction.
            if self.end-self.cursor>=self.fps:return False
            if self.features is not None and not self.generation_lock.locked():return False
            rep=self.model.motion_rep
            data=rep.inverse(tail,is_normalized=True)
            now=rep.inverse(self.motion[:,-1:],is_normalized=True)
            roots=data['root_positions'].clone()
            # Preserve world placement at each seam. Do not teleport back to the
            # absolute coordinates of the cached tail on repeated playback.
            for axis in (0,2):roots[:,:,axis]+=now['root_positions'][:,:,axis]-roots[:,0:1,axis]
            clip=rep(local_joint_rots=data['local_rot_mats'],root_positions=roots,to_normalize=True)
            if self.pose_snapshot is not None:clip=self.constrain_pose(clip)
            bridge=self.transition(self.motion[:,-1:],clip[:,:1],max(4,round(self.fps*.3)))
            chunk=torch.cat([bridge,clip],dim=1)
            start=self.end+1
            self.motion=torch.cat([self.motion,chunk],dim=1)
            if self.cursor-self.base>160:
                cut=self.cursor-self.base-40
                self.motion=self.motion[:,cut:].clone();self.base+=cut
            self.emit(self._packet(start,chunk))
            self.record(stage='queued_wait_loop' if queued else 'buffer_recovery_loop',waiting_action=self.queued_transition_action if queued else None,
                        transition_loop_frames=chunk.shape[1])
            return True

    def take_prepared_motion(self,step,valid):
        prepared=getattr(self,'prepared_motion',None)
        if not prepared or prepared['step'] is not step:return None
        if not valid() or not prepared['valid']() or prepared['signature']!=self.params:
            self.prepared_motion=None;return None
        with self.lock:
            anchor=min(self.end,self.cursor+4)
            if self.motion is None or anchor<self.base:return None
            rep=self.model.motion_rep
            old=rep.inverse(prepared['pose'],is_normalized=True)
            now=rep.inverse(self.motion[:,anchor-self.base:anchor-self.base+1],is_normalized=True)
            root=(old['root_positions']-now['root_positions']).norm().item()
            rotation=(old['local_rot_mats']-now['local_rot_mats']).abs().max().item()
            if root>.05 or rotation>.2:
                self.prepared_motion=None;self.record(stage='next_motion_discarded',reason='pose_changed');return None
            bridge=self.transition(self.motion[:,anchor-self.base:anchor-self.base+1],prepared['clip'][:,:1],4)
            chunk=torch.cat([bridge,prepared['clip']],dim=1)
            self.epoch+=1;self.clear_pose_guard()
            self.prompt=step.motion_prompt;self.features=prepared['feat'];self.idle_clip=None
            loop=step.kind=='loop';duration=step.duration_s*step.repeat
            self.event_end=None if loop else anchor+1+round((duration+self.params.get('settle_buffer_s',6))*self.fps)
            self.finite_boundary=self.event_end
            self.active_is_static=self.static_intent(step);self.continuous_live=True
            if self.static_request(step):
                earliest=anchor+1+round((duration+5)*self.fps)
                self.pose_pending={'earliest_frame':earliest,'deadline_frame':earliest+round(5*self.fps),'posture':'hold'}
            self.motion=torch.cat([self.motion[:,:anchor-self.base+1],chunk],dim=1)
            self.playing=not self.transport_paused
            self.emit(self._packet(anchor+1,chunk));self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})
            self.expression(step.emotion or 'neutral',step.look_at,scope='step',face_text=getattr(step,'face_text',None))
            self.timeline=[item for item in self.timeline if item['start']<anchor+1]
            self.timeline.append({'start':anchor+1,'end':anchor+1+round(duration*self.fps),
                                  'label':step.action,'prompt':step.motion_prompt,'epoch':self.epoch})
            self.timeline=self.timeline[-30:]
            self.prepared_motion=None
            self.record(stage='prepared_motion_started',prepared_action=step.action,scheduled_frame=anchor+1)
            return anchor+1

    def lookahead_state(self):
        prepared=getattr(self,'prepared_motion',None)
        return {'enabled':self.params.get('motion_prefetch',True),
                'preparing':getattr(self,'motion_preparing',False),
                'ready':prepared is not None and prepared['valid']() and prepared['signature']==self.params,
                'action':prepared['step'].action if prepared else getattr(getattr(self,'motion_prepare_step',None),'action',None)}
