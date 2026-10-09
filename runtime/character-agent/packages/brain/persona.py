"""Editable character identity, shared by dialogue and autonomous planning."""
import json
import os
import threading
from pathlib import Path

PATH=Path(os.environ.get('CHARACTER_PERSONA_CONFIG',str(Path(__file__).resolve().parents[2]/'config'/'persona.json')))
LOCK=threading.RLock()
MAX_LENGTH=2000
DEFAULT_PROMPT='''动作提示词优先描述幅度大、清晰、略夸张的身体动作，明确写出大幅摆臂、明显转头或充分伸展等具体幅度，而不是只写轻轻、微微。英文动作提示词最多30词；用户要求安静、保持姿势或小动作时尊重用户要求。需要明显幅度时提供具体英文prompt，不只选择普通预设。
你是一个性格活泼、可爱的女生，是陪伴用户的桌面角色。
对话自然亲近，语气轻快、有自己的想法，不机械地重复同一句话，不刻意堆砌撒娇语气。
自主行为体现轻巧、好奇、适度活跃：根据当前姿态安排不同的短动作，避免一直重复挥手或长期停在一个临时姿势。
结合用户的话选择行为，用户明确要求保持的姿态和动作优先；普通聊天不必每句话都换动作。
没有视觉或执行证据时，不声称动作已成功完成。'''

def load_persona(path=None):
    path=Path(path or PATH)
    with LOCK:
        return validate_persona(json.loads(path.read_text(encoding='utf-8-sig'))) if path.exists() else {'enabled':True,'prompt':DEFAULT_PROMPT}

def validate_persona(data,current=None):
    if not isinstance(data,dict):raise ValueError('角色设定必须是对象')
    result=dict(current or {'enabled':True,'prompt':DEFAULT_PROMPT})
    if set(data)-{'enabled','prompt'}:raise ValueError('未知角色设定字段')
    result.update(data)
    if type(result['enabled']) is not bool:raise ValueError('enabled必须是布尔值')
    prompt=result['prompt']
    if not isinstance(prompt,str) or len(prompt)>MAX_LENGTH:raise ValueError(f'角色提示词最多{MAX_LENGTH}字符')
    result['prompt']=prompt.strip()
    if result['enabled'] and not result['prompt']:raise ValueError('启用角色设定时提示词不能为空')
    return result

def save_persona(data,path=None):
    path=Path(path or PATH)
    with LOCK:
        result=validate_persona(data,load_persona(path))
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
        return result

def character_system(base,path=None):
    profile=load_persona(path)
    if not profile['enabled']:return base
    return base+'\n\n角色身份、性格、说话与行为偏好（同时用于用户对话和自主规划）：\n'+profile['prompt']+'\n角色设定影响回复和动作选择；输出格式、动作能力与执行规则仍按前面的约定。'
