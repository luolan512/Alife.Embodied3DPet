"""Continuous body observations and a separate, playback-grounded Motion Agent."""
import json
import math
import os
import threading
import time
import uuid
from core import SYSTEM, completion
from context import compact_state, compact_observation, dialogue_history

MOTION_SYSTEM = SYSTEM + '''
You are the internal Motion Agent, not the dialogue speaker. Your reply is a SHORT internal decision label (under 100 characters), never dialogue.
If an important new blockage needs a public explanation, set say to a brief Chinese message;
ordinary keep/idle decisions must not produce say. Never answer old user questions in reply.
Use the latest character dialogue as goals/style, and actual Observation as evidence.
Keep a natural ongoing presence even without user messages. Prefer keep with no steps
when the current motion is appropriate. Avoid endlessly repeating a completed gesture;
settle to relaxed standing after a finite user action. Do not invent new conversations.
While a character plan is running, keep it; you may append a short continuation only
when its queue is empty and it is nearing completion. Do not replace an active plan.
If idle, usually keep the cached standing motion. Only propose a brief new behavior
when it follows the current goal or a meaningful new event. No random elaborate idle.
Observation outcome UNVERIFIED means playback alone cannot prove semantic success.
Pending/generated frames are predictions, never already performed actions.
'''


class BodyLoop:
    def __init__(self, controller, planner=completion, clock=time.monotonic):
        self.controller = controller
        self.planner = planner
        self.clock = clock
        self.enabled = True
        self.suspended = False
        self.pending = False
        self.version = 0
        self.sequence = 0
        self.decisions = 0
        self.history = []
        self.observations = []
        self.previous = None
        self.last_observed = 0
        self.next_decision = 0
        self.failures = 0
        self.phase = 'waiting_for_runtime'
        self.last_decision = None
        self.last_action_feedback = None
        self.inflight = False  # Network requests cannot always be physically cancelled.

    def interval(self):
        cfg = self.controller.config_loader()
        key = 'body_interval_s' if self.controller.current else 'idle_interval_s'
        return max(6, min(300, float(cfg.get(key, 20 if self.controller.current else 45))))

    def invalidate(self, suspend=False):
        self.version += 1
        self.pending = False
        if suspend:
            self.suspended = True
        self.next_decision = self.clock() + 6

    def configure(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        self.enabled = enabled
        self.suspended = False
        self.invalidate()
        self.next_decision = self.clock()

    def snapshot(self):
        return {'enabled': self.enabled, 'suspended': self.suspended,
                'phase': self.phase, 'pending': self.pending, 'inflight': self.inflight,
                'decision_count': self.decisions, 'observation_count': self.sequence,
                'decision_interval_s': self.interval(), 'last_decision': self.last_decision,
                'last_action_feedback': self.last_action_feedback,
                'feedback': self.observations[-8:]}

    def terminal(self, action, status, frame, start, reason):
        observation = {'type': 'action_result', 'action': action,
                    'action_id': f'{self.controller.motion_id}:{start}',
                    'epoch': self.controller.motor.state()['epoch'],
                    'execution_state': status, 'outcome': 'UNVERIFIED',
                    'played_range': [start, frame], 'reason': reason}
        self._push(observation)
        self.last_action_feedback = observation

    def _push(self, observation):
        self.sequence += 1
        observation.update(observation_id=self.sequence, observed_at=time.time())
        self.observations.append(observation)
        if self.controller.trace:self.controller.trace.record('observation', observation)
        self.observations = self.observations[-24:]

    def observe(self, state):
        now = self.clock()
        body = state.get('body_observation', {})
        # Only the runtime owner can report final rendered frames.
        if (body.get('age_s') if body.get('age_s') is not None else 999) > 2 or body.get('frame') != state['frame']:
            return
        sample = {'epoch': state['epoch'], 'frame': state['frame'],
                  'position': list(state['position']), 'time': now}
        previous = self.previous
        if previous is None or previous['epoch'] != sample['epoch'] or sample['frame'] < previous['frame']:
            self.previous = sample
            return
        horizon = state['horizon_frames']
        if sample['frame'] - previous['frame'] < horizon:
            if not body.get('waiting_for_motion') or now - self.last_observed < 3:
                return
        frames = sample['frame'] - previous['frame']
        duration = frames / state['fps']
        distance = math.hypot(sample['position'][0]-previous['position'][0],
                              sample['position'][2]-previous['position'][2])
        flags = []
        if body.get('waiting_for_motion'):
            flags.append('BUFFER_UNDERRUN')
        if max(abs(sample['position'][0]), abs(sample['position'][2])) >= 3.15:
            flags.append('NEAR_SCENE_BOUNDARY')
        joints = body.get('joints', {})
        geometry = {}
        for side in ('left', 'right'):
            hand, shoulder = joints.get(side+'Hand'), joints.get(side+'Shoulder')
            if self._point(hand) and self._point(shoulder):
                geometry[side+'_hand_above_shoulder_m'] = round(hand[1]-shoulder[1], 3)
            foot = joints.get(side+'Foot')
            if self._point(foot):
                geometry[side+'_foot_world_y_m'] = round(foot[1], 3)
        world=state.get('world_state',{})
        self._push({'type': 'body_window', 'source': 'final_runtime',
                    'epoch': sample['epoch'], 'played_range': [previous['frame'], sample['frame']],
                    'played_seconds': round(duration, 3),
                    'root_displacement_m': round(distance, 4),
                    'mean_root_speed_m_s': round(distance/duration, 3) if duration else None,
                    'physical_state': {'position': sample['position'], 'is_grounded': None,
                                       'pose_type': 'UNVERIFIED', 'contact_source': 'unavailable'},
                    'joint_geometry': geometry,
                    'orientation': world.get('actor'), 'gaze': world.get('gaze'),
                    'intended_action': self.controller.current.action if self.controller.current else 'idle',
                    'outcome': 'UNVERIFIED', 'rule_flags': flags})
        self.previous = sample
        self.last_observed = now

    @staticmethod
    def _point(point):
        return (isinstance(point, list) and len(point) == 3 and
                all(type(v) in (int, float) and math.isfinite(v) for v in point))

    def tick(self):
        c = self.controller
        with c.lock:
            if c.closed:
                return
            state = c.motor.state()
            self.observe(state)
            if not self.enabled or self.suspended:
                self.phase = 'suspended'
                return
            if not getattr(c.motor, 'owner', False) or not state.get('backend_ready'):
                self.phase = 'waiting_for_runtime'
                return
            age = state.get('body_observation', {}).get('age_s')
            if not state['playing'] or age is None or age > 2:
                self.phase = 'paused_or_no_feedback'
                return
            if c.pending_request:
                self.phase = 'user_priority'
                return
            behavior=state.get('behavior_state',{})
            if behavior.get('effective_mode')=='event' and not c.current and not c.queue:
                self.phase='cached_idle_budget'
                return
            if self.inflight or self.clock() < self.next_decision:
                return
            cfg = c.config_loader()
            if not cfg.get('auto_continue', False) or os.environ.get('CHARACTER_BODY_LLM') == '0':
                self.phase = 'local_feedback_only'
                return
            if not cfg.get('base_url') or not cfg.get('model'):
                self.phase = 'interface_not_configured'
                return
            # User plans own the scheduler until the final segment is nearly done.
            if c.queue or (c.current and (c.segment_start_frame is None or
                    (state['frame']-c.segment_start_frame)/state['fps'] < max(0, c.current.duration_s-2))):
                self.phase = 'executing_character_plan'
                return
            context = compact_state({**state, 'current': c.current.model_dump() if c.current else None,
                                     'motion_observer': c.motion_observer.snapshot() if c.motion_observer else None,
                                     'body_loop': self.snapshot()})
            context['dialogue_goal'] = dialogue_history(c.history, pairs=2, text=250)
            context['recent_decisions'] = self.history[-3:]
            user = {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}
            # Only CURRENT state is supplied; old environment snapshots stay in local logs.
            messages = [{'role': 'system', 'content': MOTION_SYSTEM}, user]
            cfg = {**cfg, '_role': 'body'}
            token = (self.version, c.request_id, c.motion_id)
            self.pending = self.inflight = True
            self.phase = 'motion_agent_thinking'
            self.next_decision = self.clock() + self.interval()
        threading.Thread(target=self._request, args=(cfg, messages, user, token), daemon=True).start()

    def _request(self, cfg, messages, user, token):
        c = self.controller
        started = self.clock()
        call_id = uuid.uuid4().hex
        def debug(**data):
            look_round=data.pop('look_round',0)
            if c.trace:c.trace.llm('body', call_id+('/look-'+str(look_round) if look_round else ''), **data)
        try:
            plan = c.perception.decide(cfg,messages,self.planner,lambda:self._valid(token),debug) if c.perception else self.planner(cfg, messages, on_debug=debug)
            with c.lock:
                if not self._valid(token):
                    if c.trace:c.trace.record('plan_discarded', {'role': 'body', 'call_id': call_id, 'plan': plan.model_dump()})
                    return
                self.history.append({'mode': plan.mode, 'label': plan.reply[:100],
                                     'actions': [step.action for step in plan.steps],
                                     'frame': c.motor.state()['frame']})
                self.history = self.history[-3:]
                self.decisions += 1
                self.failures = 0
                self.last_decision = {'mode': plan.mode, 'decision_label': plan.reply[:100],
                                      'actions': [step.action for step in plan.steps],
                                      'latency_ms': round((self.clock()-started)*1000, 2)}
                if plan.gaze is not None:c.motor.expression(plan.emotion,plan.gaze,plan.gaze_target)
                if not c.queue and (c.current is None or plan.mode == 'append'):
                    if plan.mode != 'keep' and plan.steps:
                        c.queue.extend(plan.steps)
                        c.phase = '自主身体计划已准备'
                if plan.say:
                    c.add_chat('AI', plan.say)
                    c.remember([{'role':'assistant','content':plan.say}])
                    if c.trace:c.trace.record('body_public_notice', {'content': plan.say})
                self.phase = 'keeping_motion' if not plan.steps else 'continuation_prepared'
                if c.trace:c.trace.record('body_decision', {'call_id': call_id, 'plan': plan.model_dump(),
                                                          'queued_actions': [step.action for step in c.queue]})
                c.motor.record(body_agent_decision=self.last_decision)
        except Exception as exc:
            with c.lock:
                if self._valid(token):
                    if c.trace:c.trace.record('llm_failed', {'role': 'body', 'call_id': call_id, 'error': type(exc).__name__})
                    self.failures += 1
                    self.phase = 'motion_agent_error'
                    self.last_decision = {'error': type(exc).__name__}
                    self.next_decision = self.clock()+min(60, 6*2**min(self.failures, 3))
        finally:
            with c.lock:
                self.inflight = False
                self.pending = False
                self.next_decision = max(self.next_decision, self.clock()+self.interval())

    def _valid(self, token):
        c = self.controller
        return (not c.closed and self.enabled and not self.suspended and
                c.config_loader().get('auto_continue', False) and
                os.environ.get('CHARACTER_BODY_LLM') != '0' and
                token == (self.version, c.request_id, c.motion_id) and
                not c.pending_request and c.motor.state()['playing'])
