"""Deliberate mouth intent reaches VRM without body generation or CTEG."""
import sys,unittest,threading
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services'/'face-generator'),*[str(ROOT/'packages'/p) for p in ('brain','scheduler','world','conversation')]]
from core import Plan,FaceAction
from controller import AgentController
from manager import FaceGenerator
import importlib.util
spec=importlib.util.spec_from_file_location('face_plan_test',ROOT/'scripts'/'test-face-plan.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class Tests(unittest.TestCase):
    def generator(self):
        packets=[]
        with patch.object(threading.Thread,'start'):
            face=FaceGenerator(packets.append)
        face.enabled=False
        return face,packets

    def test_plan_emits_mouth_without_body_or_cteg(self):
        face,packets=self.generator()
        motor=fixture.Motor();controller=AgentController(motor,start_threads=False);controller.face_generator=face
        plan=Plan(reply='好，张大嘴巴保持十秒。',mode='keep',face_action={'mouth_open':1,'hold_s':10},steps=[],after_reply=[])
        with patch('controller.completion',return_value=plan):controller._request({},[],{},controller.request_id,False)
        self.assertEqual(packets[0]['type'],'face_action');self.assertEqual(packets[0]['mouth_open'],1)
        self.assertEqual(packets[0]['hold_s'],10);self.assertFalse(controller.queue);self.assertIsNone(controller.current)

    def test_reconnect_remaining_and_reset(self):
        face,packets=self.generator()
        with patch('manager.time.monotonic',return_value=10):face.set_action({'mouth_open':1,'hold_s':10})
        with patch('manager.time.monotonic',return_value=13):
            action=next(p for p in face.replay() if p['type']=='face_action');self.assertEqual(action['hold_s'],7)
        with patch('manager.time.monotonic',return_value=21):self.assertFalse(any(p['type']=='face_action' for p in face.replay()))
        face.reset();self.assertIsNone(face.action)

    def test_validation(self):
        for data in ({'mouth_open':2},{'mouth_open':float('nan')},{'mouth_open':1,'hold_s':121},{'mouth_open':1,'other':1}):
            with self.assertRaises(ValueError):FaceAction.model_validate(data)
        self.assertEqual(FaceAction(mouth_open=0).hold_s,5)

    def test_eyes_only_and_independent_replay(self):
        face,packets=self.generator()
        with patch('manager.time.monotonic',return_value=10):face.set_action({'mouth_open':1,'hold_s':30})
        with patch('manager.time.monotonic',return_value=15):face.set_action({'eyes_closed':1,'hold_s':10})
        self.assertNotIn('mouth_open',packets[-1]);self.assertEqual(packets[-1]['eyes_closed'],1)
        with patch('manager.time.monotonic',return_value=20):
            actions=[p for p in face.replay() if p['type']=='face_action']
            self.assertEqual([p['hold_s'] for p in actions],[20,5])
        with self.assertRaises(ValueError):FaceAction(eyes_closed=2)
        with self.assertRaises(ValueError):FaceAction()

if __name__=='__main__':unittest.main()
