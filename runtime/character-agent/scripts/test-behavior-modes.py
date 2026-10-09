import sys,time,threading,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services'/'ardy-worker'),str(ROOT/'packages'/'brain'),str(ROOT/'packages'/'scheduler'),str(ROOT/'packages'/'world')]
from controller import AgentController
from core import Step,Plan
from event_motion import EventMotion
from settings import validate

class Motor(EventMotion):
    def __init__(self):
        self.init_events();self.params={'behavior_mode':'hybrid','inactive_minutes':30}
        self.owner=self.ready=True;self.transport_paused=False;self.features=None;self.motion=None
        self.started=threading.Event();self.release=threading.Event();self.published=[]
    def state(self):return {'frame':0,'fps':20,'epoch':0,'playing':True,'backend_ready':True,'behavior_state':self.event_state()}
    def record(self,**data):pass
    def expression(self,*args):pass
    def invalidate(self):pass
    def halt(self):pass
    def begin(self,step,valid):self.started.set();self.release.wait(3);return 0
    def publish(self,state,chat):self.published.append((state,chat))

class Tests(unittest.TestCase):
    def test_hybrid_clock_ignores_background_playback(self):
        m=Motor();m.note_interaction();self.assertFalse(m.event_mode_active())
        m.last_interaction-=1801;self.assertTrue(m.event_mode_active())
        m.note_interaction();self.assertFalse(m.event_mode_active())
        m.params['behavior_mode']='event';self.assertTrue(m.event_mode_active())
        m.params['behavior_mode']='stream';self.assertFalse(m.event_mode_active())
    def test_policy_switches_locally_without_llm(self):
        m=Motor();c=AgentController(m,start_threads=False);m.note_interaction();m.last_interaction-=1801;m.features=object()
        c.tick_policy();self.assertEqual(c.queue[0].action,'policy-rest')
        c.queue=[];m.features=None;m.note_interaction();c.tick_policy();self.assertEqual(c.queue[0].action,'policy-active')
    def test_queue_edits_preserve_current(self):
        for mode,expected in [('revise',['jump','terminal-idle']),('insert',['jump','wave','terminal-idle']),('append',['wave','jump','terminal-idle']),('clear',[])]:
            c=AgentController(Motor(),start_threads=False);current=Step(action='walk');c.current=current;c.queue=[Step(action='wave')]
            plan=Plan(reply='test',mode=mode,steps=[] if mode=='clear' else [Step(action='jump')])
            with patch('controller.completion',return_value=plan):c._request({},[],{},0,False)
            self.assertIs(c.current,current);self.assertEqual([s.action for s in c.queue],expected)
    def test_state_publish_is_not_blocked_by_motion_preparation(self):
        m=Motor();c=AgentController(m,start_threads=True)
        try:
            with c.lock:c.queue=[Step(action='wave')]
            self.assertTrue(m.started.wait(1));before=len(m.published);time.sleep(.4)
            self.assertGreater(len(m.published),before)
        finally:m.release.set();c.close()
    def test_config_bounds_and_legacy_mapping(self):
        self.assertEqual(validate({'event_mode':False})['behavior_mode'],'stream')
        for bad in [{'behavior_mode':'bad'},{'inactive_minutes':0},{'settle_buffer_s':100}]:
            with self.assertRaises(ValueError):validate(bad)
    def test_stop_latch_survives_mouse_interaction(self):
        m=Motor();c=AgentController(m,start_threads=False);m.note_interaction();c.cancel();m.note_interaction();c.tick_policy()
        self.assertTrue(m.transport_paused);self.assertEqual(c.queue,[])

if __name__=='__main__':unittest.main()
