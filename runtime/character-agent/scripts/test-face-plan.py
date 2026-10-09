"""Dialogue-to-face regression; completion is stubbed, no external API."""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'packages'/p) for p in ('brain','scheduler','world','conversation')]
from core import Plan,Step
from controller import AgentController
class Motor:
    owner=True
    def __init__(self):self.calls=[]
    def state(self):return {'frame':0,'epoch':0,'fps':20,'playing':False,'backend_ready':True,'position':[0,0,0],'motion_parameters':{}}
    def record(self,**kw):pass
    def expression(self,*args):self.calls.append(args)
    def invalidate(self):pass
    def halt(self):pass
class Face:
    def __init__(self):self.calls=[]
    def request(self,*args,**kwargs):self.calls.append(args)
class Tests(unittest.TestCase):
    def test_chinese_reply_generates_face_without_waiting_for_audio(self):
        motor=Motor();face=Face();controller=AgentController(motor,start_threads=False);controller.face_generator=face
        plan=Plan(reply='太好了，我很开心！',face_text='That sounds wonderful, I am really excited!',emotion='happy',mode='keep',steps=[])
        with patch('controller.completion',return_value=plan):controller._request({},[],{},controller.request_id,False)
        self.assertEqual(len(face.calls),1);self.assertEqual(face.calls[0][:2],(plan.face_text,'happy'));self.assertGreater(face.calls[0][2],0)
        self.assertEqual(motor.calls[0][0],'happy')
    def test_step_keeps_its_own_face_intent(self):
        step=Step(action='wave',emotion='happy',face_text='Hello, great to see you!')
        self.assertEqual(step.model_dump()['face_text'],'Hello, great to see you!')
if __name__=='__main__':unittest.main()
