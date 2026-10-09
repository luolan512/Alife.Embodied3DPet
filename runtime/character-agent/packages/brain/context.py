"""Bounded model views of runtime state. Full diagnostics remain in local logs."""
import json


def bounded(value, text=240, items=4, depth=4):
    if depth < 0:
        return None
    if isinstance(value, str):
        return value[:text]
    if isinstance(value, dict):
        return {k: bounded(v, text, items, depth-1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [bounded(v, text, items, depth-1) for v in value[:items]]
    return value


def compact_world(world):
    world = world or {}
    actor = world.get('actor') or {}
    camera = world.get('camera') or {}
    output = {
        'actor': {k: actor[k] for k in ('position', 'position_y_is', 'heading_degrees', 'facing') if k in actor},
        'camera': {k: camera[k] for k in ('position', 'forward', 'mode') if k in camera},
        'relative_objects': [{k: item[k] for k in ('id', 'relative_direction', 'distance_m') if k in item}
                             for item in (world.get('relative_objects') or [])[:6]],
    }
    visual = world.get('visual_observation') or {}
    if visual.get('fresh'):
        output['visual_observation'] = {k: bounded(visual.get(k)) for k in
            ('age_s', 'summary', 'uncertainties') if k in visual}
    return output


def compact_step(step):
    if step is None:
        return None
    value = step.model_dump() if hasattr(step, 'model_dump') else step
    return {k: bounded(value.get(k), text=300) for k in
            ('action', 'label', 'kind', 'prompt', 'duration_s', 'repeat', 'continuous', 'after_idle', 'look_at', 'look_target') if value.get(k) is not None}


def compact_observation(obs):
    if not obs:
        return None
    return {k: bounded(obs[k], items=6) for k in
            ('type', 'observation_id', 'action', 'execution_state', 'outcome', 'reason',
             'played_seconds', 'root_displacement_m', 'mean_root_speed_m_s',
             'physical_state', 'joint_geometry', 'rule_flags') if k in obs}


def compact_state(state):
    result = {'playing': state.get('playing'), 'prompt': str(state.get('prompt', ''))[:300],
            'current': compact_step(state.get('current')),
            'next': [compact_step(s) for s in state.get('next', [])[:16]],
            'world_state': compact_world(state.get('world_state')),
            'motion_timing': {'fps': state.get('fps', 20),
                              'generation_chunk_frames': state.get('horizon_frames', 40)}}
    result['behavior_state']=state.get('behavior_state',{})
    if state.get('face_state'):
        result['face_state']={k:state['face_state'][k] for k in ('emotion','valence','arousal','dominance','effective_intensity','age_s') if k in state['face_state']}
    observer=state.get('motion_observer') or {}
    observation=observer.get('result') or {}
    if observer.get('enabled'):
        rows=[r for r in observation.get('windows',[]) if r.get('stream')=='rendered']
        result['motion_feedback']={'fresh':bool(observer.get('result_fresh')),
            'status':observation.get('status','unknown') if observer.get('result_fresh') else 'unknown',
            'observed_frame':observation.get('frame'),'target':str(observer.get('target',''))[:160],
            'nearest_candidate':rows[0].get('nearest_candidate') if rows and observer.get('result_fresh') else None,
            'geometry_flags':(observation.get('geometry',{}).get('rendered',{}).get('flags',[])[:3] if observer.get('result_fresh') else []),
            'meaning':'Similarity evidence; no_drift_signal does not prove action success.'}
    progress=state.get('plan_progress',{})
    plans=progress.get('plans',[])
    active=next((p for p in reversed(plans) if any(s['id']==progress.get('current_step_id') for s in p.get('steps',[]))),None)
    if active:
        result['task_plan']={'goal':active.get('goal','')[:160],
                             'previous_steps':[{'label':s.get('label',s.get('action')),'kind':s.get('kind'),'status':s.get('status')}
                                               for s in active.get('steps',[]) if s.get('status') in ('completed','interrupted','cancelled')][-6:]}
        receipt=next((s for s in active.get('steps',[]) if s['id']==progress.get('current_step_id')), {})
        result['task_plan']['current_execution']={k:receipt[k] for k in ('status','phase','elapsed_s','progress','outcome','continuation') if k in receipt}
    result['observed_frame']=state.get('frame')
    perception = (state.get('world_state') or {}).get('perception') or {}
    if perception.get('enabled') and perception.get('active_enabled'):
        result['can_observe'] = True
    return result


def dialogue_history(history, pairs=4, text=500):
    """Migrate existing full assistant plans to conversational text; never replay state."""
    result = []
    for message in history[-pairs*2:]:
        content = message.get('content', '')
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                content = str(parsed.get('reply', parsed.get('message', '')))
        except (ValueError, TypeError):
            pass
        result.append({'role': message['role'], 'content': content[:text]})
    return result


def provider_options(cfg, role='dialogue'):
    from urllib.parse import urlparse
    cap = cfg.get(role+'_max_tokens', {'body': 600, 'vision': 700}.get(role, 1400))
    options = {'max_tokens': max(128, min(4096, int(cap)))}
    # Keep generic compatible providers free of DeepSeek-specific extensions.
    if urlparse(str(cfg.get('base_url', ''))).hostname == 'api.deepseek.com':
        options['thinking'] = {'type': 'enabled' if cfg.get('thinking_enabled', False) else 'disabled'}
        if cfg.get('thinking_enabled', False):
            options['reasoning_effort'] = 'low'
    return options


def check_text_budget(messages,role='dialogue'):
    """Block accidental state amplification before an HTTP request; images aren't text."""
    total=0
    for message in messages:
        content=message.get('content','')
        if isinstance(content,str):total+=len(content)
        elif isinstance(content,list):
            total+=sum(len(str(part.get('text',''))) for part in content if part.get('type')=='text')
    limit={'body':12000,'vision':8000}.get(role,16000)
    if total>limit:raise ValueError(f'{role} 上下文超出文本预算（{total}/{limit} 字符），请求已在本地阻止')
    return total
