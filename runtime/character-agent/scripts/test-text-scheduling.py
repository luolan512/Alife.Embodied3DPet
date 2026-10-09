"""Priority/fairness regression with a cancellable fake encoder; no model calls."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/ardy-worker'))
from text_schedule import TextScheduler,TextPreparationPreempted

class Encoder:
    def __init__(self):
        self.values={};self.cache=SimpleNamespace(contains=lambda prompt:prompt in self.values)
        self.encoder=self;self.cancelled=threading.Event();self.entered=threading.Event()
        self.release=threading.Event();self.calls=[];self.cancel_calls=0
        self.block='slow';self.blocks_once=False
    def cancel_current(self):self.cancel_calls+=1;self.cancelled.set()
    def __call__(self,texts):
        prompt=texts[0]
        if prompt in self.values:return self.values[prompt]
        self.calls.append(prompt)
        if prompt==self.block and (not self.blocks_once or self.calls.count(prompt)==1):
            self.cancelled.clear();self.entered.set();deadline=time.monotonic()+2
            while not self.release.wait(.005):
                if self.cancelled.is_set():raise RuntimeError('encoder cancelled')
                if time.monotonic()>deadline:raise RuntimeError('test encoder timeout')
        result=(prompt,None);self.values[prompt]=result
        return result

class Tests(unittest.TestCase):
    def job(self,scheduler,encoder,prompt,priority,valid=lambda:True,on_timing=None):
        errors=[];result=[]
        def run():
            try:result.append(scheduler.encode(encoder,prompt,valid,priority,on_timing))
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        return thread,errors,result
    def pending(self,scheduler,count):
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            with scheduler.condition:
                if len(scheduler.pending)>=count:return
            time.sleep(.005)
        self.fail('request did not queue')
    def finish(self,*jobs):
        for thread,errors,result in jobs:
            thread.join(3);self.assertFalse(thread.is_alive())
    def test_current_prompt_beats_already_queued_background_without_cancellation(self):
        s=TextScheduler();e=Encoder();order=[]
        # No cooperative cancel: even then the current request takes the next slot.
        e.encoder=None
        active=self.job(s,e,'slow',2);self.assertTrue(e.entered.wait(1))
        future=self.job(s,e,'future',2,on_timing=lambda **kw:order.append('future'))
        self.pending(s,1)
        current=self.job(s,e,'current',0,on_timing=lambda **kw:order.append('current'))
        self.pending(s,2);e.release.set();self.finish(active,current,future)
        self.assertEqual(e.calls,['slow','current','future']);self.assertEqual(order,['current','future'])
        self.assertFalse(active[1]+current[1]+future[1])
    def test_same_prompt_is_completed_once_and_foreground_gets_result_before_future(self):
        s=TextScheduler();e=Encoder();order=[]
        active=self.job(s,e,'slow',2);self.assertTrue(e.entered.wait(1))
        future=self.job(s,e,'future',2,on_timing=lambda **kw:order.append('future'))
        self.pending(s,1)
        current=self.job(s,e,'slow',0,on_timing=lambda **kw:order.append('current'))
        self.pending(s,2);e.release.set();self.finish(active,current,future)
        self.assertEqual(e.calls,['slow','future']);self.assertEqual(e.cancel_calls,0)
        self.assertEqual(order,['current','future']);self.assertEqual(current[2],[('slow',None)])
    def test_unrelated_background_yields_and_can_retry_after_current(self):
        s=TextScheduler();e=Encoder();e.blocks_once=True;errors=[]
        def background():
            try:s.encode(e,'slow',lambda:True,2)
            except TextPreparationPreempted:
                s.encode(e,'slow',lambda:True,2)
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=background);thread.start();self.assertTrue(e.entered.wait(1))
        current=self.job(s,e,'current',0);self.finish(current);thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(errors+current[1])
        self.assertEqual(e.calls,['slow','current','slow']);self.assertGreater(e.cancel_calls,0)
    def test_cache_hit_is_immediate_while_other_text_is_busy(self):
        s=TextScheduler();e=Encoder();e.values['cached']=('cached',None)
        active=self.job(s,e,'slow',2);self.assertTrue(e.entered.wait(1))
        current=self.job(s,e,'cached',0);self.finish(current)
        self.assertEqual(current[2],[('cached',None)]);self.assertTrue(active[0].is_alive())
        e.release.set();self.finish(active)
    def test_cancelled_queued_request_never_runs(self):
        s=TextScheduler();e=Encoder();valid=[True]
        active=self.job(s,e,'slow',0);self.assertTrue(e.entered.wait(1))
        queued=self.job(s,e,'obsolete',2,valid=lambda:valid[0]);self.pending(s,1)
        valid[0]=False;self.finish(queued);self.assertIsInstance(queued[1][0],RuntimeError)
        e.release.set();self.finish(active);self.assertEqual(e.calls,['slow'])
    def test_queue_wait_is_reported_separately_from_cached_compute(self):
        s=TextScheduler();e=Encoder();timing=[]
        active=self.job(s,e,'slow',2);self.assertTrue(e.entered.wait(1))
        current=self.job(s,e,'slow',0,on_timing=lambda **kw:timing.append(kw));self.pending(s,1)
        time.sleep(.03);e.release.set();self.finish(active,current)
        self.assertTrue(timing[0]['cache_hit']);self.assertGreater(timing[0]['queue_ms'],20)
        self.assertLess(timing[0]['compute_ms'],timing[0]['queue_ms'])

if __name__=='__main__':unittest.main()
