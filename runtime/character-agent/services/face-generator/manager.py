"""Latest-request CUDA/CPU face worker; loading never blocks the agent lock."""
import json,subprocess,sys,threading,time,secrets,collections,os
from pathlib import Path
from emotion import EmotionState,english
ROOT=Path(__file__).resolve().parents[2]

class FaceGenerator:
    def __init__(self,emit,active=lambda:True):
        self.emit=emit;self.lock=threading.RLock();self.process=None;self.closed=False
        self.enabled=os.environ.get('CHARACTER_FACE_ENABLED','1')!='0';self.gain=1.;self.ready=False;self.sequence=0;self.latest=None;self.busy=False
        self.error=None;self.first_batch_ms=None;self.total_ms=None;self.wake=threading.Event()
        self.device='loading';self.device_reason=None
        self.history=collections.deque(maxlen=8)
        self.retained=None
        self.affect=EmotionState();self.active=active;self.continuous=True;self.guidance=True
        self.next_window=0;self.context_text='';self.windows=0;self.stopped=False
        self.stream=None;self.speaking_until=0
        self.action=None;self.action_until=0;self.action_id=0
        self.action_controls={}
        threading.Thread(target=self._loop,daemon=True,name='face-generator').start()
        threading.Thread(target=self._speech_clock,daemon=True,name='face-speech-clock').start()

    def snapshot(self):
        with self.lock:return {'enabled':self.enabled,'gain':self.gain,'ready':self.ready,'busy':self.busy,'error':self.error,'id':self.sequence,'first_batch_ms':self.first_batch_ms,'total_ms':self.total_ms,'device':self.device,'device_reason':self.device_reason,'mapping':'FLAME landmark approximation','history':list(self.history),'continuous':self.continuous,'guidance':self.guidance,'affect':self.affect.snapshot(),'windows':self.windows,'action':dict(self.action,hold_s=max(0,self.action_until-time.monotonic())) if self.action and self.action_until>time.monotonic() else None}

    def configure(self,data):
        if not isinstance(data,dict) or set(data)-{'enabled','gain','continuous','guidance'}:raise ValueError('Unknown face setting')
        for k in ('continuous','guidance'):
            if k in data and type(data[k]) is not bool:raise ValueError(k+' must be boolean')
        if 'enabled' in data and type(data['enabled']) is not bool:raise ValueError('enabled must be boolean')
        if 'gain' in data and (type(data['gain']) not in (int,float) or not 0<=data['gain']<=1.5):raise ValueError('gain must be 0..1.5')
        with self.lock:
            self.gain=data.get('gain',self.gain)
            self.continuous=data.get('continuous',self.continuous);self.guidance=data.get('guidance',self.guidance)
            if 'enabled' in data and data['enabled']!=self.enabled:
                self.enabled=data['enabled'];self.sequence+=1;self.latest=None
                if not self.enabled:self._stop()
            self.emit({'type':'face_config','enabled':self.enabled,'gain':self.gain,'continuous':self.continuous,'guidance':self.guidance,'device':self.device})
            self.wake.set()
            return self.snapshot()

    def target(self,emotion):
        with self.lock:
            if self.enabled and emotion!=self.affect.label:
                self.emit(self.affect.set(emotion));self.context_text=self.affect.continuation_text('');self.next_window=time.monotonic()

    def replay(self):
        with self.lock:
            packets=[{'type':'face_config','enabled':self.enabled,'gain':self.gain,'continuous':self.continuous,'guidance':self.guidance,'device':self.device},self.affect.packet()]
            if self.enabled and self.retained:packets.append(dict(self.retained,gain=self.gain))
            stream=self.stream
            if self.enabled and stream and stream['request']['id']==self.sequence and not stream['done']:
                request=stream['request'];packets.append({'type':'face_pending',**{k:request[k] for k in ('id','emotion','continuation')}})
                if stream['frames']:packets.append({'type':'face_batch','id':request['id'],'offset':0,'first':True,'frames':list(stream['frames']),'fps':24,'gain':self.gain,'speaking_s':0,'continuation':True})
            elif self.enabled and self.latest:packets.append({'type':'face_pending',**{k:self.latest[k] for k in ('id','emotion','continuation')}})
            remaining=max(0,self.speaking_until-time.monotonic())
            packets.append({'type':'face_speech','id':self.sequence,'duration_s':remaining})
            for channel,(value,until) in self.action_controls.items():
                remaining=until-time.monotonic()
                if remaining>0:packets.append({'type':'face_action','id':self.action_id,channel:value,'hold_s':remaining})
            return packets

    def set_action(self,data):
        # These VRM controls work even without a loaded CTEG worker.
        from core import FaceAction
        action=FaceAction.model_validate(data)
        with self.lock:
            self.action_id+=1;self.action_until=time.monotonic()+action.hold_s
            self.action={'type':'face_action','id':self.action_id,**action.model_dump(exclude_none=True)}
            for channel in ('mouth_open','eyes_closed'):
                value=getattr(action,channel)
                if value is not None:self.action_controls[channel]=(value,self.action_until)
            self.emit(dict(self.action))
            return dict(self.action)

    def request(self,text,emotion='neutral',speaking_s=0,seed=None,state=None,preserve_state=False):
        text=english(text)
        if not text:raise ValueError('face_text needs English description; Chinese input is converted by the test endpoint')
        if emotion not in ('neutral','happy','sad','angry','relaxed'):raise ValueError('Invalid emotion')
        if type(speaking_s) not in (int,float) or not 0<=speaking_s<=30:raise ValueError('Invalid speaking duration')
        if seed is not None and (type(seed) is not int or not 0<=seed<2**31):raise ValueError('Invalid seed')
        with self.lock:
            if not self.enabled:return {'accepted':False,'reason':'disabled'}
            if not (preserve_state and state is None and emotion==self.affect.label):self.emit(self.affect.set(emotion,state))
            self.context_text=text;self.stopped=False;self.next_window=time.monotonic()+5
            return self._queue(text,emotion,speaking_s,seed,False)

    def _queue(self,text,emotion,speaking_s,seed,continuation):
            self.sequence+=1
            self.latest={'id':self.sequence,'text':text.strip(),'emotion':emotion,'speaking_s':speaking_s,'seed':seed if seed is not None else secrets.randbelow(2**31),'queued_at':time.monotonic(),'continuation':continuation}
            if speaking_s>0:
                self.speaking_until=time.monotonic()+speaking_s
                self.emit({'type':'face_speech','id':self.sequence,'duration_s':speaking_s})
            self.first_batch_ms=None;self.total_ms=None
            self.emit({'type':'face_pending','id':self.sequence,'emotion':emotion,'speaking_s':speaking_s,'gain':self.gain,'text':text.strip(),'continuation':continuation})
            self.wake.set()
            return {'accepted':True,'id':self.sequence}

    def _speech_clock(self):
        while not self.closed:
            time.sleep(.05)
            with self.lock:
                if self.speaking_until and time.monotonic()>=self.speaking_until:self.stop_speech()

    def stop_speech(self):
        with self.lock:
            self.speaking_until=0;self.emit({'type':'face_speech','id':self.sequence,'duration_s':0})

    def set_state(self,emotion,state):
        with self.lock:
            packet=self.affect.set(emotion,state);self.emit(packet);self.stopped=False
            self.context_text=self.affect.continuation_text('');self.next_window=time.monotonic();self.wake.set()
            return self.snapshot()

    def reset(self):
        with self.lock:
            self.sequence+=1;self.latest=None;self.retained=None
            self.context_text='';self.stopped=True;self.emit(self.affect.set('neutral',{'intensity':0}))
            self.speaking_until=0;self.stream=None
            self.action=None;self.action_until=0
            self.action_controls={}
            self.emit({'type':'face_reset','id':self.sequence})
            return self.snapshot()

    def _stop(self):
        process=self.process;self.process=None;self.ready=False;self.busy=False
        if process and process.poll() is None:process.terminate()

    def _start(self):
        log=ROOT/'.run'/'face-worker-error.log';log.parent.mkdir(exist_ok=True)
        with log.open('a',encoding='utf-8') as stderr:
            process=subprocess.Popen([sys.executable,'-u',str(Path(__file__).with_name('engine.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        with self.lock:
            if self.closed or not self.enabled:process.terminate();return
            self.process=process
        line=process.stdout.readline()
        if not line:raise RuntimeError('Face worker failed to load; see face-worker-error.log')
        packet=json.loads(line)
        if packet.get('type')!='ready':raise RuntimeError('Face worker did not become ready')
        with self.lock:
            if process is self.process:
                self.ready=True;self.error=None;self.device=packet.get('device','cpu');self.device_reason=packet.get('device_reason')
                self.emit({'type':'face_config','enabled':self.enabled,'gain':self.gain,'continuous':self.continuous,'guidance':self.guidance,'device':self.device})

    def _loop(self):
        while not self.closed:
            self.wake.wait(.5);self.wake.clear()
            if not self.enabled:continue
            process=None
            try:
                if self.process is None:self._start()
                with self.lock:
                    now=time.monotonic()
                    if (not self.latest and self.continuous and not self.stopped and self.context_text and self.active()
                            and now>=self.next_window and self.affect.snapshot()['effective_intensity']>.025):
                        self._queue(self.context_text,self.affect.label,0,None,True);self.next_window=now+5
                    request=self.latest;self.latest=None;process=self.process
                    if not request or process is None:continue
                    self.busy=True
                    self.stream={'request':request,'frames':[],'done':False}
                process.stdin.write(json.dumps({k:v for k,v in request.items() if k!='queued_at'})+'\n');process.stdin.flush()
                first=True
                for line in iter(process.stdout.readline,''):
                    packet=json.loads(line)
                    with self.lock:
                        valid=self.enabled and request['id']==self.sequence and process is self.process
                        if valid and packet['type']=='batch':
                            self.stream['frames'].extend(packet['frames'])
                            latency=round((time.monotonic()-request['queued_at'])*1000,2)
                            if first:self.first_batch_ms=latency
                            self.emit({'type':'face_batch','id':request['id'],'offset':packet['offset'],'frames':packet['frames'],'fps':24,'first':first,'emotion':request['emotion'],'speaking_s':request['speaking_s'],'gain':self.gain,'latency_ms':latency,'continuation':request['continuation']})
                            self.retained={'type':'face_restore','id':request['id'],'frame':packet['frames'][-1],'emotion':request['emotion']}
                            first=False
                        if packet['type']=='done':
                            if valid:
                                self.stream['done']=True
                                self.total_ms=packet['elapsed_ms'];self.history.append({'id':request['id'],'text':request['text'],'emotion':request['emotion'],'first_batch_ms':self.first_batch_ms,'total_ms':self.total_ms})
                                self.windows+=1
                                self.emit({'type':'face_done','id':request['id'],'total_ms':self.total_ms})
                            break
                        if packet['type']=='error':raise RuntimeError(packet.get('error','Face generation failed'))
                if process.poll() is not None:raise RuntimeError('Face worker exited')
            except Exception as exc:
                with self.lock:
                    if self.enabled and (process is None or process is self.process):
                        self.error=str(exc);self.enabled=False
                        self.emit({'type':'face_error','error':str(exc),'enabled':False})
                    self._stop()
            finally:
                with self.lock:self.busy=False
                if self.latest:self.wake.set()

    def close(self):
        with self.lock:self.closed=True;self._stop();self.wake.set()
