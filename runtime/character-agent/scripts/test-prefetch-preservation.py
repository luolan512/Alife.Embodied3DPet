"""Queue updates preserve an encoder job still needed by the waiting action."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services/ardy-worker'),str(ROOT/'packages/brain')]
from worker import ArdyWorker
from core import Step

class Tests(unittest.TestCase):
    def worker(self):
        w=object.__new__(ArdyWorker);w.ready=True;w.closed=False;w.lock=threading.RLock()
        w.prefetch_serial=0;w.warmed=set();w.calls=[];w.validities=[]
        w.encoder=SimpleNamespace(cache=SimpleNamespace(contains=lambda text:text in w.warmed))
        w.record=lambda **data:None
        return w

    def wait_finished(self,w):
        deadline=time.monotonic()+2
        while w.prefetch_running and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(w.prefetch_running)

    def test_appending_does_not_cancel_or_restart_slow_encoding(self):
        w=self.worker();first=Step(action='wave');second=Step(action='look_around')
        entered=threading.Event();release=threading.Event()
        def encode(prompt,valid,**kwargs):
            w.calls.append(prompt)
            if prompt==first.motion_prompt:entered.set();release.wait(2)
            w.validities.append(valid())
            if not valid():raise RuntimeError('cancelled')
            w.warmed.add(prompt)
        w.encode_prompt=encode;w.prefetch_prompts([first]);self.assertTrue(entered.wait(1))
        try:
            serial=w.prefetch_serial
            w.prefetch_prompts([first,second]);w.prefetch_prompts([first,second])
            self.assertEqual(w.prefetch_serial,serial)
        finally:release.set()
        self.wait_finished(w)
        self.assertEqual(w.calls,[first.motion_prompt])
        w.prefetch_prompts([second]);self.wait_finished(w)
        self.assertEqual(w.calls,[first.motion_prompt,second.motion_prompt])
        self.assertEqual(w.validities,[True,True])

    def test_changed_first_prompt_can_cancel_obsolete_prefetch(self):
        w=self.worker();first=Step(action='wave');second=Step(action='look_around')
        entered=threading.Event();release=threading.Event();old_done=threading.Event()
        def encode(prompt,valid,**kwargs):
            if prompt==first.motion_prompt:
                entered.set();release.wait(2);w.validities.append(valid());old_done.set()
                if not valid():raise RuntimeError('cancelled')
            w.warmed.add(prompt)
        w.encode_prompt=encode;w.prefetch_prompts([first]);self.assertTrue(entered.wait(1))
        serial=w.prefetch_serial
        try:w.prefetch_prompts([second]);self.assertGreater(w.prefetch_serial,serial)
        finally:release.set()
        self.assertTrue(old_done.wait(1));self.wait_finished(w)
        self.assertEqual(w.validities,[False]);self.assertIn(second.motion_prompt,w.warmed)

if __name__=='__main__':unittest.main()
