"""Exercise actual worker commit logic with a delayed CPU-only model/encoder."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'services'/'ardy-worker'), str(ROOT/'packages'/'brain')]
from worker import ArdyWorker
from core import Step


class Tests(unittest.TestCase):
    def make_worker(self):
        w = object.__new__(ArdyWorker)
        w.lock = threading.RLock()
        w.control_lock = threading.RLock()
        w.generation_lock = threading.Lock()
        w.ready = w.playing = True
        w.transport_paused = False
        w.epoch = 7
        w.base = 0
        w.cursor = 20
        w.motion = torch.zeros(1, 80, 3)
        w.features = torch.zeros(1, 2, 3)
        w.prompt = 'Old motion.'
        w.position = [0, .9, 0]
        w.horizon = 40
        w.last_generation_s = .2
        w.trace = None
        w.metrics = {}
        w.debug_events = []
        w.timeline = []
        w.params = dict(history_frames=16, denoising_steps=1, cfg_text=2,
                        cfg_constraint=2, transition_lead_frames=20, replan_buffer_frames=24)
        w.outputs = []
        w.emit = w.outputs.append
        w._packet = lambda start, seq: dict(type='motion', start=start, epoch=w.epoch,
                                          prompt=w.prompt, roots=seq[0].tolist())
        return w

    def test_old_feedback_stays_valid_during_encoding_and_generation(self):
        w = self.make_worker()
        encoding, encoded, generating, generated = [threading.Event() for _ in range(4)]
        class Encoder:
            cache = SimpleNamespace(contains=lambda text: False)
            def __call__(self, texts):
                encoding.set()
                if not encoded.wait(2): raise RuntimeError('encoder timeout')
                return torch.ones(1, 2, 3), None
        class Model:
            def parameters(self): return iter([torch.zeros(1)])
            def autoregressive_step(self, **kw):
                generating.set()
                if not generated.wait(2): raise RuntimeError('model timeout')
                return torch.ones(1, kw['num_frames'], 3)
        w.encoder, w.model = Encoder(), Model()
        result, errors = [], []
        def begin():
            try: result.append(w.begin(Step(prompt='A woman waves.', action='motion'), lambda: True))
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=begin)
        thread.start()
        try:
            self.assertTrue(encoding.wait(1))
            self.assertEqual(w.epoch, 7)
            self.assertTrue(w.feedback(dict(epoch=7, frame=25)))
            self.assertEqual(w.prompt, 'Old motion.')
            encoded.set()
            self.assertTrue(generating.wait(1))
            self.assertTrue(w.feedback(dict(epoch=7, frame=30)))
            self.assertTrue(w.playing)
            self.assertEqual(w.prompt, 'Old motion.')
            generated.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, [46])
            self.assertEqual(w.epoch, 8)
            self.assertEqual(w.prompt, 'A woman waves.')
            self.assertTrue(w.feedback(dict(epoch=8, frame=46)))
            self.assertFalse(w.feedback(dict(epoch=7, frame=47)))
            packet = next(p for p in w.outputs if p['type']=='motion')
            self.assertEqual((packet['epoch'], packet['prompt']), (8, 'A woman waves.'))
        finally:
            encoded.set(); generated.set(); thread.join(2)

    def test_cancelled_generation_cannot_commit(self):
        w = self.make_worker()
        valid = [True]
        class Model:
            def parameters(self): return iter([torch.zeros(1)])
            def autoregressive_step(self, **kw):
                valid[0] = False
                return torch.ones(1, kw['num_frames'], 3)
        w.model = Model()
        self.assertFalse(w._generate(40, w.features, 7, switch_prompt='New motion.',
                                    is_current=lambda: valid[0]))
        self.assertEqual((w.epoch, w.prompt), (7, 'Old motion.'))
        self.assertEqual(w.outputs, [])

    def test_restart_from_now_trims_before_encoding_and_keeps_history(self):
        w = self.make_worker()
        w.params['restart_from_now'] = True
        captured = {}
        class Encoder:
            cache = SimpleNamespace(contains=lambda text: False)
            def __call__(self, texts):
                self_check.assertFalse(w.playing)
                self_check.assertEqual(w.end, 20)
                w.feedback(dict(epoch=7, frame=21))
                self_check.assertEqual(w.cursor, 20)
                return torch.ones(1, 2, 3), None
        class Model:
            def parameters(self): return iter([torch.zeros(1)])
            def autoregressive_step(self, **kw):
                captured.update(kw)
                return torch.ones(1, kw['num_frames'], 3)
        self_check = self
        w.encoder, w.model = Encoder(), Model()
        self.assertEqual(w.begin(Step(prompt='New motion.', action='motion'), lambda: True), 21)
        self.assertEqual(captured['init_history_sequence'].shape[1], 16)
        self.assertTrue(torch.equal(w.motion[:, :21], torch.zeros(1, 21, 3)))
        self.assertEqual(w.end, 60)
        self.assertTrue(w.playing)
        self.assertEqual(w.outputs[0], dict(type='motion_trim', frame=20, epoch=7))

    def test_cancelled_restart_stays_paused_without_new_condition(self):
        w = self.make_worker()
        w.params['restart_from_now'] = True
        valid = [True]
        class Encoder:
            cache = SimpleNamespace(contains=lambda text: False)
            def __call__(self, texts):
                valid[0] = False
                return torch.ones(1, 2, 3), None
        w.encoder = Encoder()
        self.assertIsNone(w.begin(Step(prompt='Cancelled motion.'), lambda: valid[0]))
        self.assertFalse(w.playing)
        self.assertEqual(w.end, 20)
        self.assertEqual((w.epoch, w.prompt), (7, 'Old motion.'))

    def test_last_pose_refresh_rebuilds_static_history(self):
        w=self.make_worker();captured={}
        class Rep:
            def inverse(self,x,is_normalized):
                captured['last_input']=x.clone()
                return {'local_rot_mats':torch.eye(3).reshape(1,1,1,3,3), 'root_positions':torch.tensor([[[2.,.9,3.]]])}
            def __call__(self,**kw):
                captured['pose_inputs']=kw
                return torch.full((1,4,3),7.)
        class Model:
            motion_rep=Rep()
            def parameters(self):return iter([torch.zeros(1)])
            def autoregressive_step(self,**kw):
                captured['history']=kw['init_history_sequence'].clone()
                return torch.ones(1,kw['num_frames'],3)
        w.model=Model()
        w.motion[:,79]=42
        self.assertTrue(w._generate(79,w.features,7,refresh_pose=True))
        self.assertTrue(torch.equal(captured['last_input'],torch.full((1,1,3),42.)))
        self.assertEqual(captured['pose_inputs']['local_joint_rots'].shape,(1,4,1,3,3))
        self.assertEqual(captured['pose_inputs']['root_positions'].shape,(1,4,3))
        self.assertTrue(torch.equal(captured['history'],torch.full((1,4,3),7.)))
        self.assertEqual(w.pose_refresh_count,1)
        self.assertEqual(w.end,119)
        self.assertEqual(w.prompt,'Old motion.')


if __name__=='__main__': unittest.main()
