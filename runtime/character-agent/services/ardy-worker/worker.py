"""Headless ARDY backend: timed motion stream, independent of Viser/VRM/LLM."""
import math
import os
import threading
import time
import sys
from types import SimpleNamespace
from pathlib import Path
import numpy as np
import torch
from scipy.spatial.transform import Rotation

torch.set_num_threads(max(1,min(8,int(os.environ.get("CHARACTER_CPU_THREADS","4")))))

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent / 'ardy' / 'scripts'))
from interactive_demo.embedding_cache import CachedTextEncoder
from ardy.model.load_model import load_model, load_text_encoder
from text_schedule import TextScheduler,TextPreparationPreempted
import text_encoder_config
from settings import load as load_settings, save as save_settings, validate
from event_motion import EventMotion
from motion_lookahead import MotionLookahead
from acceleration import AcceleratedInference
from interactive_demo.window_budget import compute_window_num_frames

class ArdyWorker(EventMotion,MotionLookahead):
    def __init__(self, emit, trace=None):
        self.emit = emit
        self.trace = trace
        self.lock = threading.RLock()
        self.generation_lock = threading.Lock()
        self.text_scheduler=TextScheduler()
        self.prefetch_serial=0
        self.ready = False
        self.closed = False
        self.owner = False
        self.playing = False
        self.transport_paused = False
        self.cursor = 0
        self.base = 0
        self.epoch = 0
        self.motion = None
        self.position = [0.,0.,1.35]
        self.prompt = ''
        self.face = 'neutral'
        self.features = None
        self.last_generation_s = .6
        self.error = None
        self.encoder = None
        self.params = load_settings()
        self.metrics = {'cpu_threads':torch.get_num_threads(),'text_encoder_threads':int(os.environ.get('KIMODO_THREADS','4'))}
        self.debug_events = []
        self.timeline = []
        self.last_pose_refresh_cursor=0
        self.pose_refresh_count=0
        self.init_events();self.init_lookahead()
        self.horizon = 40 if self.params['model']=='core40' else 8
        self.on_publish = lambda snapshot,chat:None
        self.environment = lambda:{}
        threading.Thread(target=self._run,daemon=True).start()

    def _run(self):
        try:
            raw_encoder=load_text_encoder(mode='kimodo',device='cpu')
            self.text_encoder_settings=text_encoder_config.load()
            raw_encoder.select_backend(self.text_encoder_settings['mode'],text_encoder_config.GPU_EXE,self.text_encoder_settings['layer_chunk'])
            self.encoder = CachedTextEncoder(raw_encoder,cache_dir=str(ROOT.parent/'ardy'/'.cache'/'text_embeddings'))
            self.model = load_model(self.params['model'],device='cuda:0' if torch.cuda.is_available() else 'cpu',text_encoder=self.encoder)
            self.acceleration=AcceleratedInference(self.model,self.params,self.record)
            self.fps = self.model.motion_rep.fps
            self.skeleton = self.model.motion_rep.skeleton
            torch.manual_seed(int(self.params['seed']))
            if self.params['startup_default_prompt']:
                self.prompt = 'A person is standing.'
                self.features,_ = self.encoder([self.prompt])
                with self.generation_lock:
                    self._generate(-1,self.features,self.epoch)
            self.ready = True
            self.playing = self.motion is not None
            self.emit({'type':'backend_ready','skeleton':self.metadata()})
            self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})
            while not self.closed:
                self.pump_queued_transition(allow_unqueued=True)
                if self.pump_idle():
                    time.sleep(.05)
                    continue
                self.refill_playback()
                time.sleep(.05)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.error = type(exc).__name__+': '+str(exc)[:160]
            self.playing = False
            self.emit({'type':'backend_error','error':self.error})

    @property
    def end(self):
        return self.base+self.motion.shape[1]-1 if self.motion is not None else -1

    def refill_playback(self):
        """Append at the actual tail; never rebuild already buffered playback."""
        trigger=max(self.params['trigger_frames'],math.ceil((self.last_generation_s+.5)*self.fps))
        trigger=min(trigger,self.fps*10)
        if not (self.ready and self.owner and self.playing and not self.transport_paused
                and self.features is not None and (self.params.get('behavior_mode') or self.params['auto_replan'])
                and self.end-self.cursor<trigger):return False
        if not self.generation_lock.acquire(blocking=False):return False
        try:
            with self.lock:
                anchor,epoch,feat=self.end,self.epoch,self.features
                refresh=not self.params.get('behavior_mode') and self.params.get('refresh_last_pose',False) and self.cursor-getattr(self,'last_pose_refresh_cursor',0)>=self.fps*10
            return self._generate(anchor,feat,epoch,refresh_pose=refresh)
        finally:self.generation_lock.release()

    def metadata(self):
        return {'names':self.skeleton.bone_order_names,'parents':self.skeleton.joint_parents.cpu().tolist(),
                'rest_positions':self.skeleton.neutral_joints.cpu().tolist(),'fps':self.fps,
                'source':'ARDY Core27','coordinate_system':'right-handed Y-up metres; quaternions xyzw'}

    def _packet(self,start,features):
        output = self.model.motion_rep.inverse(self.model.motion_rep.unnormalize(features),is_normalized=False)
        mats=output['global_rot_mats'][0].float().detach().cpu().numpy()
        q=Rotation.from_matrix(mats.reshape(-1,3,3)).as_quat().reshape(len(mats),27,4)
        roots=output['root_positions'][0].float().detach().cpu().numpy()
        joints=output['posed_joints'][0].float().detach().cpu().numpy()
        return {'type':'motion','epoch':self.epoch,'start':start,'fps':self.fps,
                'roots':np.round(roots,5).tolist(),'rotations':np.round(q,6).tolist(),'joints':np.round(joints,5).tolist(),
                'prompt':self.prompt}

    @torch.inference_mode()
    def _generate(self,anchor,feat,epoch,*,switch_prompt=None,is_current=None,refresh_pose=False,hold_pose=None):
        with self.lock:
            if epoch != self.epoch or (is_current and not is_current()): return False
            boundary=getattr(self,'finite_boundary',None) if switch_prompt is None else None
            if boundary is not None and (anchor>=boundary or self.end>=boundary):return False
            history=None
            if self.motion is not None:
                idx=anchor-self.base
                if refresh_pose:
                    rep=self.model.motion_rep
                    pose=rep.inverse(self.motion[:,idx:idx+1].clone(),is_normalized=True)
                    history=rep(local_joint_rots=pose['local_rot_mats'].repeat(1,4,1,1,1),
                                root_positions=pose['root_positions'].repeat(1,4,1),to_normalize=True)
                else:
                    length=min(self.params['history_frames'],idx+1,int(getattr(self,'fps',20)*10)-self.horizon)//4*4
                    if length: history=self.motion[:,idx-length+1:idx+1].clone()
            length=0 if history is None else history.shape[1]
        device=next(self.model.parameters()).device
        text=feat.to(device)
        started=time.monotonic()
        window=compute_window_num_frames(length,self.horizon,4,int(getattr(self,'fps',20)*10))
        result=self.inference_step(num_frames=window,num_denoising_steps=self.params['denoising_steps'],
            motion_mask=None,observed_motion=None,cfg_weight=(self.params['cfg_text'],self.params['cfg_constraint']),texts=None,
            text_feat=text,text_pad_mask=torch.ones(text.shape[:2],dtype=torch.bool,device=device),
            init_history_sequence=history,
            init_global_translation=torch.tensor([[0.,0.,1.35]],device=device) if history is None else None)
        if device.type=='cuda':torch.cuda.synchronize(device)
        inference_ms=(time.monotonic()-started)*1000
        new=result[:,length:].detach()
        if boundary is not None:new=new[:,:max(0,boundary-anchor)]
        with self.lock:
            self.last_generation_s=inference_ms/1000
            self.metrics['ardy_inference_ms']=round(inference_ms,2)
            self.metrics['ardy_inference_calls']=self.metrics.get('ardy_inference_calls',0)+1
            if epoch != self.epoch or (anchor>=0 and self.cursor>anchor) or (is_current and not is_current()): return False
            if switch_prompt is not None:
                # Commit the new condition/version only once its frames exist.
                # Until this point old packets and playback feedback remain valid.
                self.epoch += 1
                self.prompt = switch_prompt
                self.features = feat
                self.last_pose_refresh_cursor=self.cursor
                self.idle_clip=None;self.event_end=None
                self.clear_pose_guard()
                if hold_pose is not None:
                    self.pose_snapshot=hold_pose.clone();self.pose_guard='anchor';self.pose_guard_frame=anchor
                    self.pose_live_features=feat;self.pose_condition_prompt=switch_prompt
                    new=self.constrain_pose(new)
            if refresh_pose:
                self.last_pose_refresh_cursor=self.cursor
                self.pose_refresh_count=getattr(self,'pose_refresh_count',0)+1
                self.record(pose_refresh_count=self.pose_refresh_count,pose_refresh_anchor=anchor,
                            pose_refresh_history_frames=4,pose_refresh_strategy='last_pose_zero_velocity')
            if switch_prompt is None:new=self.protect_generated(new,anchor)
            packet_epoch = self.epoch
            if self.motion is None: self.motion=new
            else: self.motion=torch.cat([self.motion[:,:anchor-self.base+1],new],dim=1)
            packing=time.monotonic()
            packet=self._packet(anchor+1,new)
            self.metrics['packet_export_ms']=round((time.monotonic()-packing)*1000,2)
            packet['epoch']=packet_epoch
            if self.cursor-self.base>200:
                cut=self.cursor-self.base-100
                self.motion=self.motion[:,cut:]
                self.base+=cut
        self.emit(packet)
        return True

    def invalidate(self):
        with self.lock:
            self.epoch+=1
            self.queued_transition_epoch=None;self.queued_transition_loop=None

    def halt(self):
        with self.lock:
            self.playing=False
            self.emit({'type':'play','playing':False,'epoch':self.epoch})

    def reset_motion(self):
        if not self.ready:raise ValueError('Wait until ARDY is ready')
        with self.lock:
            self.epoch+=1;self.playing=False;self.transport_paused=False
            self.motion=None;self.base=0;self.cursor=0;self.features=None;self.prompt='';self.timeline=[]
            self.motion_prepare_serial=getattr(self,'motion_prepare_serial',0)+1
            self.prepared_motion=None;self.motion_preparing=False
            self.last_pose_refresh_cursor=0;self.pose_refresh_count=0
            self.idle_clip=None;self.event_end=None;self.idle_kind='stand';self.anchor_kind='stand'
            self.clear_pose_guard()
            self.emit({'type':'motion_reset','epoch':self.epoch,'skeleton':self.metadata()})

    def state(self):
        with self.lock:
            return {'frame':self.cursor,'epoch':self.epoch,'fps':float(getattr(self,'fps',20)),
                    'motion_start_frame':self.base,'motion_end_frame':self.end,'horizon_frames':self.horizon,
                    'motion_parameters':dict(self.params),'debug':dict(self.metrics),'debug_events':self.debug_events[-25:],
                    'effective_trigger_frames':min(max(self.params['trigger_frames'],math.ceil((self.last_generation_s+.5)*getattr(self,'fps',20))),getattr(self,'fps',20)*10),
                    'timeline':list(self.timeline[-30:]),
                    'position':list(self.position),'playing':self.playing,
                    'buffered_seconds':max(0,self.end-self.cursor)/20,
                    'backend_ready':self.ready,'backend_error':self.error,'prompt':self.prompt,
                    'text_encoder':self.encoder.encoder.backend_status() if self.encoder and hasattr(self.encoder.encoder,'backend_status') else None,
                    'behavior_state':self.event_state(),**self.environment()}

    def feedback(self,data):
        with self.lock:
            if data.get('epoch')!=self.epoch:return False
            frame=int(data.get('frame',self.cursor))
            if self.cursor<=frame<=self.end: self.cursor=frame
            position=data.get('position')
            if isinstance(position,list) and len(position)==3 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in position):
                self.position=position
            return True

    def snapshot_motion(self):
        with self.lock:
            if self.motion is None:return None
            start=max(self.base,self.cursor-4)
            packet=self._packet(start,self.motion[:,start-self.base:])
            packet['cursor']=self.cursor
            return packet

    def begin(self,step,is_current):
        if getattr(step,'action','') in ('idle_stand','idle_sit','idle_kneel','idle_lie'):
            return self.begin_cached_idle(step.action[5:],step,is_current)
        prepared=self.take_prepared_motion(step,is_current)
        if prepared is not None:return prepared
        loop=getattr(step,'kind','one_shot')=='loop'
        mode_two=self.params.get('behavior_mode')=='event'
        terminal=mode_two and loop and step.action in ('terminal-idle','terminal-hold')
        hold_pose=None
        if terminal and self.motion is not None:
            with self.lock:
                hold_pose=(self.pose_snapshot if self.pose_snapshot is not None else self.motion[:,min(self.end,max(self.base,self.cursor))-self.base:][:,:1]).clone()
        if not mode_two and self.static_request(step):return self.begin_event(step,is_current)
        resting=step.action in ('terminal-idle','policy-rest','idle','stand','sit','kneel','lie')
        if not mode_two and loop and resting and self.event_mode_active():
            if self.idle_clip is not None and (step.after_idle=='keep' or step.after_idle==self.idle_kind) and self.cursor>=getattr(self,'event_end',self.cursor):
                with self.control_lock:
                    if not is_current():return None
                    self.transport_paused=False;self.playing=True;self.prompt='Cached idle: '+self.idle_kind
                    self.emit({'type':'play','playing':True,'epoch':self.epoch})
                    self.expression(step.emotion or 'neutral',step.look_at,scope='step',face_text=getattr(step,'face_text',None))
                    return self.cursor
            return self.begin_event(step,is_current)
        if not mode_two and self.event_mode_active() and not loop and not getattr(step,'continuous',False):return self.begin_event(step,is_current)
        if not self.ready: raise RuntimeError('ARDY 尚未准备完成')
        self.transport_paused=False
        started=time.monotonic()
        restart_anchor=None
        if self.params.get('restart_from_now', False) and not self.params.get('behavior_mode'):
            with self.generation_lock:
                with self.control_lock:
                    if not is_current():return None
                    with self.lock:
                        restart_anchor=min(self.cursor,self.end)
                        self.playing=False
                        if self.motion is not None:
                            self.motion=self.motion[:,:restart_anchor-self.base+1].clone()
                            self.cursor=restart_anchor
                        self.timeline=[item for item in self.timeline if item['start']<=restart_anchor]
                        self.emit({'type':'motion_trim','frame':restart_anchor,'epoch':self.epoch})
                        self.emit({'type':'play','playing':False,'epoch':self.epoch})
            self.record(stage='restart_from_now',restart_anchor_frame=restart_anchor)
        hit=self.encoder.cache.contains(step.motion_prompt)
        words=len(step.motion_prompt.split())
        paragraphs=len([p for p in step.motion_prompt.split('\n') if p.strip()])
        self.record(stage='kimodo_encoding',text_cache_hit=hit,prompt=step.motion_prompt,
                    prompt_words=words,prompt_paragraphs=paragraphs,
                    prompt_warning='Prefer one short motion sentence; split phases into steps.' if words>30 or paragraphs>1 else None)
        try:feat,_=self.encode_prompt(step.motion_prompt,is_current)
        except RuntimeError:
            if not is_current():return None
            raise
        if not is_current():return None
        hit=self.metrics.get('text_cache_hit',hit)
        encode_ms=round((time.monotonic()-started)*1000,2)
        prepared=self.take_prepared_motion(step,is_current)
        if prepared is not None:return prepared
        self.record(stage='motion_generation',kimodo_encode_ms=encode_ms,text_cache_hit=hit)
        if getattr(step,'action',None) and self.metrics.get('first_motion_encode_ms') is None:
            self.record(first_motion_encode_ms=encode_ms,first_motion_cache_hit=hit)
        if not is_current():return None
        with self.generation_lock:
            with self.control_lock:
                if not is_current():return None
                epoch=self.epoch
            old=self.features
            for attempt in range(3):
                if not is_current():return None
                if restart_anchor is not None:
                    anchor=restart_anchor
                else:
                    lead=max(self.params['transition_lead_frames'],math.ceil((self.last_generation_s+.5)*20))
                    if old is not None and self.playing and self.end-self.cursor<lead+4:self._generate(min(self.end,self.cursor+self.params['replan_buffer_frames']),old,epoch)
                    anchor=min(self.end,max(3,self.cursor+(lead if self.playing else 0)))
                if self._generate(anchor,feat,epoch,switch_prompt=step.motion_prompt,is_current=is_current,hold_pose=hold_pose):break
            else:raise RuntimeError('运动生成落后于播放，无法准备未来动作')
            with self.control_lock:
                if not is_current():return None
                self.features=feat
                self.continuous_live=mode_two or getattr(step,'continuous',False) or loop
                if mode_two:
                    self.active_is_static=self.static_intent(step)
                    duration=getattr(step,'duration_s',3)*getattr(step,'repeat',1)
                    self.event_end=None if loop else anchor+1+round((duration+self.params.get('settle_buffer_s',6))*self.fps)
                    self.finite_boundary=self.event_end
                    if terminal:
                        self.pose_pending=None;self.finite_boundary=None
                    elif self.static_request(step):
                        earliest=anchor+1+round((duration+5)*self.fps)
                        self.pose_pending={'earliest_frame':earliest,'deadline_frame':earliest+round(5*self.fps),'posture':'hold'}
                    # No independent anchor, idle synthesis, nearest-idle selection or idle bridge.
                self.playing=not self.transport_paused
                self.expression(getattr(step,'emotion',None) or 'neutral',getattr(step,'look_at','auto'),getattr(step,'look_target',None),scope='step')
                self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})
                self.timeline=[item for item in self.timeline if item['start']<anchor+1]
                self.timeline.append({'start':anchor+1,'end':anchor+1+round(getattr(step,'duration_s',0)*20),
                                      'label':getattr(step,'action','idle'),'prompt':step.motion_prompt,'epoch':self.epoch})
                self.timeline=self.timeline[-30:]
                self.record(stage='scheduled',scheduled_frame=anchor+1,transition_wait_ms=round(max(0,anchor+1-self.cursor)*50),
                            motion_prepare_ms=round((time.monotonic()-started)*1000,2))
                return anchor+1

    def inference_step(self,**kwargs):
        acceleration=getattr(self,'acceleration',None)
        device=next(self.model.parameters()).device
        if device.type=='cuda':torch.cuda.synchronize(device)
        started=time.monotonic()
        result=acceleration.step(**kwargs) if acceleration else self.model.autoregressive_step(**kwargs)
        if device.type=='cuda':torch.cuda.synchronize(device)
        inference_ms=(time.monotonic()-started)*1000
        history=kwargs.get('init_history_sequence');history_length=0 if history is None else history.shape[1]
        self.record(motion_inference_ms=round(inference_ms,2),motion_history_frames=history_length,
                    motion_generated_frames=kwargs['num_frames']-history_length,motion_denoising_steps=kwargs['num_denoising_steps'],
                    motion_inference_backend=(self.metrics.get('inference_call_backend') if acceleration else 'none'),
                    motion_generation_ms=round(inference_ms,2))
        if self.params.get('official_postprocess',False) and not getattr(self,'postprocess_failed',False):
            try:
                postprocess_started=time.monotonic()
                from ardy.postprocess import post_process_motion
                history=kwargs.get('init_history_sequence');length=0 if history is None else history.shape[1]
                rep=self.model.motion_rep
                predicted=rep.inverse(result[:,length:],is_normalized=True)
                contacts=rep.unnormalize(result[:,length:])[:,:,rep.slice_dict['foot_contacts']]
                corrected=post_process_motion(predicted['local_rot_mats'],predicted['root_positions'],contacts,self.skeleton)
                rebuilt=rep(local_joint_rots=corrected['local_rot_mats'],root_positions=corrected['root_positions'],to_normalize=True)
                if not torch.isfinite(rebuilt).all():raise RuntimeError('Non-finite corrected motion')
                result=torch.cat([result[:,:length],rebuilt],dim=1)
                self.record(official_postprocess_active=True,official_postprocess_ms=round((time.monotonic()-postprocess_started)*1000,2),official_postprocess_error=None)
            except Exception as exc:
                self.postprocess_failed=True
                self.record(official_postprocess_active=False,official_postprocess_error=type(exc).__name__+': '+str(exc)[:160])
        self.record(motion_generation_ms=round((time.monotonic()-started)*1000,2))
        return result

    def encode_prompt(self,prompt,valid=None,*,priority=0):
        valid=valid or (lambda:True)
        if not hasattr(self,'text_scheduler'):self.text_scheduler=TextScheduler()
        def timing(**data):
            if priority==0:self.record(text_queue_wait_ms=data['queue_ms'],
                                      text_encode_compute_ms=data['compute_ms'],text_cache_hit=data['cache_hit'])
        return self.text_scheduler.encode(self.encoder,prompt,valid,priority,on_timing=timing)

    def prefetch_prompts(self,steps):
        if not self.ready or self.encoder is None:return
        # Speculation is bounded to one real upcoming action, not the whole plan
        # or the automatically inserted terminal hold.
        prompts=list(dict.fromkeys(step.motion_prompt for step in steps
                     if step.action not in ('terminal-hold','terminal-idle')))[:1]
        with self.lock:
            active=getattr(self,'prefetch_active_prompt',None)
            if getattr(self,'prefetch_running',False) and (
                    prompts==getattr(self,'prefetch_signature',None) or active in prompts):
                # Appending conversational reactions must not cancel a slow
                # encoding already needed by the current/waiting body task.
                self.prefetch_signature=prompts
                self.prefetch_queue=[p for p in prompts if p!=active]
                return
            self.prefetch_serial=getattr(self,'prefetch_serial',0)+1
            serial=self.prefetch_serial
            self.prefetch_signature=prompts;self.prefetch_queue=list(prompts)
            self.prefetch_running=True;self.prefetch_active_prompt=None
        def prepare():
            try:
                while True:
                    with self.lock:
                        if getattr(self,'closed',False) or serial!=self.prefetch_serial:return
                        if not self.prefetch_queue:
                            self.prefetch_running=False;self.prefetch_active_prompt=None
                            return
                        prompt=self.prefetch_queue.pop(0);self.prefetch_active_prompt=prompt
                    if self.encoder.cache.contains(prompt):continue
                    started=time.monotonic()
                    try:
                        self.encode_prompt(prompt,lambda:not getattr(self,'closed',False) and serial==self.prefetch_serial,priority=2)
                        self.record(prefetch_prompt=prompt,prefetch_ms=round((time.monotonic()-started)*1000,2))
                    except TextPreparationPreempted:
                        with self.lock:
                            if serial==self.prefetch_serial and prompt not in self.prefetch_queue:self.prefetch_queue.insert(0,prompt)
                        continue
                    except Exception as exc:
                        self.record(prefetch_error=type(exc).__name__);return
            finally:
                with self.lock:
                    if serial==self.prefetch_serial:
                        self.prefetch_running=False;self.prefetch_active_prompt=None
        threading.Thread(target=prepare,daemon=True).start()

    def cancel_text_prefetch(self):
        with self.lock:
            self.prefetch_serial=getattr(self,'prefetch_serial',0)+1
            self.prefetch_queue=[];self.prefetch_signature=None
            self.prefetch_running=False;self.prefetch_active_prompt=None

    def finish_event(self,next_step=None):
        """Continue the current condition until a queued action is ready to join."""
        if self.params.get('behavior_mode')!='event':return
        # Epoch invalidation makes any in-flight continuation stale, without taking the
        # generation lock under the controller lock.
        with self.lock:
            if self.motion is None or not self.playing:return
            prepared=getattr(self,'prepared_motion',None)
            ready=(prepared is not None and prepared['step'] is next_step
                   and prepared['valid']() and prepared['signature']==self.params)
            if ready and next_step is not None and next_step.action not in ('terminal-hold','terminal-idle'):
                # The controller will consume this segment on its next tick.
                # Do not replace the outgoing motion with a frozen epoch first.
                self.arm_queued_transition(next_step)
                self.record(stage='event_handoff_ready',waiting_action=next_step.action)
                return
            if next_step is not None and next_step.action not in ('terminal-hold','terminal-idle') and not ready:
                self.arm_queued_transition(next_step)
                self.waiting_for_next=True;self.waiting_motion_live=True
                self.record(stage='event_wait_continuation',waiting_action=next_step.action)
                return
            frame=min(self.end,max(self.base,self.cursor))
            pose=self.motion[:,frame-self.base:frame-self.base+1]
            if self.features is not None:self.pose_live_features=self.features
            self.pose_pending=None;self.active_is_static=True;self.waiting_for_next=True;self.waiting_motion_live=True
            self.pose_snapshot=pose.clone();self.pose_guard='anchor';self.pose_guard_frame=frame
            self.pose_condition_prompt=self.prompt
            self.arm_queued_transition(next_step or SimpleNamespace(action='terminal-hold'))
            self.idle_clip=None
            self.record(stage='event_terminal_continuation',waiting_pose_frame=frame)

    def record(self,**values):
        if self.trace:self.trace.record('motion_debug', values)
        with self.lock:
            self.metrics.update(values)
            self.debug_events.append({'time':round(time.time(),3),**values})
            self.debug_events=self.debug_events[-50:]

    def configure(self,data):
        if not self.ready:raise ValueError('Wait until ARDY is ready')
        new=validate(data,self.params)
        if new.get('official_postprocess')!=self.params.get('official_postprocess'):self.postprocess_failed=False
        if new==self.params:return dict(new)
        if new['model']!=self.params['model'] or new.get('inference_backend')!=self.params.get('inference_backend'):
            self.ready=False;self.halt()
            self.emit({'type':'backend_loading'})
            threading.Thread(target=self._reload,args=(new,),daemon=True).start()
        else:
            with self.generation_lock:
                with self.lock:
                    if new['seed']!=self.params['seed']:torch.manual_seed(int(new['seed']))
                    if new.get('refresh_last_pose',False) and not self.params.get('refresh_last_pose',False):self.last_pose_refresh_cursor=self.cursor
                    if any(new[k]!=self.params.get(k) for k in ('seed','denoising_steps','cfg_text','cfg_constraint')):
                        self.idle_cache={}
                    if new.get('behavior_mode')!=self.params.get('behavior_mode'):
                        active_static=self.active_is_static
                        if self.features is None and self.pose_live_features is not None:
                            self.features=self.pose_live_features;self.prompt=self.pose_condition_prompt
                        self.idle_clip=None;self.event_end=None
                        self.clear_pose_guard();self.active_is_static=active_static
                    previous=self.params
                    self.params=new
                    if new.get('static_protection')!=previous.get('static_protection') or new.get('behavior_mode')!=previous.get('behavior_mode'):self.update_active_protection(previous)
                    save_settings(new)
        return new

    def _reload(self,new):
        backup=None
        try:
            with self.generation_lock:
                backup={key:getattr(self,key) for key in ('model','params','horizon','motion','base','cursor','timeline','skeleton','fps','acceleration')}
                model=load_model(new['model'],device='cuda:0' if torch.cuda.is_available() else 'cpu',text_encoder=self.encoder)
                acceleration=AcceleratedInference(model,new,self.record)
                with self.lock:
                    self.epoch+=1;self.model=model;self.params=new
                    self.acceleration=acceleration
                    self.horizon=40 if new['model']=='core40' else 8
                    self.motion=None;self.base=0;self.cursor=0;self.timeline=[]
                    self.skeleton=model.motion_rep.skeleton;self.fps=model.motion_rep.fps
                    self.init_events()
                    self.motion_prepare_serial=getattr(self,'motion_prepare_serial',0)+1
                    self.prepared_motion=None;self.motion_preparing=False
                    torch.manual_seed(int(new['seed']))
                if self.features is not None:
                    self._generate(-1,self.features,self.epoch)
                self.ready=True;save_settings(new)
                self.emit({'type':'backend_ready','skeleton':self.metadata(),'reset':True})
                packet=self.snapshot_motion()
                if packet is not None:self.emit(packet)
                self.playing=self.motion is not None;self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})
        except Exception as exc:
            with self.lock:
                if backup:
                    for key,value in backup.items():setattr(self,key,value)
                self.epoch+=1;self.playing=False
            self.ready=True
            self.record(stage='reload_error',error=type(exc).__name__)
            self.emit({'type':'backend_error','error':'Model reload failed: '+type(exc).__name__})
            self.emit({'type':'backend_ready','skeleton':self.metadata(),'reset':True})
            packet=self.snapshot_motion()
            if packet is not None:self.emit(packet)
            self.emit({'type':'play','playing':False,'epoch':self.epoch})

    def playback(self,action,frame=None):
        with self.lock:
            if not self.ready:raise ValueError('ARDY is not ready')
            if action=='seek':
                if type(frame) is not int or not self.base<=frame<=self.end:raise ValueError('Frame outside retained range')
                self.cursor=frame;self.playing=False
                self.transport_paused=True
                self.emit(self.snapshot_motion())
                self.emit({'type':'seek','frame':frame,'epoch':self.epoch})
            elif action in ('play','pause'):
                if action=='play' and self.motion is None:raise ValueError('Generate a manual motion first')
                self.playing=action=='play';self.transport_paused=action=='pause'
            else:raise ValueError('Unknown playback action')
            self.emit({'type':'play','playing':self.playing,'epoch':self.epoch})

    def velocity(self,x,z):
        # Precise navigation is not exposed in phase 1. Root movement is ARDY-generated.
        pass

    def expression(self,emotion,look_at=None,look_target=None,scope='plan',face_text=None):
        self.face=emotion
        if scope=='manual' and look_at in ('user','none'):self.gaze_mode=look_at
        packet={'type':'expression','emotion':emotion}
        if face_text:packet['face_text']=face_text
        if look_at is not None:packet.update(look_at=look_at,look_target=look_target,gaze_scope=scope)
        self.emit(packet)

    def idle(self,is_current):
        """Settle to generated standing without spending another LLM request."""
        def settle():
            time.sleep(.5)
            if not self.owner or not is_current() or not self.ready or self.transport_paused:return
            step=SimpleNamespace(motion_prompt='A person is standing.',emotion=self.face,look_at='auto')
            try:self.begin(step,is_current)
            except Exception as exc:self.emit({'type':'backend_error','error':'待机准备失败：'+type(exc).__name__})
        threading.Thread(target=settle,daemon=True).start()

    def publish(self,snapshot,chat):
        self.on_publish(snapshot,chat)

    def close(self):
        self.closed=True
        self.halt()
        if self.encoder:self.encoder.encoder.close()
