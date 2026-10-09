import json,runpy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
import torch
ROOT=Path(__file__).resolve().parents[1]
old=runpy.run_path(str(ROOT/'scripts/test-motion-switch.py'))
pose=runpy.run_path(str(ROOT/'scripts/test-static-protection.py'))
from core import Step
import sys
sys.path.insert(0,str(ROOT/'packages/observability'))
from runtime_trace import TraceStore

class Tests(unittest.TestCase):
    def worker(self):
        w=old['Tests']().make_worker();w.init_events();w.fps=20
        w.params.update(behavior_mode='event',settle_buffer_s=6,static_protection='freeze')
        w.motion=pose['clip'](80);w.calls=0
        class Model:
            motion_rep=pose['Rep']()
            def parameters(self):return iter([torch.zeros(1)])
            def autoregressive_step(self,**kw):
                w.calls+=1
                return pose['clip'](kw['num_frames'])
        w.model=Model()
        w.encoder=SimpleNamespace()
        class Encoder:
            cache=SimpleNamespace(contains=lambda text:True)
            def __call__(self,texts):return torch.zeros(1,2,3),None
        w.encoder=Encoder()
        def forbidden(*a,**kw):raise AssertionError('Mode two must not generate a preset idle or independent event')
        w.idle_for=forbidden;w.begin_event=forbidden
        return w
    def test_first_chunk_without_pregenerated_idle_or_settling(self):
        w=self.worker();start=w.begin(Step(action='sit',pose_intent='static'),lambda:True)
        self.assertEqual(w.calls,1);self.assertTrue(w.playing)
        self.assertIsNone(w.idle_clip);self.assertIsNotNone(w.pose_pending)
        self.assertEqual(w.event_end,start+180)
        self.assertGreater(w.pose_pending['earliest_frame'],w.end)
    def test_observation_happens_in_continuation_with_real_trace(self):
        w=self.worker()
        with tempfile.TemporaryDirectory() as directory:
            w.trace=TraceStore(directory,kind='test')
            start=w.begin(Step(action='stand',kind='loop',pose_intent='static'),lambda:True)
            self.assertEqual(w.pose_guard,'off')
            while w.pose_pending:w._generate(w.end,w.features,w.epoch)
            self.assertEqual(w.pose_guard,'freeze');self.assertIsNone(w.features)
            self.assertGreaterEqual(w.pose_guard_frame,start+160)
            json.dumps(w.metrics,allow_nan=False)
            self.assertIs(type(w.stable_pose_window(pose['clip']())),bool)
            w.trace.db.close()
    def test_dynamic_loop_never_arms_snapshot(self):
        w=self.worker();w.begin(Step(action='walk',kind='loop',pose_intent='dynamic'),lambda:True)
        self.assertEqual(w.calls,1);self.assertIsNone(w.pose_pending);self.assertIsNone(w.idle_clip)
    def test_static_sway_stops_at_deadline(self):
        w=self.worker();w.begin(Step(action='stand',kind='loop',pose_intent='static'),lambda:True)
        w.stable_pose_window=lambda _:False
        deadline=w.pose_pending['deadline_frame']
        while w.end<deadline:w._generate(w.end,w.features,w.epoch)
        self.assertEqual(w.pose_guard,'freeze');self.assertIsNone(w.features)
        self.assertEqual(w.metrics['static_capture_reason'],'static_settling_limit')

    def test_active_method_switch_freezes_then_resumes(self):
        w=self.worker();w.begin(Step(action='stand',kind='loop',pose_intent='static'),lambda:True)
        while w.pose_pending:w._generate(w.end,w.features,w.epoch)
        w.params['static_protection']='anchor';w.update_active_protection({})
        self.assertIsNotNone(w.features);self.assertIsNone(w.idle_clip);self.assertEqual(w.pose_guard,'anchor')
        w.params['static_protection']='freeze';w.update_active_protection({})
        self.assertIsNone(w.features);self.assertEqual(w.pose_guard,'freeze')
        self.assertLessEqual(w.pose_guard_frame,w.cursor)

    def test_auto_static_legacy_prompt(self):
        w=self.worker()
        self.assertTrue(w.static_intent(Step(action='motion',prompt='A person remains seated on the spot, breathing naturally with subtle upper body sway.',kind='loop')))
        self.assertFalse(w.static_intent(Step(action='motion',prompt='A person keeps waving their hand.',kind='loop')))

    def test_scheduler_keeps_buffer_and_does_not_add_standing_idle(self):
        plan=runpy.run_path(str(ROOT/'scripts/test-plan-progress.py'))
        c,m=plan['Tests']().make();m.params={'behavior_mode':'event'}
        original=m.state
        m.state=lambda:dict(original(),behavior_state={'configured_mode':'event','enabled':False,'event_end_frame':160})
        c.install_plan([Step(action='wave',duration_s=2,after_idle='stand')],'wave')
        self.assertEqual(c.queue[-1].action,'terminal-hold')
        self.assertNotIn('standing',c.queue[-1].motion_prompt)
        c.tick();m.frame=50;c.tick();self.assertEqual(c.current.action,'wave')
        m.frame=160;c.tick();c.tick();self.assertEqual(c.current.action,'terminal-hold')

    def test_explicit_stand_releases_protection_and_starts_new_action(self):
        w=self.worker();w.capture_pose(pose['clip'](),10)
        w.begin(Step(action='stand',kind='loop',pose_intent='static'),lambda:True)
        self.assertEqual(w.pose_guard,'off');self.assertIsNotNone(w.pose_pending)

if __name__=='__main__':unittest.main()
