"""Mode-two static holds: observe first, then freeze or bound local rotations.

This is a kinematic drift guard, not IK, physics or task-completion detection.
"""
import math
import re
import numpy as np
import torch
from scipy.spatial.transform import Rotation


class PoseProtection:
    def clear_pose_guard(self):
        self.pose_snapshot = None
        self.pose_guard = 'off'
        self.pose_pending = None
        self.pose_guard_frame = None
        self.waiting_for_next=False
        self.waiting_motion_live=False
        self.finite_boundary=None
        self.pose_live_features = None
        self.pose_condition_prompt = ''
        self.active_is_static = False
        self.queued_transition_epoch = None
        self.queued_transition_loop = None
        self.queued_transition_action = None

    def static_intent(self, step):
        intent=getattr(step,'pose_intent','auto')
        if intent=='dynamic':return False
        if intent=='static' or getattr(step,'after_idle','keep')=='hold':return True
        if re.search(r'\b(turns?|tilts?|nods?|shakes?)\b',step.motion_prompt.lower()):return False
        if step.action in ('terminal-idle','terminal-hold','policy-rest'):return True
        if step.action in ('stand','sit','kneel','lie','idle') and not getattr(step,'prompt',None):return True
        prompt=step.motion_prompt.lower()
        # A maintained posture in an older/custom AI plan can omit pose_intent.
        if re.search(r'\b(walk\w*|run\w*|danc\w*|wav\w*|jump\w*|exercis\w*)\b',prompt):return False
        return bool(re.search(r'\b(remains? (seated|sitting|standing|kneeling|lying)|holds? .*(pose|posture)|stands? (upright|still)|is standing)\b',prompt))

    def static_request(self, step):
        return self.params.get('behavior_mode')=='event' and self.params.get('static_protection','freeze')!='off' and self.static_intent(step)

    def update_active_protection(self, previous):
        """Apply a changed method to the active static task, including a frozen one."""
        if self.params.get('behavior_mode')!='event' or not self.active_is_static:return
        method=self.params.get('static_protection','freeze')
        if self.pose_snapshot is not None:
            if method=='freeze':
                frame=min(self.end,max(self.base,self.cursor))
                pose=self.motion[:,frame-self.base:frame-self.base+1]
                self.capture_pose(pose,frame)
                # Overwrite all buffered future motion; the renderer must not keep old moving frames.
                prefix=self.motion[:,:frame-self.base+1]
                count=max(96,self.end-frame)
                clip=self.static_history(pose,count)
                self.motion=torch.cat([prefix,clip],dim=1)
                self.event_end=frame
                self.emit(self._packet(frame+1,clip))
            else:
                self.features=self.pose_live_features
                self.prompt=self.pose_condition_prompt or self.prompt
                self.idle_clip=None
                self.pose_guard=method
                if method=='off':self.pose_snapshot=None;self.pose_pending=None
        elif method=='off':self.pose_pending=None
        elif self.pose_pending is None:
            earliest=self.cursor+round(5*self.fps)
            self.pose_pending={'earliest_frame':earliest,'deadline_frame':earliest+round(5*self.fps),'posture':'hold'}

    def stable_pose_window(self, clip):
        # Two full seconds, including peak velocity: mean speed alone hides a moving hand.
        count = round(self.fps*2)
        if clip.shape[1] < count or not torch.isfinite(clip).all():return False
        data = self.model.motion_rep.inverse(clip[:,-count:],is_normalized=True)
        joints = data['posed_joints']
        speed = (joints[:,1:]-joints[:,:-1]).norm(dim=-1)*self.fps
        mats = data['local_rot_mats'].float().cpu().numpy()
        if not np.isfinite(mats).all():return False
        orthogonal = np.max(np.abs(np.swapaxes(mats,-1,-2)@mats-np.eye(3))) < .02
        if not orthogonal or np.min(np.linalg.det(mats)) < .98:return False
        relative = np.swapaxes(mats[:,:-1],-1,-2)@mats[:,1:]
        angular = np.linalg.norm(Rotation.from_matrix(relative.reshape(-1,3,3)).as_rotvec(),axis=-1)*self.fps
        mean,peak = speed.mean().item(),speed.max().item()
        self.metrics['static_window'] = {'mean_m_s':round(mean,4),'peak_m_s':round(peak,4),
                                        'peak_deg_s':round(math.degrees(float(angular.max())),2)}
        return bool(mean < .035 and peak < .12 and angular.max() < math.radians(18))

    def capture_pose(self, clip, frame):
        if getattr(self,'features',None) is not None:self.pose_live_features=self.features
        if not getattr(self,'prompt','').startswith('Held pose'):self.pose_condition_prompt=getattr(self,'prompt','')
        self.pose_snapshot = clip[:,-1:].clone()
        # A static task may settle while a real queued task is preparing. Bound
        # its continuation instead of disabling the generator between tasks.
        queued=getattr(self,'queued_transition_epoch',None)==getattr(self,'epoch',0)
        self.pose_guard = 'anchor' if queued else self.params.get('static_protection','freeze')
        self.pose_guard_frame = frame
        self.pose_pending = None
        self.record(stage='static_snapshot',protection=self.pose_guard,snapshot_frame=frame)
        if self.pose_guard == 'freeze':
            self.idle_kind='hold'
            self.idle_clip = self.static_history(self.pose_snapshot,32)
            self.features = None

    @torch.inference_mode()
    def constrain_pose(self, clip):
        rep = self.model.motion_rep
        data = rep.inverse(clip,is_normalized=True)
        base = rep.inverse(self.pose_snapshot,is_normalized=True)
        shape = data['local_rot_mats'].shape
        a = base['local_rot_mats'].float().cpu().numpy()
        b = data['local_rot_mats'].float().cpu().numpy()
        relative = np.swapaxes(a,-1,-2)@b
        vectors = Rotation.from_matrix(relative.reshape(-1,3,3)).as_rotvec()
        angles = np.linalg.norm(vectors,axis=-1,keepdims=True)
        dead,band = math.radians(3),math.radians(7)
        allowed = np.where(angles<=dead,angles,dead+band*(1-np.exp(-np.maximum(angles-dead,0)/band)))
        vectors *= allowed/np.maximum(angles,1e-12)
        mats = a@Rotation.from_rotvec(vectors).as_matrix().reshape(shape)
        rots = torch.as_tensor(mats,device=clip.device,dtype=clip.dtype)
        delta = data['root_positions']-base['root_positions']
        distance = delta.norm(dim=-1,keepdim=True)
        bound = torch.where(distance<=.01,distance,.01+.02*(1-torch.exp(-(distance-.01).clamp_min(0)/.02)))
        roots = base['root_positions']+delta*(bound/distance.clamp_min(1e-12))
        # Recompute FK, velocities and contacts, rather than patching normalized features.
        return rep(local_joint_rots=rots,root_positions=roots,to_normalize=True)

    def protect_generated(self, new, anchor):
        if self.params.get('behavior_mode') != 'event':return new
        if self.pose_guard == 'anchor' and self.pose_snapshot is not None:
            untouched=max(0,min(new.shape[1],self.pose_guard_frame-anchor))
            if untouched==new.shape[1]:return new
            return torch.cat([new[:,:untouched],self.constrain_pose(new[:,untouched:])],dim=1)
        pending = self.pose_pending
        if pending is not None:
            prefix = self.motion[:,:anchor-self.base+1] if self.motion is not None else new[:,:0]
            candidate = torch.cat([prefix,new],dim=1)
            end = anchor+new.shape[1]
            if end >= pending['earliest_frame']:
                stable=self.stable_pose_window(candidate)
                expired=end>=pending.get('deadline_frame',pending['earliest_frame']+round(5*self.fps))
                if stable or expired:
                    # Explicit static intent has a bounded settling phase, never endless sway.
                    self.capture_pose(candidate,end)
                    self.record(static_capture_reason='stable_window' if stable else 'static_settling_limit')
                    if getattr(self,'queued_transition_epoch',None)!=getattr(self,'epoch',0):
                        self.event_end=end
        return new
