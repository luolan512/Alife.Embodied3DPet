"""Bounded playback-grounded observer. Never generates, stops or repairs motion."""
import collections
import gzip
import json
import math
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT=Path(__file__).resolve().parents[2]
STREAMS=('source','retarget','rendered')
CANDIDATES=[('站立','A person stands upright and still.'),('走路','A person walks forward.'),
 ('跑步','A person runs.'),('挥手','A person waves their right hand.'),
 ('坐着','A person sits on the ground.'),('跪着','A person kneels.'),
 ('躺着','A person lies on the ground.'),('蹲下','A person crouches.'),
 ('跳舞','A person dances.'),('举手','A person raises both arms above their head.')]
DEFAULTS={'enabled':False,'interval_s':1.5,'baseline_drop':.12,'competitor_margin':.06,
          'low_similarity':-1.,'confirm_windows':3,'record_samples':False}

def target_text(prompt):
    if prompt.startswith('Cached idle: '):
        return {'stand':CANDIDATES[0][1],'sit':CANDIDATES[4][1],'kneel':CANDIDATES[5][1],
                'lie':CANDIDATES[6][1]}.get(prompt.split(': ',1)[1], '')
    if prompt in ('Held pose snapshot',''):return ''
    return prompt[:300]

def geometry(samples,stream):
    p=np.asarray([s[stream] for s in samples],dtype=float)
    scale=float(np.median(np.linalg.norm(p[:,1]-p[:,4],axis=1)+np.linalg.norm(p[:,4]-p[:,7],axis=1)))
    scale=max(scale,.1)
    spine=p[-1,12]-p[-1,0]
    tilt=math.degrees(math.acos(np.clip(spine[1]/max(np.linalg.norm(spine),1e-8),-1,1)))
    times=np.asarray([s['frame']/s['fps'] for s in samples])
    speed=np.linalg.norm(np.diff(p,axis=0),axis=2)/np.maximum(np.diff(times)[:,None],1e-6)
    # Proxy tests retain world coordinates; semantic preprocessing removes global placement.
    flags=[]
    if len(speed) and float(np.max(speed))/scale>18:flags.append('JOINT_SPEED_SPIKE')
    pelvis_delta=float(p[-1,0,1]-p[0,0,1])
    if abs(pelvis_delta)/scale>.6:flags.append('HIP_HEIGHT_CHANGE')
    a,b=p[-1,0],p[-1,12];axis=b-a;length2=float(axis@axis)
    width=float(np.linalg.norm(p[-1,16]-p[-1,17]));radius=width*.24
    deepest=0.
    if length2>1e-6:
        for elbow,hand in ((18,20),(19,21)):
            point=p[-1,elbow]*.3+p[-1,hand]*.7
            t=float(np.clip((point-a)@axis/length2,0,1))
            if .12<t<.88:deepest=max(deepest,radius-float(np.linalg.norm(point-(a+t*axis))))
    if deepest/scale>.045:flags.append('FOREARM_TORSO_OVERLAP_PROXY')
    return {'flags':flags,'torso_tilt_degrees':round(tilt,1),'hip_delta_m':round(pelvis_delta,3),
            'peak_joint_speed_m_s':round(float(np.max(speed)) if len(speed) else 0,2),
            'forearm_overlap_proxy_m':round(max(0,deepest),3)}

def make_clips(samples):
    clips=[];labels=[]
    end=samples[-1]['frame']/samples[-1]['fps']
    for window in (2,4,8):
        start=end-window
        selected=[s for s in samples if s['frame']/s['fps']>=start-.06]
        times=np.asarray([s['frame']/s['fps'] for s in selected])
        if len(times)<2 or times[-1]-times[0]<window-.15:continue
        if np.max(np.diff(times))>.16:continue  # Never bridge a missing playback interval.
        grid=np.arange(times[-1]-window+.05,times[-1]+.001,.05)
        for stream in STREAMS:
            points=np.asarray([s[stream] for s in selected],dtype=np.float32)
            resampled=np.stack([np.interp(grid,times,points[:,j,k]) for j in range(22) for k in range(3)],axis=1).reshape(-1,22,3)
            clips.append(resampled.tolist());labels.append((stream,window))
    return clips,labels

class DriftDecision:
    """Exploratory relative detector. Its bootstrap reference is explicitly unverified."""
    def __init__(self):self.references={};self.counts={};self.verified=set()
    def update(self,key,score,competitor,config):
        reference=self.references.setdefault(key,[])
        if len(reference)<3:reference.append(score)
        baseline=float(np.median(reference))
        drop=baseline-score
        suspect=score<config['low_similarity'] or (drop>config['baseline_drop'] and competitor-score>config['competitor_margin'])
        self.counts[key]=self.counts.get(key,0)+1 if suspect else 0
        return {'reference_cosine':round(baseline,4),'reference_verified':key in self.verified,'drop':round(drop,4),
                'consecutive_suspect':self.counts[key],
                'status':'possible_drift' if self.counts[key]>=config['confirm_windows'] else 'suspect' if suspect else 'no_drift_signal'}

class MotionMonitor:
    def __init__(self,trace=None,start_thread=True,config_path=None):
        self.trace=trace;self.lock=threading.RLock();self.closed=False;self.proc=None
        self.config_path=Path(config_path or ROOT/'config/motion-observer.json')
        self.config=dict(DEFAULTS)
        if self.config_path.exists():self.config.update(json.loads(self.config_path.read_text(encoding='utf-8-sig')))
        self.samples=collections.deque(maxlen=220);self.identity=None;self.version=0
        self.decision=DriftDecision();self.last_received=0;self.last_evaluated=-1;self.sequence=0
        self.result=None;self.error=None;self.phase='waiting_for_playback';self.history=collections.deque(maxlen=12)
        self.recorded_bytes=0;self.recording_limit=32*1024*1024;self.record_file=None
        if trace:self.record_file=trace.run_dir/'motion-observer-samples.jsonl.gz'
        self.wake=threading.Event()
        if start_thread:threading.Thread(target=self._loop,daemon=True,name='motion-observer').start()

    def configure(self,data):
        allowed=set(DEFAULTS)
        if not isinstance(data,dict) or set(data)-allowed:raise ValueError('Unknown motion observer option')
        cfg={**self.config,**data}
        for k in ('enabled','record_samples'):
            if type(cfg[k]) is not bool:raise ValueError(k+' must be boolean')
        for k,lo,hi in (('interval_s',.5,10),('baseline_drop',.02,1),('competitor_margin',0,1),('low_similarity',-1,1)):
            if type(cfg[k]) not in (float,int) or not math.isfinite(cfg[k]) or not lo<=cfg[k]<=hi:raise ValueError('Invalid '+k)
        if type(cfg['confirm_windows']) is not int or not 2<=cfg['confirm_windows']<=10:raise ValueError('Invalid confirm_windows')
        with self.lock:
            self.config=cfg;self.reset('configuration_changed')
            self.config_path.parent.mkdir(exist_ok=True)
            temp=self.config_path.with_suffix('.tmp');temp.write_text(json.dumps(cfg,indent=2),encoding='utf-8');temp.replace(self.config_path)
        self.wake.set();return self.snapshot()

    def reset(self,reason='runtime_changed'):
        with self.lock:
            self.version+=1;self.samples.clear();self.identity=None;self.result=None;self.last_evaluated=-1
            self.last_received=0;self.decision=DriftDecision();self.phase=reason

    def offer(self,data):
        if not self.config['enabled']:return
        incoming=data.get('motion_samples',[])
        if not isinstance(incoming,list):return
        with self.lock:
            for sample in incoming[-32:]:
                if not isinstance(sample,dict):continue
                frame=sample.get('frame');fps=sample.get('fps');epoch=sample.get('epoch')
                if type(frame) is not int or frame<0 or frame>data.get('frame',-1) or epoch!=data.get('epoch'):continue
                if type(fps) not in (int,float) or not 1<=fps<=120:continue
                if not isinstance(sample.get('prompt'),str):continue
                valid=True
                for stream in STREAMS:
                    try:p=np.asarray(sample.get(stream),dtype=float)
                    except (TypeError,ValueError):valid=False;break
                    if p.shape!=(22,3) or not np.isfinite(p).all() or np.max(np.abs(p))>10000:valid=False;break
                if not valid:continue
                target=target_text(sample['prompt'])
                generation=sample.get('playback_generation',0)
                if type(generation) is not int:continue
                identity=(epoch,target,fps,generation)
                if self.identity!=identity:
                    self.reset('warming_up');self.identity=identity
                if self.samples and frame<=self.samples[-1]['frame']:continue
                if self.samples and (frame-self.samples[-1]['frame'])/fps>.16:
                    self.reset('playback_gap');self.identity=identity
                clean={k:sample[k] for k in ('frame','epoch','fps','prompt',*STREAMS)}
                clean['playback_generation']=generation
                clean['estimated_joints']=sample.get('estimated_joints',[])[:4]
                self.samples.append(clean);self.last_received=time.monotonic()
                if self.record_file and self.config['record_samples'] and self.recorded_bytes<self.recording_limit:
                    line=json.dumps(clean,separators=(',',':'))+'\n'
                    with gzip.open(self.record_file,'at',encoding='utf-8') as output:output.write(line)
                    self.recorded_bytes=self.record_file.stat().st_size
        self.wake.set()

    def snapshot(self):
        with self.lock:
            age=round(time.monotonic()-self.last_received,1) if self.last_received else None
            fresh=age is not None and age<3
            result_fresh=fresh and self.result is not None and bool(self.samples) and (self.samples[-1]['frame']-self.result['frame'])/self.samples[-1]['fps']<4
            return {'enabled':self.config['enabled'],'mode':'observe_only','phase':self.phase if fresh else 'stale_or_no_playback',
                'backend':'TMR-HumanML3D-CPU','error':self.error,'config':dict(self.config),
                'age_s':age,'fresh':fresh,'result_fresh':result_fresh,'target':self.identity[1] if self.identity else '',
                'sample_count':len(self.samples),'evaluation_count':self.sequence,'result':self.result,
                'recent_events':list(self.history),'recorded_bytes':self.recorded_bytes,
                'recording_limit_reached':self.recorded_bytes>=self.recording_limit}

    def annotate(self,kind,note=''):
        if kind not in ('normal','drift_start','baseline_good'):raise ValueError('Invalid annotation kind')
        with self.lock:
            if not self.samples:raise ValueError('No played motion to annotate')
            if kind=='baseline_good':
                if not self.snapshot()['result_fresh']:raise ValueError('Wait for a fresh TMR evaluation')
                for row in self.result['windows']:
                    key=(row['stream'],row['window_s'])
                    self.decision.references[key]=[row['target_cosine']]*3
                    self.decision.counts[key]=0;self.decision.verified.add(key)
            marker={'kind':kind,'note':str(note)[:160],'frame':self.samples[-1]['frame'],
                    'epoch':self.identity[0],'target':self.identity[1],'time':time.time()}
            if self.trace:self.trace.record('motion_observer_annotation',marker)
            self.history.append(marker)
            return marker

    def _start_backend(self):
        self.phase='loading_tmr'
        env=__import__('os').environ.copy();env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false',PYTHONIOENCODING='utf-8')
        err_path=self.trace.run_dir/'motion-observer-backend.log' if self.trace else ROOT/'.cache/motion-observer-backend.log'
        err_path.parent.mkdir(parents=True,exist_ok=True)
        with err_path.open('a',encoding='utf-8') as err:
            self.proc=subprocess.Popen([sys.executable,'-u',str(Path(__file__).with_name('tmr_backend.py'))],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,encoding='utf-8',env=env,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.responses=queue.Queue()
        def read():
            for line in self.proc.stdout:self.responses.put(line)
            self.responses.put('{"error":"TMR subprocess exited"}')
        threading.Thread(target=read,daemon=True).start()
        result=json.loads(self.responses.get(timeout=90))
        if not result.get('ready'):raise RuntimeError(result.get('error','TMR not ready'))

    def _loop(self):
        next_evaluation=0
        while not self.closed:
            self.wake.wait(.5);self.wake.clear()
            with self.lock:
                if not self.config['enabled'] or len(self.samples)<38 or time.monotonic()-self.last_received>3 or time.monotonic()<next_evaluation:continue
                if self.samples[-1]['frame']==self.last_evaluated:continue
                samples=list(self.samples);identity=self.identity;version=self.version;cfg=dict(self.config)
            try:
                evaluation_started=time.monotonic()
                if self.proc is None:
                    self._start_backend()
                    continue  # Loading may take seconds; resnapshot the latest played window.
                clips,labels=make_clips(samples)
                if not clips:continue
                if not identity[1]:
                    with self.lock:self.phase='unknown_target'
                    continue
                texts=[identity[1]]+[text for _,text in CANDIDATES]
                self.proc.stdin.write(json.dumps({'clips':clips,'texts':texts})+'\n');self.proc.stdin.flush()
                response=json.loads(self.responses.get(timeout=30))
                if 'error' in response:raise RuntimeError(response['error'])
                rows=[]
                with self.lock:
                    if version!=self.version or identity!=self.identity or not self.config['enabled']:continue
                    for (stream,window),scores in zip(labels,response['scores']):
                        best=max(range(len(CANDIDATES)),key=lambda i:scores[i+1])
                        decision=self.decision.update((stream,window),scores[0],scores[best+1],cfg)
                        rows.append({'stream':stream,'window_s':window,'target_cosine':round(scores[0],4),
                            'nearest_candidate':CANDIDATES[best][0],'candidate_cosine':round(scores[best+1],4),**decision})
                    geo={stream:geometry(samples[-min(len(samples),80):],stream) for stream in STREAMS}
                    final=[r for r in rows if r['stream']=='rendered']
                    status='possible_drift' if any(r['status']=='possible_drift' for r in final) else 'suspect' if any(r['status']=='suspect' for r in final) else 'no_drift_signal'
                    self.sequence+=1;self.last_evaluated=samples[-1]['frame'];self.phase='observing';self.error=None
                    self.result={'status':status,'frame':self.last_evaluated,'epoch':identity[0],'target':identity[1],
                        'score_kind':'cosine_similarity_not_probability','windows':rows,'geometry':geo,
                        'inference_ms':response['inference_ms'],'estimated_joints':samples[-1]['estimated_joints'],
                        'calibration':'exploratory_unverified_bootstrap','automatic_repair':False}
                    event={'frame':self.last_evaluated,'status':status,'target':identity[1],
                           'nearest_candidate':final[0]['nearest_candidate'] if final else 'unknown'}
                    if not self.history or self.history[-1].get('status')!=status:self.history.append(event)
                    if self.trace:self.trace.record('motion_observer',self.result)
                # Rest after inference too; slow CPU inference must not turn into
                # a back-to-back loop that monopolizes both worker threads.
                next_evaluation=time.monotonic()+cfg['interval_s']
            except Exception as exc:
                with self.lock:self.error=str(exc)[:400];self.phase='backend_error';self.result=None
                if self.proc:self.proc.terminate();self.proc=None
                next_evaluation=time.monotonic()+30

    def close(self):
        self.closed=True;self.wake.set()
        if self.proc:self.proc.terminate()
