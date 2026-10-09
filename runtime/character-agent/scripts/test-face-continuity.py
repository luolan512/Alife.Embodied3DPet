import sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services'/'face-generator'),*[str(ROOT/'packages'/p) for p in ('brain','scheduler','world','conversation')]]
from emotion import EmotionState,english
from language import describe
from core import Plan,Step,FaceState
from controller import AgentController
import importlib.util
spec=importlib.util.spec_from_file_location('face_plan_test',ROOT/'scripts'/'test-face-plan.py')
test=importlib.util.module_from_spec(spec);spec.loader.exec_module(test)
Motor,Face=test.Motor,test.Face
class Tests(unittest.TestCase):
    def test_state_ages_and_rejects_invalid(self):
        now=[0];state=EmotionState(lambda:now[0]);state.set('happy',{'hold_s':8,'decay_s':10});now[0]=8;self.assertEqual(state.snapshot()['effective_intensity'],.8);now[0]=18;self.assertLess(state.snapshot()['effective_intensity'],.3)
        for bad in ({'intensity':float('nan')},{'hold_s':-1},{'other':1}):
            with self.assertRaises(ValueError):state.set('happy',bad)
    def test_chinese_conversion_and_english_no_request(self):
        calls=[]
        def complete(cfg,messages):calls.append(messages);return Plan(reply='已转换',mode='keep',face_text='I am happy to see you!',emotion='happy')
        result=describe('见到你很开心！','happy',complete,{})
        self.assertTrue(result['translated']);self.assertEqual(result['text'],'I am happy to see you!');self.assertEqual(len(calls),1)
        describe('I am happy!','happy',complete,{});self.assertEqual(len(calls),1)
        self.assertEqual(english('I’m happy!'),"I'm happy!");self.assertFalse(english('我很开心'))
    def test_after_reply_deferred_and_cancelled(self):
        motor=Motor();controller=AgentController(motor,start_threads=False);controller.face_generator=Face()
        plan=Plan(reply='好呀！',face_text='I feel happy!',emotion='happy',mode='keep',after_reply=[Step(action='nod',prompt='A person gently nods and waits attentively.',duration_s=3)])
        with patch('controller.completion',return_value=plan):controller._request({},[],{},controller.request_id,False)
        controller.tick_reply_followup();self.assertFalse(controller.queue)
        controller.reply_followup['deadline']=0;controller.tick_reply_followup();self.assertEqual(controller.queue[0].action,'nod');self.assertEqual(controller.queue[-1].kind,'loop')
        self.assertIsNone(controller.reply_followup)
        with patch('controller.completion',return_value=plan):controller._request({},[],{},controller.request_id,False)
        controller.cancel();self.assertIsNone(controller.reply_followup)
    def test_explicit_empty_after_reply_is_respected(self):
        controller=AgentController(Motor(),start_threads=False);controller.face_generator=Face()
        with patch('controller.completion',return_value=Plan(reply='静静陪你。',mode='keep',after_reply=[])):controller._request({},[],{},controller.request_id,False)
        self.assertIsNone(controller.reply_followup)
    def test_waiting_loop_yields_but_walking_does_not(self):
        controller=AgentController(Motor(),start_threads=False)
        controller.current=Step(action='walk',kind='loop',duration_s=4)
        controller.reply_followup={'request_id':0,'deadline':0,'steps':[Step(action='wave',duration_s=3,emotion='happy')]}
        controller.tick_reply_followup();self.assertEqual(controller.current.action,'walk')
        controller.current=Step(action='stand',kind='loop',duration_s=4)
        controller.segment_start_frame=0  # This fixture represents a started waiting loop.
        controller.tick_reply_followup();self.assertIsNone(controller.current);self.assertEqual(controller.queue[-1].emotion,'happy')
    def test_after_reply_inherits_emotion(self):
        plan=Plan(reply='hello',emotion='sad',after_reply=[Step(action='wave')])
        self.assertEqual(plan.after_reply[0].emotion,'sad')
    def test_face_state_contract(self):
        with self.assertRaises(ValueError):FaceState(intensity=2)
        self.assertEqual(Plan(reply='hello',face_state={'valence':.8}).face_state.valence,.8)
        state=EmotionState.resolve('happy',{'mixture':{'happy':.6,'sad':.4}})
        self.assertAlmostEqual(state['valence'],.21)
        with self.assertRaises(ValueError):EmotionState.resolve('happy',{'mixture':{'happy':float('nan')}})
if __name__=='__main__':unittest.main()
