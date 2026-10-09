"""Validated runtime controls with the same units as the ARDY demo."""
import json
import math
from pathlib import Path

DEFAULTS={'model':'core40','denoising_steps':10,'history_frames':16,'replan_buffer_frames':24,
          'trigger_frames':40,'transition_lead_frames':20,'cfg_text':2.,'cfg_constraint':2.,
          'seed':2,'auto_replan':False,'startup_default_prompt':False,'restart_from_now':False,'refresh_last_pose':False,
          'event_mode':True,'behavior_mode':'hybrid','inactive_minutes':30,'settle_buffer_s':6,'static_protection':'freeze','allow_cached_idle_loops':False,'motion_prefetch':True,'inference_backend':'trt-fp16','official_postprocess':False}
LIMITS={'denoising_steps':(1,10),'history_frames':(0,200),'replan_buffer_frames':(0,100),
        'trigger_frames':(4,100),'transition_lead_frames':(4,100),'cfg_text':(0,10),
        'cfg_constraint':(0,10),'seed':(0,2147483647)}
PATH=Path(__file__).resolve().parents[2]/'config'/'motion.json'

def validate(data,current=None):
    result=dict(current or DEFAULTS)
    for key,value in data.items():
        if key not in DEFAULTS:raise ValueError('Unknown motion parameter: '+key)
        if key=='inference_backend':
            if value not in ('none','trt-fp16','torch-compile'):raise ValueError('inference_backend must be none/trt-fp16/torch-compile')
        elif key=='static_protection':
            if value not in ('off','freeze','anchor'):raise ValueError('static_protection must be off/freeze/anchor')
        elif key=='behavior_mode':
            if value not in ('stream','event','hybrid'):raise ValueError('behavior_mode must be stream/event/hybrid')
        elif key in ('inactive_minutes','settle_buffer_s'):
            lo,hi=(1,180) if key=='inactive_minutes' else (2,20)
            if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError(key+' out of range')
        elif key=='model':
            if value not in ('core40','core8'):raise ValueError('Only Core40 / Core8 are supported')
        elif key in ('auto_replan','startup_default_prompt','restart_from_now','refresh_last_pose','event_mode','allow_cached_idle_loops','motion_prefetch','official_postprocess'):
            if type(value) is not bool:raise ValueError(key+' must be boolean')
        else:
            lo,hi=LIMITS[key]
            if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError(f'{key} must be {lo}..{hi}')
            if key not in ('cfg_text','cfg_constraint') and value!=int(value):raise ValueError(key+' must be integer')
        result[key]=value
    if 'event_mode' in data and 'behavior_mode' not in data:result['behavior_mode']='event' if data['event_mode'] else 'stream'
    if 'behavior_mode' in data:result['event_mode']=data['behavior_mode']!='stream'
    return result

def load():
    return validate(json.loads(PATH.read_text(encoding='utf-8'))) if PATH.exists() else dict(DEFAULTS)

def save(data):
    PATH.parent.mkdir(exist_ok=True)
    temporary=PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,indent=2),encoding='utf-8')
    temporary.replace(PATH)
