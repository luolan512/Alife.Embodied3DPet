"""New dialogue must not cancel a body task that is still generating."""
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'packages'/p) for p in ('brain','scheduler','world','conversation')]
from core import Plan,Step
from controller import AgentController

spec=importlib.util.spec_from_file_location('chat_face_fixture',ROOT/'scripts/test-face-plan.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class Motor(fixture.Motor):
    def __init__(self):
        super().__init__();self.started=threading.Event();self.release=threading.Event()
        self.starts=[];self.valid_after=None;self.records=[];self.transport_paused=False
    def begin(self,step,valid):
        self.starts.append(step);self.started.set();self.release.wait(3)
        self.valid_after=valid()
        return 0 if self.valid_after else None
    def record(self,**data):self.records.append(data)
    def prefetch_prompts(self,steps):self.calls.append(('prefetch',list(steps)))

class Tests(unittest.TestCase):
    def controller(self):
        motor=Motor()
        c=AgentController(motor,config_loader=lambda:{'base_url':'http://local.test/v1','model':'test'},start_threads=False)
        c.face_generator=fixture.Face()
        return c,motor

    def request(self,c,plan):
        with patch('controller.completion',return_value=plan):
            c._request({},[],{},c.request_id,False)

    def test_first_segment_has_priority_over_plan_prefetch(self):
        c,m=self.controller();first=Step(action='sit');second=Step(action='wave');third=Step(action='look_around')
        c.install_plan([first,second,third],'three tasks')
        self.assertFalse(any(row[0]=='prefetch' for row in m.calls))
        thread=threading.Thread(target=c.tick);thread.start();self.assertTrue(m.started.wait(1))
        try:self.assertFalse(any(row[0]=='prefetch' for row in m.calls))
        finally:m.release.set();thread.join(3)
        warmed=[row[1] for row in m.calls if row[0]=='prefetch']
        self.assertEqual(warmed,[[second]])

    def test_speaking_during_generation_reuses_original_inflight_task(self):
        c,m=self.controller();first=Step(action='sit');second=Step(action='wave')
        c.install_plan([first,second],'two tasks');thread=threading.Thread(target=c.tick)
        thread.start();self.assertTrue(m.started.wait(1));mid=c.motion_id
        try:
            plan=Plan(reply='你好呀。',mode='auto',steps=[first.model_copy(update={'label':'已安排坐下'}),second.model_copy()])
            with patch('controller.completion',return_value=plan):
                c.submit('你好')
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    with c.lock:finished=not c.pending_request
                    if finished:break
                    time.sleep(.01)
                self.assertTrue(finished)
            with c.lock:
                self.assertIs(c.current,first);self.assertIs(c.queue[0],second)
                self.assertEqual(c.motion_id,mid);self.assertIsNone(c.segment_start_frame)
            self.assertFalse(any('first_motion_encode_ms' in row for row in m.records))
        finally:m.release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertTrue(m.valid_after)
        self.assertEqual(m.starts,[first]);self.assertEqual(c.segment_start_frame,0)

    def test_new_auto_reaction_appends_without_discarding_waiting_step(self):
        c,m=self.controller();c.current=Step(action='sit');waiting=Step(action='wave')
        c.queue=c.with_terminal_loop([waiting]);mid=c.motion_id
        self.request(c,Plan(reply='听你说。',mode='auto',steps=[Step(action='look_around')]))
        self.assertEqual(c.current.action,'sit');self.assertIs(c.queue[0],waiting)
        self.assertEqual(c.queue[1].action,'look_around');self.assertEqual(c.motion_id,mid)

    def test_echoed_waiting_queue_is_not_added_twice(self):
        c,m=self.controller();c.current=Step(action='sit');waiting=Step(action='wave')
        c.queue=c.with_terminal_loop([waiting]);old=list(c.queue)
        self.request(c,Plan(reply='它还在准备。',mode='auto',steps=[waiting.model_copy()]))
        self.assertEqual(c.queue,old);self.assertIs(c.queue[0],waiting)

    def test_auto_append_keeps_real_queued_loop_instead_of_dropping_it(self):
        c,m=self.controller();c.current=Step(action='sit');waiting=Step(action='walk',kind='loop')
        c.queue=[waiting]
        self.request(c,Plan(reply='听着呢。',mode='auto',steps=[Step(action='wave')]))
        self.assertIs(c.queue[0],waiting);self.assertEqual(c.queue[1].action,'wave')

    def test_after_reply_does_not_interrupt_preparing_passive_loop(self):
        c,m=self.controller();c.current=Step(action='stand',kind='loop');current=c.current
        c.segment_start_frame=None;c.reply_followup={'request_id':c.request_id,'deadline':0,'steps':[Step(action='wave')]}
        mid=c.motion_id;c.tick_reply_followup()
        self.assertIs(c.current,current);self.assertEqual(c.motion_id,mid)
        self.assertIsNotNone(c.reply_followup)
        c.segment_start_frame=20;c.tick_reply_followup()
        self.assertIs(c.current,current);self.assertEqual(c.motion_id,mid)
        c.segment_start_frame=0;c.tick_reply_followup()
        self.assertEqual(c.queue[0].action,'wave');self.assertGreater(c.motion_id,mid)

    def test_explicit_replace_still_interrupts_and_starts_new_plan(self):
        c,m=self.controller();c.current=Step(action='sit');c.queue=[Step(action='wave')]
        mid=c.motion_id
        self.request(c,Plan(reply='改成张望。',mode='replace',steps=[Step(action='look_around')]))
        self.assertIsNone(c.current);self.assertEqual(c.queue[0].action,'look_around')
        self.assertGreater(c.motion_id,mid)

    def test_auto_without_active_task_starts_new_plan(self):
        c,m=self.controller();self.request(c,Plan(reply='挥挥手。',mode='auto',steps=[Step(action='wave')]))
        self.assertEqual(c.queue[0].action,'wave');self.assertEqual(c.motion_id,1)

if __name__=='__main__':unittest.main()
