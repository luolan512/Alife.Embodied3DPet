"""Persistent text execution preferences; changes apply on backend restart."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
PATH=ROOT/'config'/'text-encoder.json'
DEFAULTS={'mode':'cpu','layer_chunk':2}
GPU_EXE=ROOT.parent/'kimodo.cpp'/'build'/'vulkan'/'kmd-encode-server.exe'

def validate(data):
    if not isinstance(data,dict) or set(data)-set(DEFAULTS):raise ValueError('Unknown text encoder setting')
    result={**DEFAULTS,**data}
    if result['mode'] not in ('cpu','vulkan-stream','vulkan-resident'):raise ValueError('Invalid text encoder mode')
    if type(result['layer_chunk']) is not int or not 1<=result['layer_chunk']<=8:raise ValueError('layer_chunk must be 1..8')
    return result

def load():return validate(json.loads(PATH.read_text(encoding='utf-8'))) if PATH.exists() else dict(DEFAULTS)

def save(data):
    validate(data)
    result=validate({**load(),**data})
    if result['mode']!='cpu' and not GPU_EXE.is_file():raise ValueError('Vulkan encoder is missing; run build.cmd -TextEncoderOnly -TextBackend vulkan')
    PATH.parent.mkdir(exist_ok=True);temporary=PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');temporary.replace(PATH)
    return result

def status(worker):
    raw=getattr(getattr(worker,'encoder',None),'encoder',None)
    current=raw.backend_status() if raw and hasattr(raw,'backend_status') else None
    selected=load()
    return {'settings':selected,'vulkan_available':GPU_EXE.is_file(),'runtime':current,
            'restart_required':getattr(worker,'text_encoder_settings',None)!=selected}
