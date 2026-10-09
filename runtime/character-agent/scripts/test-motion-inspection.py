"""Read-only motion inspection and optional body actions; no external model calls."""
import sys,unittest,importlib.util,json
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'packages'/p) for p in ('brain','scheduler','world','conversation')]
from core import Plan,Step
from controller import AgentController
spec=importlib.util.spec_from_file_location('face_plan_test',ROOT/'scripts/test-face-plan.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class Tests(unittest.TestCase):
 def controller(self):
  motor=fixture.Motor();motor.transport_paused=True
  motor.prefetch_prompts=lambda steps:motor.calls.append(('prefetch',steps))
  controller=AgentController(motor,start_threads=False);controller.face_generator=fixture.Face()
  controller.current=Step(action='walk',kind='loop',duration_s=4);controller.segment_start_frame=0
  controller.queue=[Step(action='wave',duration_s=3)]
  return controller,motor
 def test_observe_preserves_body(self):
  controller,motor=self.controller();current=controller.current;queue=list(controller.queue);motion_id=controller.motion_id
  with patch('controller.completion',return_value=Plan(reply='我继续原来的动作。',mode='observe',emotion='happy')):
   controller._request({},[],{},controller.request_id,False)
  self.assertIs(controller.current,current);self.assertEqual(controller.queue,queue);self.assertEqual(controller.motion_id,motion_id)
  self.assertTrue(motor.transport_paused);self.assertIsNone(controller.reply_followup)
  self.assertFalse(any(call[0]=='prefetch' for call in motor.calls));self.assertEqual(len(controller.face_generator.calls),1)
 def test_inspection_returns_latest_state_once(self):
  controller,motor=self.controller();queue=list(controller.queue)
  with patch('controller.completion',side_effect=[Plan(mode='observe',inspect_motion=True),Plan(reply='计划里接下来是挥手。',mode='observe')]) as complete:
   controller._request({},[],{},controller.request_id,False)
  self.assertEqual(complete.call_count,2)
  payload=json.loads(complete.call_args.args[1][-1]['content'])['motion_inspection']
  self.assertEqual(payload['current']['action'],'walk');self.assertEqual(payload['next'][0]['action'],'wave')
  self.assertEqual(controller.queue,queue);self.assertTrue(any(e['event']=='motion_inspected' for e in controller.events))
 def test_plain_reply_never_invents_followup(self):
  controller,motor=self.controller()
  with patch('controller.completion',return_value=Plan(reply='聊聊天就好。',mode='auto')):controller._request({},[],{},controller.request_id,False)
  self.assertIsNone(controller.reply_followup)
 def test_inspection_does_not_force_observe(self):
  controller,motor=self.controller();current=controller.current
  with patch('controller.completion',side_effect=[Plan(mode='observe',inspect_motion=True),Plan(reply='我继续走，接着挥手。',mode='append',steps=[Step(action='wave',duration_s=3)])]):
   controller._request({},[],{},controller.request_id,False)
  self.assertIs(controller.current,current);self.assertEqual(controller.queue[1].action,'wave')
  self.assertFalse(motor.transport_paused);self.assertTrue(any(call[0]=='prefetch' for call in motor.calls))
 def test_readonly_contract(self):
  for data in ({'steps':[{'action':'wave'}]},{'after_reply':[{'action':'wave'}]},{'gaze':'user'},{'body_constraints':[]}):
   with self.assertRaises(ValueError):Plan(reply='test',mode='observe',**data)
 def test_inspection_loop_is_bounded(self):
  controller,motor=self.controller()
  with patch('controller.completion',return_value=Plan(mode='observe',inspect_motion=True)) as complete:controller._request({},[],{},controller.request_id,False)
  self.assertEqual(complete.call_count,2);self.assertTrue(any(e['event']=='llm_error' for e in controller.events))
if __name__=='__main__':unittest.main()
