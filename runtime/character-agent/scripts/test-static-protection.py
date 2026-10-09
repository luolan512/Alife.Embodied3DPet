"""Static safety gates and quaternion/root bounds, no model download or LLM."""
import sys,threading,unittest,math
from pathlib import Path
from types import SimpleNamespace
import torch
import numpy as np
from scipy.spatial.transform import Rotation
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services/ardy-worker'),str(ROOT/'packages/brain')]
from event_motion import EventMotion
from core import Step
from settings import validate

class Rep:
    def __call__(self,local_joint_rots,root_positions,to_normalize=True):
        return torch.cat([root_positions,local_joint_rots.flatten(2)],dim=-1)
    def inverse(self,x,is_normalized=True):
        root=x[:,:,:3];rots=x[:,:,3:].reshape(1,x.shape[1],3,3,3)
        return {'root_positions':root,'local_rot_mats':rots,'posed_joints':root[:,:,None,:]+rots[:,:,:,:,0]}

def clip(n=40,angle=0.,moving=False):
    angles=np.arange(n)*angle if moving else np.full(n,angle)
    mats=torch.tensor(Rotation.from_euler('z',angles,degrees=True).as_matrix(),dtype=torch.float32)[None,:,None].repeat(1,1,3,1,1)
    return Rep()(mats,torch.zeros(1,n,3))

class Motor(EventMotion):
    def __init__(self,method='freeze',moving=False):
        self.init_events();self.params={'behavior_mode':'event','static_protection':method,'settle_buffer_s':2}
        self.model=SimpleNamespace(motion_rep=Rep());self.encoder=lambda p:(torch.zeros(1,1,4),None)
        self.fps=20;self.horizon=40;self.metrics={};self.motion=clip();self.base=0;self.cursor=20
        self.epoch=0;self.ready=self.owner=self.playing=True;self.transport_paused=False;self.features=None
        self.lock=threading.RLock();self.control_lock=threading.RLock();self.event_build_lock=threading.Lock()
        self.outputs=[];self.emit=self.outputs.append;self.calls=0;self.moving=moving
    @property
    def end(self):return self.base+self.motion.shape[1]-1
    def record(self,**data):self.metrics.update(data)
    def expression(self,*a,**kw):pass
    def encode_prompt(self,prompt,valid=None):return self.encoder([prompt])
    def _packet(self,start,c):return {'start':start,'frames':c.shape[1]}
    def idle_for(self,kind,valid):return clip(32)
    def synthesize_clip(self,prompt,frames,history,valid):
        self.calls+=1
        return clip(frames,angle=2 if self.moving else 0,moving=self.moving) if valid() else None

class Tests(unittest.TestCase):
    def test_scope_and_intent(self):
        m=Motor()
        self.assertTrue(m.static_request(Step(action='wave',after_idle='hold')))
        self.assertFalse(m.static_request(Step(action='walk',kind='loop',pose_intent='dynamic',after_idle='hold')))
        self.assertFalse(m.static_request(Step(action='wave',kind='loop')))
        for mode in ('hybrid','stream'):
            m.params['behavior_mode']=mode
            self.assertFalse(m.static_request(Step(action='sit',pose_intent='static')))
        with self.assertRaises(ValueError):validate({'static_protection':'bad'})
    def test_full_window_and_peak_rotation_gate(self):
        m=Motor()
        self.assertFalse(m.stable_pose_window(clip(39)))
        self.assertTrue(m.stable_pose_window(clip(40)))
        self.assertFalse(m.stable_pose_window(clip(40,2,True)))
        bad=clip();bad[0,0,0]=float('nan');self.assertFalse(m.stable_pose_window(bad))
    def test_freeze_waits_and_replays_without_generation(self):
        m=Motor();m.begin_event(Step(action='motion',prompt='Hold both hands up.',after_idle='hold'),lambda:True)
        self.assertGreaterEqual(m.metrics['settle_buffer_frames'],100)
        self.assertEqual(m.pose_guard,'freeze');self.assertIsNone(m.features)
        calls=m.calls
        for _ in range(300):m.cursor=m.end-20;m.pump_idle()
        self.assertEqual(m.calls,calls);self.assertLess(m.motion.shape[1],220)
    def test_unsettled_is_not_frozen(self):
        m=Motor(moving=True);m.begin_event(Step(action='motion',prompt='Hold both hands up.',after_idle='hold'),lambda:True)
        self.assertEqual(m.pose_guard,'off');self.assertIsNotNone(m.pose_pending)
        self.assertIsNone(m.idle_clip);self.assertIsNotNone(m.features)
        self.assertTrue(m.continuous_live)
    def test_anchor_deadzone_bounds_and_rebuilt_rotations(self):
        m=Motor('anchor');m.capture_pose(clip(),0)
        small=clip(angle=2);torch.testing.assert_close(m.constrain_pose(small),small)
        huge=clip(angle=170);huge[:,:,:3]=1
        data=Rep().inverse(m.constrain_pose(huge))
        degrees=np.linalg.norm(Rotation.from_matrix(data['local_rot_mats'].numpy().reshape(-1,3,3)).as_rotvec(),axis=-1)*180/math.pi
        self.assertLessEqual(degrees.max(),10.001)
        self.assertLessEqual(data['root_positions'].norm(dim=-1).max().item(),.030001)
    def test_new_event_releases_old_snapshot(self):
        m=Motor('anchor');m.capture_pose(clip(),0)
        m.begin_event(Step(action='wave',after_idle='stand',pose_intent='dynamic'),lambda:True)
        self.assertIsNone(m.pose_snapshot);self.assertEqual(m.pose_guard,'off')
    def test_pending_capture_and_future_boundary(self):
        m=Motor('anchor');m.pose_pending={'earliest_frame':80}
        m.protect_generated(clip(40),39);self.assertIsNone(m.pose_snapshot)
        m.protect_generated(clip(40),79);self.assertIsNotNone(m.pose_snapshot)
        # Frames preceding snapshot remain untouched, not retroactively corrected.
        before=clip(20,170);torch.testing.assert_close(m.protect_generated(before,90),before)

if __name__=='__main__':unittest.main()
