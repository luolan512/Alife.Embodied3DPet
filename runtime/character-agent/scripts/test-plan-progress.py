import sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'packages'/'brain'),str(ROOT/'packages'/'scheduler'),str(ROOT/'packages'/'world'),str(ROOT/'packages'/'conversation')]
from core import Step
from controller import AgentController
from store import ConversationStore

class Motor:
    owner=True
    def __init__(self):self.frame=0;self.playing=True;self.prompt='';self.calls=[]
    def state(self):return {'frame':self.frame,'epoch':0,'fps':20,'playing':self.playing,'backend_ready':True,'position':[0,0,0],'motion_parameters':{'auto_replan':True},'prompt':self.prompt}
    def begin(self,step,valid):self.prompt=step.motion_prompt;self.calls.append(step);return self.frame
    def record(self,**kw):pass
    def invalidate(self):pass
    def halt(self):self.playing=False

class Tests(unittest.TestCase):
    def make(self):m=Motor();return AgentController(m,start_threads=False),m
    def test_missing_terminal_loop_is_added_and_wave_stops_being_condition(self):
        c,m=self.make();c.install_plan([Step(action='wave',label='打招呼',duration_s=2)],'打招呼后等待')
        self.assertEqual(c.queue[-1].kind,'loop');c.tick();m.frame=41;c.tick();c.tick()
        self.assertEqual(c.current.kind,'loop');self.assertNotIn('wave',m.prompt)
        m.frame=400;c.tick();self.assertEqual(c.current.kind,'loop')
        plans=c.snapshot()['plan_progress']['plans'];self.assertEqual(plans[0]['steps'][0]['status'],'completed');self.assertEqual(plans[0]['steps'][1]['status'],'looping')
    def test_progress_distinguishes_playing_pause_and_loop(self):
        c,m=self.make();c.install_plan([Step(action='wave',duration_s=4)],'task');c.tick();m.frame=40
        entry=c.snapshot()['plan_progress']['plans'][0]['steps'][0];self.assertAlmostEqual(entry['progress'],.5);self.assertEqual(entry['phase'],'action')
        m.playing=False;self.assertEqual(c.snapshot()['plan_progress']['plans'][0]['steps'][0]['phase'],'paused')
    def test_history_persists_and_restart_does_not_resume_old_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ConversationStore(Path(folder)/'test.db');c,m=self.make();c.plan_timeline.on_save=store.save_plan
            c.install_plan([Step(action='wave')],'task');c.tick();m.frame=100;c.tick();c.tick()
            self.assertEqual(store.plans()[0]['steps'][-1]['status'],'looping')
            c2,_=self.make();c2.plan_timeline.restore(store.plans());self.assertIsNone(c2.current)
            self.assertEqual(c2.snapshot()['plan_progress']['plans'][0]['steps'][-1]['status'],'interrupted')
            c.cancel();self.assertEqual(store.plans()[0]['steps'][-1]['status'],'cancelled')
            store.db.close()
    def test_existing_loop_is_not_duplicated(self):
        c,_=self.make();c.install_plan([Step(action='wave'),Step(action='stand',kind='loop')],'task');self.assertEqual(len(c.queue),2)
    def test_intermediate_loop_yields_to_next_step(self):
        c,m=self.make();c.install_plan([Step(action='walk',kind='loop',duration_s=2),Step(action='wave')],'task');c.tick()
        m.frame=10;c.tick();self.assertEqual(c.current.action,'walk');m.frame=41;c.tick();c.tick();self.assertEqual(c.current.action,'wave')

if __name__=='__main__':unittest.main()
