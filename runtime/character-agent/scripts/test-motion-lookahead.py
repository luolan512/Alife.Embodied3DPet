"""Queue lookahead respects endpoint, invalidation and actual handoff pose."""
import runpy,time,unittest,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
helper=runpy.run_path(str(ROOT/'scripts/test-mode-two-live.py'))
from core import Step
from settings import DEFAULTS

class Tests(unittest.TestCase):
 def worker(self):
  w=helper['Tests']().worker();w.init_lookahead();w.closed=False;return w
 def ready(self,w):
  end=time.monotonic()+3
  while w.motion_preparing and time.monotonic()<end:time.sleep(.01)
  self.assertFalse(w.motion_preparing);self.assertIsNotNone(w.prepared_motion)
 def test_prepares_future_motion_and_reuses_without_inference(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  boundary=w.finite_boundary;target=Step(action='look_around');w.prepare_next_step(target,lambda:True);self.ready(w)
  self.assertEqual(w.end,boundary);self.assertEqual(w.prompt,Step(action='wave').motion_prompt)
  calls=w.calls
  self.assertFalse(w._generate(w.cursor+24,w.features,w.epoch),'prepared endpoint must not be regenerated')
  self.assertEqual(w.calls,calls)
  w.cursor=boundary;w.finish_event()
  self.assertIsNotNone(w.begin(target,lambda:True));self.assertEqual(w.calls,calls)
  self.assertEqual(w.prompt,target.motion_prompt);self.assertEqual(w.timeline[-1]['label'],'look_around')
 def test_changed_pose_discards_prediction(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  target=Step(action='look_around');w.prepare_next_step(target,lambda:True);self.ready(w)
  w.motion=w.motion.clone();w.motion[:,:,:3]+=1
  self.assertIsNone(w.take_prepared_motion(target,lambda:True));self.assertIsNone(w.prepared_motion)
 def test_cancelled_queue_never_consumes_old_segment(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  valid=[True];target=Step(action='look_around');w.prepare_next_step(target,lambda:valid[0]);self.ready(w)
  valid[0]=False;self.assertFalse(w.lookahead_state()['ready'])
  self.assertIsNone(w.take_prepared_motion(target,lambda:True))
 def test_infinite_loop_keeps_live_generation(self):
  w=self.worker();w.begin(Step(action='walk',kind='loop',pose_intent='dynamic'),lambda:True)
  w.prepare_next_step(Step(action='wave'),lambda:True)
  self.assertFalse(w.motion_preparing);self.assertIsNone(w.finite_boundary)
  end=w.end;self.assertTrue(w._generate(w.end,w.features,w.epoch));self.assertGreater(w.end,end)
 def test_slow_next_action_keeps_old_motion_live_until_commit(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  while w.end<w.finite_boundary:w._generate(w.end,w.features,w.epoch)
  w.cursor=w.finite_boundary;epoch=w.epoch;old_prompt=w.prompt;target=Step(action='look_around')
  w.finish_event(next_step=target)
  self.assertTrue(w.waiting_motion_live);self.assertIsNotNone(w.features);self.assertIsNone(w.finite_boundary)
  entered=threading.Event();release=threading.Event();encode=w.encode_prompt;result=[]
  def delayed(prompt,valid=None,**kwargs):entered.set();release.wait(2);return encode(prompt)
  w.encode_prompt=delayed
  thread=threading.Thread(target=lambda:result.append(w.begin(target,lambda:True)));thread.start()
  try:
   self.assertTrue(entered.wait(1));end=w.end
   self.assertTrue(w._generate(w.end,w.features,w.epoch));self.assertGreater(w.end,end)
   self.assertEqual(w.epoch,epoch);self.assertEqual(w.prompt,old_prompt)
  finally:release.set();thread.join(3)
  self.assertTrue(result);self.assertEqual(w.prompt,target.motion_prompt);self.assertFalse(w.waiting_motion_live)
 def test_prepared_next_action_still_joins_at_original_endpoint(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  target=Step(action='look_around');w.prepare_next_step(target,lambda:True);self.ready(w)
  w.cursor=w.finite_boundary;w.finish_event(next_step=target)
  self.assertFalse(w.waiting_motion_live);calls=w.calls
  self.assertIsNotNone(w.begin(target,lambda:True));self.assertEqual(w.calls,calls)
 def test_ready_handoff_does_not_freeze_or_replace_epoch(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  target=Step(action='look_around');w.prepare_next_step(target,lambda:True);self.ready(w)
  w.cursor=w.finite_boundary;epoch=w.epoch;features=w.features;motion=w.motion
  w.finish_event(next_step=target)
  self.assertEqual(w.epoch,epoch);self.assertIs(w.features,features);self.assertIs(w.motion,motion)
  self.assertIsNone(w.pose_snapshot);self.assertIsNotNone(w.take_prepared_motion(target,lambda:True))
 def test_slow_handoff_buffers_before_endpoint_without_finishing_early(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  target=Step(action='look_around');boundary=w.finite_boundary
  while w.end<boundary:w._generate(w.end,w.features,w.epoch)
  w.cursor=boundary-10;w.buffer_queued_transition(target)
  self.assertIsNone(w.finite_boundary);self.assertEqual(w.event_end,boundary)
  while w.end<=boundary:self.assertTrue(w._generate(w.end,w.features,w.epoch))
  self.assertGreater(w.end,boundary);self.assertIsNone(w.pose_snapshot)
 def test_terminal_hold_buffers_with_anchor_but_pause_keeps_cap(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  boundary=w.finite_boundary;w.cursor=boundary-2
  w.buffer_queued_transition(Step(action='terminal-hold',kind='loop',prompt='A person holds their current pose.'))
  self.assertIsNone(w.finite_boundary);self.assertEqual(w.event_end,boundary);self.assertEqual(w.pose_guard,'anchor')
  w.finite_boundary=boundary;w.playing=False;w.buffer_queued_transition(Step(action='look_around'))
  self.assertEqual(w.finite_boundary,boundary)
 def test_ready_segment_still_has_outgoing_buffer_during_commit(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  target=Step(action='look_around');w.prepare_next_step(target,lambda:True);self.ready(w)
  boundary=w.finite_boundary;w.cursor=boundary-10;w.buffer_queued_transition(target)
  self.assertIsNone(w.finite_boundary);self.assertEqual(w.event_end,boundary)
  self.assertTrue(w._generate(w.end,w.features,w.epoch));self.assertGreater(w.end,boundary)
  w.cursor=boundary;self.assertIsNotNone(w.begin(target,lambda:True))
 def test_next_text_warms_before_waiting_for_distant_endpoint(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=30),lambda:True)
  valid=[True];encoded=threading.Event();encode=w.encode_prompt
  def observe(prompt,valid=None,**kwargs):encoded.set();return encode(prompt)
  w.encode_prompt=observe;w.prepare_next_step(Step(action='look_around'),lambda:valid[0])
  self.assertTrue(encoded.wait(1));self.assertIsNone(w.prepared_motion)
  valid[0]=False
  end=time.monotonic()+2
  while w.motion_preparing and time.monotonic()<end:time.sleep(.01)
  self.assertFalse(w.motion_preparing)
 def test_cached_idle_opt_in_does_not_remove_dynamic_loops(self):
  plan=runpy.run_path(str(ROOT/'scripts/test-plan-progress.py'))['Tests'];c,m=plan().make();m.params={'behavior_mode':'event'}
  self.assertFalse(DEFAULTS['allow_cached_idle_loops'])
  self.assertEqual(c.with_terminal_loop([Step(action='idle_stand')])[0].action,'stand')
  step=Step(action='walk',kind='loop',pose_intent='dynamic')
  self.assertIs(c.with_terminal_loop([step])[0],step)
  m.params['allow_cached_idle_loops']=True
  self.assertEqual(c.with_terminal_loop([Step(action='idle_stand')])[0].action,'idle_stand')

 def test_frozen_intermediate_task_resumes_with_pose_bounds(self):
  w=self.worker();w.begin(Step(action='sit',pose_intent='static'),lambda:True)
  w.capture_pose(w.motion[:,-1:],w.end)
  self.assertEqual(w.pose_guard,'freeze');self.assertIsNone(w.features)
  saved=w.pose_live_features;pose=w.pose_snapshot.clone();epoch=w.epoch
  target=Step(action='wave');w.finish_event(next_step=target)
  self.assertEqual(w.epoch,epoch);self.assertEqual(w.pose_guard,'anchor')
  self.assertIs(w.features,saved);self.assertIsNone(w.idle_clip)
  self.assertTrue(w.waiting_motion_live);self.assertIsNone(w.finite_boundary)
  self.assertTrue(w._generate(w.end,w.features,w.epoch))
  torch=helper['torch'];torch.testing.assert_close(w.pose_snapshot,pose)

 def test_gpu_busy_wait_uses_tail_without_inference_or_root_teleport(self):
  w=self.worker();torch=helper['torch']
  w.motion=helper['pose']['clip'](80,2,True);w.motion[:,:,0]=torch.arange(80)*.01
  w.motion[:,:,2]=3;w.cursor=w.end-2;w.prompt='Outgoing motion'
  w.arm_queued_transition(Step(action='wave'));calls=w.calls;epoch=w.epoch
  self.assertFalse(w.pump_queued_transition(),'live generation has priority when GPU is free')
  end=w.end;last=w.model.motion_rep.inverse(w.motion[:,-1:])['root_positions'].clone()
  with w.generation_lock:self.assertTrue(w.pump_queued_transition())
  self.assertGreater(w.end,end);self.assertEqual(w.calls,calls);self.assertEqual(w.epoch,epoch)
  self.assertEqual(w.prompt,'Outgoing motion')
  appended=w.model.motion_rep.inverse(w.motion[:,end+1:])
  torch.testing.assert_close(appended['root_positions'][:,:1],last)
  self.assertTrue(torch.isfinite(w.motion).all())

 def test_wait_loop_stops_on_pause_cancel_or_new_task(self):
  w=self.worker();w.arm_queued_transition(Step(action='wave'));w.cursor=w.end-1
  with w.generation_lock:
   w.transport_paused=True;self.assertFalse(w.pump_queued_transition())
   w.transport_paused=False;w.playing=False;self.assertFalse(w.pump_queued_transition())
   w.playing=True;w.invalidate();self.assertFalse(w.pump_queued_transition())
  w.arm_queued_transition(Step(action='wave'));w.clear_pose_guard()
  with w.generation_lock:self.assertFalse(w.pump_queued_transition())

 def test_final_task_continues_with_anchor_and_terminal_keeps_bounds(self):
  w=self.worker();w.begin(Step(action='wave'),lambda:True)
  terminal=Step(action='terminal-hold',kind='loop',prompt='A person holds the current pose.')
  w.prepare_next_step(terminal,lambda:True)
  self.assertFalse(w.motion_preparing);self.assertIsNone(w.prepared_motion)
  w.finish_event(next_step=terminal)
  self.assertEqual(w.pose_guard,'anchor');self.assertIsNotNone(w.features)
  self.assertIsNotNone(w.queued_transition_loop);self.assertIsNone(w.finite_boundary)
  snapshot=w.pose_snapshot.clone();end=w.end
  self.assertTrue(w._generate(w.end,w.features,w.epoch));self.assertGreater(w.end,end)
  w.begin(terminal,lambda:True)
  self.assertEqual(w.pose_guard,'anchor');self.assertIsNone(w.pose_pending);self.assertIsNone(w.finite_boundary)
  helper['torch'].testing.assert_close(w.pose_snapshot,snapshot)

 def test_empty_buffer_refills_at_tail_without_overwriting_future(self):
  w=self.worker();w.owner=True;w.params.update(auto_replan=True,trigger_frames=40)
  w.begin(Step(action='walk',kind='loop',pose_intent='dynamic'),lambda:True)
  w.cursor=w.end;before=w.motion.clone();end=w.end
  self.assertTrue(w.refill_playback());self.assertEqual(w.end,end+40)
  helper['torch'].testing.assert_close(w.motion[:,:before.shape[1]],before)
  w.transport_paused=True;self.assertFalse(w.refill_playback())

 def test_busy_empty_buffer_gets_recovery_without_queue(self):
  w=self.worker();w.owner=True;w.begin(Step(action='walk',kind='loop',pose_intent='dynamic'),lambda:True)
  w.cursor=w.end;end=w.end;calls=w.calls
  with w.generation_lock:self.assertTrue(w.pump_queued_transition(allow_unqueued=True))
  self.assertGreater(w.end,end);self.assertEqual(w.calls,calls);self.assertEqual(w.metrics['stage'],'buffer_recovery_loop')
  w.cursor=w.end;w.transport_paused=True
  with w.generation_lock:self.assertFalse(w.pump_queued_transition(allow_unqueued=True))

 def test_terminal_long_continuation_cannot_accumulate_pose_drift(self):
  w=self.worker();torch=helper['torch'];w.owner=True;w.params.update(trigger_frames=40,auto_replan=True)
  w.begin(Step(action='wave'),lambda:True);w.cursor=w.end;w.finish_event()
  terminal=Step(action='terminal-hold',kind='loop',prompt='A person holds the current pose.')
  w.begin(terminal,lambda:True);reference=w.pose_snapshot.clone()
  def drifting(**kw):
   seq=helper['pose']['clip'](kw['num_frames'],60)
   seq[:,:,:3]=5
   return seq
  w.model.autoregressive_step=drifting
  for _ in range(300):
   w.cursor=w.end;self.assertTrue(w.refill_playback())
   roots=w.model.motion_rep.inverse(w.motion[:,-40:])['root_positions']
   base=w.model.motion_rep.inverse(reference)['root_positions']
   self.assertLessEqual((roots-base).norm(dim=-1).max().item(),.03001)
   self.assertLessEqual(w.motion.shape[1],241)
  torch.testing.assert_close(w.pose_snapshot,reference)
  self.assertEqual(w.pose_guard,'anchor');self.assertIsNotNone(w.features)

 def test_queued_settling_cannot_freeze_generator_before_next_task(self):
  w=self.worker();w.begin(Step(action='sit',pose_intent='static'),lambda:True)
  boundary=w.event_end;w.arm_queued_transition(Step(action='wave'))
  w.capture_pose(w.motion[:,-1:],w.end)
  self.assertEqual(w.pose_guard,'anchor');self.assertIsNotNone(w.features)
  self.assertEqual(w.event_end,boundary)

 def test_transition_reserve_keeps_logical_completion_frame(self):
  w=self.worker();w.begin(Step(action='wave',duration_s=1),lambda:True)
  boundary=w.event_end;w.cursor=boundary-2
  w.buffer_queued_transition(Step(action='look_around'))
  self.assertEqual(w.event_end,boundary);self.assertIsNone(w.finite_boundary)
  w.finish_event(next_step=Step(action='look_around'))
  self.assertEqual(w.event_end,boundary)

 def test_settling_during_queued_wait_does_not_move_task_deadline(self):
  w=self.worker();w.begin(Step(action='sit',pose_intent='static'),lambda:True)
  boundary=w.event_end;w.arm_queued_transition(Step(action='wave'))
  w.pose_pending={'earliest_frame':0,'deadline_frame':0,'posture':'hold'}
  w.protect_generated(helper['pose']['clip'](),w.end)
  self.assertEqual(w.pose_guard,'anchor');self.assertEqual(w.event_end,boundary)

if __name__=='__main__':unittest.main()
