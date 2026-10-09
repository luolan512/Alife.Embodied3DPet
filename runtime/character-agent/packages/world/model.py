"""Authoritative scene description plus observations of the final rendered character."""
import threading
import time
import math
import copy

def point(value):
    return isinstance(value,list) and len(value)==3 and all(type(v) in (float,int) and math.isfinite(v) and abs(v)<=100 for v in value)

def direction(value):
    if not point(value):return None
    length=math.hypot(value[0],value[2])
    return [value[0]/length,0,value[2]/length] if length>.001 else None

def cardinal(x,z):
    if math.hypot(x,z)<.001:return 'same_position'
    names=['south','south-east','east','north-east','north','north-west','west','south-west']
    return names[int((math.degrees(math.atan2(x,z))%360+22.5)//45)%8]

class WorldModel:
    def __init__(self):
        self.lock=threading.RLock()
        self.capabilities={}
        self.body={}
        self.observed_at=None
        self.spatial={}
        self.position=None
        self.velocity=None
        self.last_epoch=None
        self.last_frame=None
        self.vision=None
        self.vision_at=None
        self.vision_ttl=120
        self.visual_channels={}
        self.perception_snapshot=None
    def clear_vision(self):
        with self.lock:self.vision=None;self.vision_at=None;self.visual_channels={}
    def observe_vision(self, observation, captured_at, stale_after_s):
        with self.lock:
            channel='active' if observation.get('source')=='character_model_image' else 'background'
            previous=self.visual_channels.get(channel)
            if previous and captured_at<previous['at']:return False
            self.visual_channels[channel]=dict(data=copy.deepcopy(observation),at=captured_at,ttl=stale_after_s)
            if self.vision_at is None or captured_at>=self.vision_at:
                self.vision=copy.deepcopy(observation);self.vision_at=captured_at;self.vision_ttl=stale_after_s
            return True
    def observe(self,data):
        joints=data.get('joints',{})
        with self.lock:
            now=time.monotonic()
            position=data.get('position')
            if point(position):
                elapsed=now-self.observed_at if self.observed_at else None
                continuous=self.last_epoch==data.get('epoch') and self.last_frame is not None and data.get('frame',0)>=self.last_frame
                old_offset=(self.body.get('root_mapping') or {}).get('offset')
                new_offset=(data.get('root_mapping') or {}).get('offset')
                if point(old_offset) and point(new_offset) and math.dist(old_offset,new_offset)>.5:continuous=False
                self.velocity=[round((position[i]-self.position[i])/elapsed,3) for i in range(3)] if continuous and self.position is not None and elapsed and elapsed>.001 else None
                self.position=list(position)
                self.last_epoch=data.get('epoch');self.last_frame=data.get('frame')
            spatial=data.get('spatial')
            if isinstance(spatial,dict):
                self.spatial={}
                for key in ('body_forward','head_forward'):
                    if point(spatial.get(key)):self.spatial[key]=list(spatial[key])
                for key in ('camera','observer'):
                    value=spatial.get(key)
                    if isinstance(value,dict) and point(value.get('position')):
                        self.spatial[key]={k:value[k] for k in ('position','mode','source') if k in value}
                        if point(value.get('forward')):self.spatial[key]['forward']=value['forward']
                        if type(value.get('fov_degrees')) in (int,float) and 1<=value['fov_degrees']<=179:self.spatial[key]['fov_degrees']=value['fov_degrees']
                objects=spatial.get('objects')
                if isinstance(objects,list):
                    self.spatial['objects']=[{k:item[k] for k in ('id','position','size','grabbable') if k in item}
                        for item in objects[:20] if isinstance(item,dict) and isinstance(item.get('id'),str) and point(item.get('position'))]
                if isinstance(spatial.get('gaze'),dict):self.spatial['gaze']=spatial['gaze']
            self.observed_at=time.monotonic()
            self.body={'frame':data.get('frame'),'waiting_for_motion':data.get('waiting',False),
                       'joints':joints,'root_mapping':copy.deepcopy(data.get('root_mapping')), 'pose_success_detection':False}
    def snapshot(self):
        # Read outside the world lock: perception commits observations in the opposite direction.
        perception=self.perception_snapshot() if self.perception_snapshot else None
        with self.lock:
            objects=self.spatial.get('objects',[
                {'id':'platform','position':[0,0,0]},
                {'id':'small_cube','position':[.86,.1,-.24]},
                {'id':'big_cube','position':[-1,.24,-.6]}])
            forward=direction(self.spatial.get('body_forward'))
            relative=[]
            if self.position and forward:
                for item in objects:
                    dx=item['position'][0]-self.position[0];dz=item['position'][2]-self.position[2]
                    front=dx*forward[0]+dz*forward[2]
                    right=-dx*forward[2]+dz*forward[0]
                    relative.append({'id':item['id'],'world_direction':cardinal(dx,dz),'distance_m':round(math.hypot(dx,dz),3),
                                     'forward_m':round(front,3),'right_m':round(right,3),
                                     'relative_direction':('front' if front>.15 else 'behind' if front<-.15 else 'beside')+' / '+('right' if right>.15 else 'left' if right<-.15 else 'center')})
            world_state={'frame_id':'world','coordinates':{'north':'-Z','south':'+Z','east':'+X','west':'-X','up':'+Y','units':'metres','camera_changes_axes':False},
                         'actor':{'position':self.position,'velocity_m_s':self.velocity,'body_forward':forward,
                                  'heading_degrees':round(math.degrees(math.atan2(forward[0],forward[2]))%360,2) if forward else None,
                                  'facing':cardinal(forward[0],forward[2]) if forward else 'unknown',
                                  'head_forward':self.spatial.get('head_forward'),'position_y_is':'pelvis_height'},
                         'camera':self.spatial.get('camera'),'observer':self.spatial.get('observer'),'gaze':self.spatial.get('gaze'),
                         'relative_objects':relative,'source':'runtime_geometry','vision_available':self.vision is not None,
                         'age_s':round(time.monotonic()-self.observed_at,3) if self.observed_at else None}
            visual_age=time.monotonic()-self.vision_at if self.vision_at is not None else None
            fresh=visual_age is not None and visual_age<=self.vision_ttl
            world_state['visual_observation']={**(copy.deepcopy(self.vision) if fresh else {}),
                'available':self.vision is not None,'fresh':fresh,
                'age_s':round(visual_age,2) if visual_age is not None else None,
                'status':'fresh' if fresh else 'stale' if self.vision else 'not_observed',
                'coordinates_authority':'runtime_geometry', 'semantic_claims':'model_estimates_not_ground_truth'}
            world_state['perception']=perception
            world_state['visual_channels']={}
            for channel,item in self.visual_channels.items():
                age=time.monotonic()-item['at'];is_fresh=age<=item['ttl']
                # Per-channel compact facts coexist; image bytes never enter World State.
                world_state['visual_channels'][channel]={**({k:copy.deepcopy(item['data'][k]) for k in ('summary','source','observation_id','question','uncertainties') if k in item['data']} if is_fresh else {}),
                    'fresh':is_fresh,'age_s':round(age,2),'status':'fresh' if is_fresh else 'stale'}
            return {'scene':{'bounds':3.2,'objects':objects,
                'object_interaction':False,'physics_collision':False},
                'world_state':world_state,
                'capabilities':dict(self.capabilities),'body_observation':{**self.body,'age_s':round(time.monotonic()-self.observed_at,3) if self.observed_at else None}}
