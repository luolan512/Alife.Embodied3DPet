"""Bounded OpenAI-compatible planner and playback-aware action executor."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from typing import Literal

CONFIG_PATH = Path(os.environ.get('ARDY_LLM_CONFIG', str(Path(__file__).resolve().parents[2] / '.local' / 'llm.json')))
DEFAULT_CONFIG = {"base_url": "", "model": "", "api_key": "", "api_key_env": "ARDY_LLM_API_KEY",
                  "timeout_s": 45, "json_mode": False, "auto_continue": False,
                  "thinking_enabled": False, "body_interval_s": 20, "idle_interval_s": 45}
MOTIONS = {
    "stand": "A person is standing.",
    "sit": "A person sits upright on the ground, facing forward.",
    "kneel": "A person kneels upright, facing forward.",
    "lie": "A person lies on their back, resting still.",
    "idle": "A person stands upright, relaxed and still.",
    "look_user": "A person stands upright facing forward.",
    "angry": "A person stands with arms crossed firmly in an annoyed posture.",
    "stretch": "A person stretches both arms above their head and relaxes.",
    "exercise": "A person gently exercises in place.",
    "walk": "A person is walking.",
    "walk_backwards": "A person is walking backwards.",
    "sidestep_left": "A person side steps to the left.",
    "sidestep_right": "A person side steps to the right.",
    "wave": "A person waves with their right hand.",
    "bow": "A person bows down and then stands upright.",
    "dance": "A person is dancing.",
    "cross_arms": "A person stands with their arms crossed.",
    "turn_left": "A person turns to the left.",
    "turn_right": "A person turns to the right.",
    "look_back": "A person looks back over their shoulder.",
    "jump": "A person jumps.",
    "look_around": "A person turns their head left, then right, looking around.",
    "idle_stand": "A person stands upright facing forward, arms relaxed at their sides, staying still.",
    "idle_sit": "A person remains seated upright on the ground, facing forward, resting still.",
    "idle_kneel": "A person remains kneeling upright facing forward, resting still.",
    "idle_lie": "A person remains lying on their back, resting still.",
}


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return cfg


def ai_configured(cfg):
    base = str(cfg.get('base_url', '')).strip()
    return base.startswith(('http://', 'https://')) and bool(str(cfg.get('model', '')).strip())


def save_config(cfg, path=CONFIG_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Step(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str = Field(default='motion', min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    duration_s: float = Field(default=3, ge=0.5, le=30)
    repeat: int = Field(default=1, ge=1, le=5, strict=True)
    after_idle: Literal['keep','stand','sit','kneel','lie','hold'] = 'keep'
    pose_intent: Literal['auto','static','dynamic'] = 'auto'
    continuous: bool = False
    kind: Literal['one_shot','loop'] = 'one_shot'
    label: str = Field(default='',max_length=80)
    target: tuple[float, float] | None = None
    speed: float = Field(default=0.8, ge=0.1, le=2)
    emotion: Literal['neutral','happy','sad','angry','relaxed'] | None = None
    face_text: str | None = Field(default=None,max_length=300)
    look_at: Literal['auto','user','none'] = 'auto'
    look_target: str | tuple[float, float, float] | None = None
    motion_description: dict = Field(default_factory=dict)

    @field_validator('motion_description', mode='before')
    @classmethod
    def optional_description(cls, value):
        return value if isinstance(value, dict) else {}

    @field_validator('look_target', mode='before')
    @classmethod
    def optional_look_target(cls, value):
        if isinstance(value,str) and value in ('camera','small_cube','big_cube','platform','none'):
            return value
        if isinstance(value,(list,tuple)) and len(value)==3 and all(type(v) in (int,float) and math.isfinite(v) and abs(v)<=100 for v in value):
            return tuple(value)
        return None

    @model_validator(mode="after")
    def validate_action(self):
        if self.continuous:self.kind='loop'
        if self.action.startswith('idle_') and self.action in MOTIONS:
            if self.prompt is not None or self.motion_description:raise ValueError('Cached idle uses its fixed prompt; omit prompt and motion_description')
            self.kind='loop';self.pose_intent='static'
        if self.action=='look_around':
            self.look_at='none';self.pose_intent='dynamic'
        if self.action == 'move_to':
            raise ValueError('This VRM phase supports generated walking prompts; precise move_to is not connected yet')
        if self.prompt is not None:
            self.prompt = self.prompt.strip()
            if not self.prompt:
                raise ValueError('Motion prompt must not be blank')
        if self.action not in MOTIONS and self.prompt is None and not self.description_prompt():
            raise ValueError('Custom motion needs an English prompt')
        if len(self.motion_prompt.split())>30:
            raise ValueError('Motion prompt must contain at most 30 English words; rewrite concisely, do not truncate')
        if self.action == "move_to":
            if self.target is None or not all(math.isfinite(v) and abs(v) <= 8 for v in self.target):
                raise ValueError("move_to requires a finite target [x,z] inside [-8,8]")
        elif self.target is not None:
            raise ValueError("target is only valid for move_to")
        if not math.isfinite(self.duration_s) or not math.isfinite(self.speed):
            raise ValueError("Non-finite number")
        return self

    @property
    def motion_prompt(self):
        return self.prompt if self.prompt is not None else self.description_prompt() or MOTIONS[self.action]

    def description_prompt(self):
        # Categories are authoring aids, not required body-part slots or an action whitelist.
        actor=self.motion_description.get('actor')
        actor=actor.strip().rstrip('.') if isinstance(actor,str) and actor.strip() else 'A person'
        parts=[]
        for key,value in self.motion_description.items():
            if key=='actor':continue
            if isinstance(value,str) and value.strip():parts.append(value.strip().rstrip('.'))
            elif isinstance(value,list):
                for item in value[:12]:
                    text=item if isinstance(item,str) else item.get('prompt',item.get('motion',item.get('text'))) if isinstance(item,dict) else None
                    if isinstance(text,str) and text.strip():parts.append(text.strip().rstrip('.'))
        if self.action in MOTIONS and not any(k not in ('actor','tempo','style') for k in self.motion_description):
            base=MOTIONS[self.action].replace('A person',actor,1).rstrip('.')
            return (base+('; '+'; '.join(parts) if parts else '')+'.')[:2000] if self.motion_description else None
        return (actor+' '+'; '.join(parts)+'.')[:2000] if parts else None


class LookRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')
    target: str | tuple[float,float,float] = 'camera'
    question: str = Field(min_length=1,max_length=600)
    turn_body: bool = True
    timeout_s: float = Field(default=12,ge=2,le=20)

    @field_validator('target')
    @classmethod
    def finite_target(cls,value):
        if isinstance(value,tuple) and not all(math.isfinite(v) and abs(v)<=100 for v in value):raise ValueError('invalid look coordinates')
        if isinstance(value,str) and not 1<=len(value)<=100:raise ValueError('invalid look target')
        return value

class FaceState(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    valence:float=Field(default=0,ge=-1,le=1)
    arousal:float=Field(default=0,ge=-1,le=1)
    dominance:float=Field(default=0,ge=-1,le=1)
    intensity:float=Field(default=.8,ge=0,le=1)
    hold_s:float=Field(default=8,ge=0,le=120)
    decay_s:float=Field(default=45,ge=2,le=300)
    mixture:dict[str,float]=Field(default_factory=dict,max_length=5)
    @field_validator('mixture')
    @classmethod
    def valid_mixture(cls,value):
        if set(value)-{'neutral','happy','sad','angry','relaxed'} or any(not math.isfinite(w) or not 0<=w<=1 for w in value.values()):raise ValueError('Invalid mixture')
        if value and sum(value.values())<=0:raise ValueError('Mixture needs positive weights')
        return value

class FaceAction(BaseModel):
    """Deliberate face pose, independent of speech and automatic blinking."""
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    mouth_open:float | None=Field(default=None,ge=0,le=1)
    eyes_closed:float | None=Field(default=None,ge=0,le=1)
    hold_s:float=Field(default=5,ge=0,le=120)
    @model_validator(mode='after')
    def needs_control(self):
        if self.mouth_open is None and self.eyes_closed is None:raise ValueError('Face action needs mouth_open or eyes_closed')
        return self

class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    think: str = ''
    goal: str = Field(default='',max_length=160)
    reply: str = Field(default='', max_length=2000)
    mode: Literal["auto", "replace", "append", "keep", "observe", "revise", "insert", "clear"] = "auto"
    inspect_motion: bool = False
    steps: list[Step] = Field(default_factory=list, max_length=8)
    emotion: Literal['neutral','happy','sad','angry','relaxed'] = 'neutral'
    face_text: str | None = Field(default=None,max_length=300)
    face_state: FaceState | None = None
    face_action: FaceAction | None = None
    after_reply: list[Step] | None = Field(default=None,max_length=2)
    say: str | None = Field(default=None, max_length=400)
    gaze: Literal['auto','user','none'] | None = None
    gaze_target: str | tuple[float,float,float] | None = None
    look: LookRequest | None = None
    observation: dict | None = None
    body_constraints: list[str] | None = Field(default=None,max_length=8)
    @field_validator('body_constraints')
    @classmethod
    def bounded_constraints(cls,value):
        if value is not None and any(not s.strip() or len(s)>160 for s in value):
            raise ValueError('Each body constraint needs 1..160 characters')
        return value
    @field_validator('think',mode='before')
    @classmethod
    def optional_think(cls,value):
        return value.strip()[:400] if isinstance(value,str) else ''
    @field_validator('gaze_target', mode='before')
    @classmethod
    def optional_gaze_target(cls, value):
        return Step.optional_look_target(value)

    @model_validator(mode="after")
    def validate_keep(self):
        if not self.reply.strip() and self.look is None and not self.inspect_motion:raise ValueError('final plan needs reply')
        if self.mode == "keep" and self.steps:
            raise ValueError("keep must have no steps")
        if self.mode=='observe' and (self.steps or self.after_reply or self.body_constraints is not None or self.gaze is not None or self.look is not None):
            raise ValueError('observe is read-only for the body: no steps, after_reply, constraints, gaze or look')
        for step in [*self.steps,*(self.after_reply or [])]:
            if step.emotion is None: step.emotion=self.emotion
        return self


def parse_plan(content):
    content = content.strip()
    if content.startswith("```") and content.endswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return Plan.model_validate_json(content)


PROMPT_PATH = Path(__file__).resolve().parents[2] / 'config' / 'character-prompt.txt'
SYSTEM = PROMPT_PATH.read_text(encoding='utf-8-sig')


def trace_messages(messages,paths):
    output=[]
    for message in messages:
        content=message.get('content')
        if isinstance(content,list):
            content=[{'type':'image_reference','path':paths.get(part.get('image_url',{}).get('url'),'not_saved')}
                     if part.get('type')=='image_url' else part for part in content]
        output.append({**message,'content':content})
    return output

def completion(cfg, messages, on_debug=None):
    base = str(cfg.get("base_url", "")).strip().rstrip("/")
    model = str(cfg.get("model", "")).strip()
    if not base or not model:
        raise ValueError("请先填写 Base URL 和 Model，并保存接口设置")
    if not base.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    key = os.environ.get(cfg.get("api_key_env", "ARDY_LLM_API_KEY"), "") or cfg.get("api_key", "")
    headers = {"Authorization": "Bearer " + key} if key else {}
    from context import provider_options,check_text_budget
    payload = {"model": model, "messages": messages, "stream": False, **provider_options(cfg, cfg.get("_role", "dialogue"))}
    if cfg.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    from pydantic import ValidationError
    with httpx.Client(timeout=float(cfg.get("timeout_s", 60))) as client:
        for attempt in range(4):
            check_text_budget(payload['messages'],cfg.get('_role','dialogue'))
            started=time.monotonic()
            safe_messages=trace_messages(payload['messages'],cfg.get('_image_paths',{}))
            if on_debug:on_debug(stage='llm_request',llm_attempt=attempt+1,context_chars=sum(len(json.dumps(m.get('content',''),ensure_ascii=False)) for m in safe_messages))
            if on_debug:on_debug(stage='llm_payload',llm_attempt=attempt+1,model=model,messages=safe_messages,options=provider_options(cfg,cfg.get('_role','dialogue')))
            response = client.post(url, json=payload, headers=headers)
            if on_debug:on_debug(stage='llm_response',llm_http_ms=round((time.monotonic()-started)*1000,2),llm_http_status=response.status_code)
            if response.is_error:
                raise RuntimeError(f'LLM HTTP {response.status_code}')
            body=response.json()
            if on_debug:on_debug(stage='llm_usage',llm_attempt=attempt+1,llm_usage=body.get('usage'))
            choice=body['choices'][0]
            if on_debug:on_debug(stage='llm_output',llm_attempt=attempt+1,output=choice.get('message'),finish_reason=choice.get('finish_reason'))
            if choice.get('finish_reason') in ('length','content_filter'):
                raise ValueError('LLM 回复被截断或过滤')
            content=choice['message'].get('content')
            if not isinstance(content,str):raise ValueError('LLM 未返回文字计划')
            try:
                return parse_plan(content)
            except ValidationError as exc:
                detail='; '.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors(include_input=False,include_url=False))[:600]
                if on_debug:on_debug(stage='json_repair',validation_error=detail)
                if attempt==3:raise ValueError('计划修复后仍无效：'+detail) from None
                payload['messages']=[*payload['messages'],{'role':'assistant','content':content},
                    {'role':'user','content':'请修正JSON计划中的这些字段，仅返回完整JSON：'+detail}]


