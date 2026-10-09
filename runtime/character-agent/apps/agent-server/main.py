import math
"""Local character orchestration server. No business logic is placed in ARDY."""
import os
import sys
import json
import secrets
import threading
import time
import mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT=Path(__file__).resolve().parents[2]
WORKSPACE=ROOT.parent
os.environ.setdefault('CHECKPOINTS_DIR',str(WORKSPACE/'models'/'ardy'))
sys.path[:0]=[str(WORKSPACE/'ardy'),str(ROOT/'packages'/'brain'),str(ROOT/'packages'/'conversation'),str(ROOT/'services'/'ardy-worker'),str(ROOT/'packages'/'scheduler'),str(ROOT/'packages'/'world')]
os.environ.setdefault('ARDY_LLM_CONFIG',str(ROOT/'config'/'llm.json'))
os.environ.setdefault('KIMODO_TEXT_BUNDLE',str(WORKSPACE/'kimodo.cpp'/'Llama-3-Kimodo-Q4_K.gguf'))
os.environ.setdefault('KIMODO_ENCODE_SERVER',str(WORKSPACE/'kimodo.cpp'/'build'/'cpu'/'kmd-encode-server.exe'))
os.environ.setdefault('KIMODO_BACKEND','cpu')
os.environ.setdefault('KIMODO_THREADS','4')
for cpu_variable in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ.setdefault(cpu_variable,'4')
os.environ.setdefault('KIMODO_TEXT_LAYER_CHUNK','2')
os.environ.setdefault('LOCAL_CACHE','true')
for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')

from core import load_config,save_config,ai_configured,Step,completion
from persona import load_persona,save_persona,DEFAULT_PROMPT,MAX_LENGTH
sys.path.insert(0,str(ROOT/'packages'/'observability'))
from runtime_trace import TraceStore
from controller import AgentController
from active_look import ActiveLook
from model import WorldModel
sys.path.insert(0,str(ROOT/'packages'/'perception'))
from vision import VisionObserver
from perception_scheduler import PerceptionScheduler
from store import ConversationStore
from worker import ArdyWorker
import text_encoder_config
sys.path.insert(0,str(ROOT/'services'/'motion-monitor'))
from monitor import MotionMonitor
sys.path.insert(0,str(ROOT/'services'/'face-generator'))
from manager import FaceGenerator
from language import describe
from websockets.sync.server import serve

PORT=int(os.environ.get('CHARACTER_PORT','8092'))
MODELS=[{'id':p.stem,'name':p.stem,'path':p} for p in sorted((ROOT/'data'/'models').glob('*.vrm'))]
WS_PORT=PORT+1
TOKEN=secrets.token_urlsafe(32)
clients=set()
client_lock=threading.RLock()
owner=None
capabilities={}
ROOT.joinpath('data').mkdir(exist_ok=True)
store=ConversationStore(ROOT/'data'/('test-conversation.sqlite3' if os.environ.get('CHARACTER_TRACE_KIND')=='test' else 'conversation.sqlite3'))
trace=TraceStore(ROOT/'data'/'runs', kind=os.environ.get('CHARACTER_TRACE_KIND','live'), run_id=os.environ.get('CHARACTER_RUN_ID'))
trace.attach_console()

def emit(data):
    generator=globals().get('face_generator')
    if generator and data.get('type')=='expression':
        generator.target(data.get('emotion','neutral'))
        if data.get('face_text'):
            try:generator.request(data['face_text'],data.get('emotion','neutral'),preserve_state=True)
            except ValueError as exc:trace.record('face_request_invalid',{'error':str(exc)})
    if data.get('type')=='motion':trace.motion(data)
    elif data.get('type') not in ('state','chat'):trace.record('runtime_output', data)
    message=json.dumps(data,ensure_ascii=False,separators=(',',':'))
    with client_lock:targets=list(clients)
    for connection in targets:
        try:connection.send(message)
        except Exception:pass

world=WorldModel()
face_generator=FaceGenerator(emit,active=lambda:bool(clients))
perception_scheduler=PerceptionScheduler(trace=trace)
vision=VisionObserver(world,Path(os.environ.get('CHARACTER_VISION_CONFIG',str(ROOT/'config'/'vision.json'))),trace=trace,scheduler=perception_scheduler)
worker=ArdyWorker(emit, trace=trace)
worker.environment=world.snapshot
controller=AgentController(worker, trace=trace)
controller.face_generator=face_generator
motion_observer=MotionMonitor(trace=trace)
controller.motion_observer=motion_observer
active_look=ActiveLook(emit,world,lambda:vision.config['enabled'] and vision.config['active_enabled'] and worker.owner,trace=trace,scheduler=perception_scheduler,on_observed=vision.active_observed)
controller.perception=active_look
vision.background_allowed=lambda:not controller.pending_request and not controller.body_loop.inflight
def perception_state():
    status=vision.snapshot()
    return dict(enabled=status['config']['enabled'],active_enabled=status['config']['active_enabled'],
                background_enabled=status['config']['background_enabled'],runtime_connected=worker.owner,
                source=status['config']['source'],background_phase=status['phase'],error=status['error'],scheduler=perception_scheduler.snapshot())
world.perception_snapshot=perception_state
history=store.recent(100)
controller.chat=[('你' if m['role']=='user' else 'AI',m['content']) for m in history]
controller.history=[{'role':m['role'],'content':m['content']} for m in history[-8:]]
controller.conversation_id=store.conversation_id
def record_chat(role,content):
    store.append('user' if role=='你' else 'assistant',content)
    if role=='AI':emit({'type':'dialogue_reply','text':content})
controller.on_chat=record_chat
controller.plan_timeline.on_save=store.save_plan
controller.plan_timeline.restore(store.plans())
from alife_bridge import AlifeBridge
alife=AlifeBridge(controller,face_generator,emit)
last_view=None
last_sent=0

def publish(snapshot,chat):
    global last_view,last_sent
    # Keep the detailed scene/world snapshot supplied by WorldModel.
    snapshot['capabilities']=capabilities
    snapshot['usage']=trace.summary()
    snapshot['face_generator']=face_generator.snapshot()
    snapshot['vision']=vision.snapshot()
    snapshot['perception_scheduler']=perception_scheduler.snapshot()
    view=json.dumps(snapshot,ensure_ascii=False)
    if view!=last_view and time.monotonic()-last_sent>.3:
        emit({'type':'state','state':snapshot})
        last_view=view;last_sent=time.monotonic()
    emit_chat=json.dumps(controller.chat[-20:],ensure_ascii=False)
    if getattr(publish,'last_chat',None)!=emit_chat:
        emit({'type':'chat','messages':store.recent()})
        publish.last_chat=emit_chat

worker.on_publish=publish

class Handler(BaseHTTPRequestHandler):
    def send_json(self,data,status=200):
        blob=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(blob)
    def authorized(self):
        return self.headers.get('Authorization')=='Bearer '+TOKEN
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/v1/session':return self.send_json({'token':TOKEN,'ws_port':WS_PORT})
        if path=='/api/v1/health':return self.send_json({'service':'character-agent','adapter':'Marisa.Embodied3DPet','pid':os.getpid(),'backend':worker.state(),'connected':owner is not None})
        if path.startswith('/api/v1/'):
            if not self.authorized():return self.send_json({'error':'unauthorized'},401)
            if path.startswith('/api/v1/alife/'):
                alife.capabilities=capabilities
                return self.send_json(alife.get(path[len('/api/v1/alife/'):],parse_qs(urlparse(self.path).query)))
            if path=='/api/v1/state':return self.send_json(controller.snapshot())
            if path=='/api/v1/face':return self.send_json(face_generator.snapshot())
            if path=='/api/v1/motion-observer':return self.send_json(motion_observer.snapshot())
            if path=='/api/v1/history':
                cid=parse_qs(urlparse(self.path).query).get('conversation_id',[None])[0]
                return self.send_json(store.recent(conversation_id=cid))
            if path=='/api/v1/conversations':return self.send_json(store.sessions())
            if path=='/api/v1/usage':return self.send_json(trace.summary())
            if path=='/api/v1/vision':return self.send_json(vision.snapshot())
            if path=='/api/v1/perception':return self.send_json(perception_state())
            if path=='/api/v1/models':return self.send_json([{'id':m['id'],'name':m['name']} for m in MODELS])
            if path=='/api/v1/motion-config':return self.send_json(worker.params)
            if path=='/api/v1/text-encoder':return self.send_json(text_encoder_config.status(worker))
            if path=='/api/v1/persona':return self.send_json({**load_persona(),'default_prompt':DEFAULT_PROMPT,'max_length':MAX_LENGTH})
            if path=='/api/v1/config':
                cfg=load_config();cfg['configured']=ai_configured(cfg);cfg['has_api_key']=bool(cfg.get('api_key') or os.environ.get(cfg.get('api_key_env','ARDY_LLM_API_KEY')));cfg.pop('api_key',None)
                return self.send_json(cfg)
        model=next((m for m in MODELS if path==f"/models/{m['id']}.vrm"),None)
        if model:file=model['path']
        else:
            file=(ROOT/'dist'/'web'/path.lstrip('/')).resolve()
            if not file.is_relative_to((ROOT/'dist'/'web').resolve()):return self.send_json({'error':'not_found'},404)
            if file.is_dir():file=file/'index.html'
        if not file.is_file():return self.send_json({'error':'not_found'},404)
        self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
        self.end_headers();self.wfile.write(file.read_bytes())
    def do_POST(self):
        if not self.authorized():return self.send_json({'error':'unauthorized'},401)
        origin=self.headers.get('Origin')
        if origin and origin!=f'http://127.0.0.1:{PORT}':return self.send_json({'error':'origin_rejected'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>20000:raise ValueError('request_too_large')
            data=json.loads(self.rfile.read(length))
            path=urlparse(self.path).path
            if path=='/api/v1/text-encoder':
                text_encoder_config.save(data)
                return self.send_json(text_encoder_config.status(worker))
            if path.startswith('/api/v1/alife/'):return self.send_json(alife.post(path[len('/api/v1/alife/'):],data))
            if path in ('/api/v1/chat','/api/v1/face-test','/api/v1/autonomy','/api/v1/body-loop','/api/v1/config','/api/v1/persona','/api/v1/vision','/api/v1/vision-observe','/api/v1/perception-event'):
                return self.send_json({'error':'Alife owns dialogue and planning in this runtime'},409)
            if path=='/api/v1/face':return self.send_json(face_generator.configure(data))
            if path=='/api/v1/face-action':return self.send_json(face_generator.set_action(data))
            if path=='/api/v1/face-test':
                if not face_generator.enabled:raise ValueError('请先启用动态表情')
                converted=describe(data.get('text'),data.get('emotion','neutral'),completion,load_config(),data.get('state'))
                result=face_generator.request(converted['text'],converted['emotion'],data.get('speaking_s',0),data.get('seed'),converted['state'])
                return self.send_json({**result,'description':converted['text'],'translated':converted['translated'],'translation_ms':converted['translation_ms']})
            if path=='/api/v1/face-state':return self.send_json(face_generator.set_state(data.get('emotion','neutral'),data.get('state',{})))
            if path=='/api/v1/face-reset':return self.send_json(face_generator.reset())
            if path=='/api/v1/face-speech-stop':
                face_generator.stop_speech()
                return self.send_json({'ok':True})
            if path=='/api/v1/motion-observer':return self.send_json(motion_observer.configure(data))
            if path=='/api/v1/motion-observer-annotation':return self.send_json(motion_observer.annotate(data.get('kind'),data.get('note','')))
            trace.record('control', {'path': path, 'data': data if path!='/api/v1/config' else {'changed_fields':list(data)}})
            if path=='/api/v1/persona':return self.send_json({**save_persona(data),'takes_effect':'next_request'})
            if path=='/api/v1/conversation/new':
                with controller.lock:
                    controller.reset_conversation()
                    face_generator.reset()
                    cid=store.new_conversation();controller.conversation_id=cid
                    vision.set_owner(owner is not None)
                    world.clear_vision()
                    emit({'type':'chat','messages':[]})
                return self.send_json({'conversation_id':cid,'archived_previous':True,'motion_paused':True})
            if path=='/api/v1/vision':
                active_look.cancel('vision_configuration_changed')
                return self.send_json(vision.configure(data))
            if path=='/api/v1/vision-observe':
                if vision.config['active_enabled']:
                    if not vision.config['enabled']:raise ValueError('请先启用视觉')
                    if owner is None:raise ValueError('请先加载角色')
                    controller.submit('请主动看一下当前感兴趣的目标，确认你想知道的信息。')
                else:vision.trigger('manual_observation')
                return self.send_json({'queued':True})
            if path=='/api/v1/perception-event':
                if data.get('type')!='FORCE_OBSERVE':raise ValueError('unsupported_perception_event')
                if not vision.config['enabled']:raise ValueError('请先启用视觉总开关')
                if not vision.config['background_enabled']:raise ValueError('请先启用后台视觉摘要')
                return self.send_json(perception_scheduler.force_observe(str(data.get('reason','manual_force')),'debug_control'))
            if path=='/api/v1/chat':
                if not ai_configured(load_config()):raise ValueError('未配置 AI 接口：请在“动作与配置”中填写 OpenAI 兼容接口地址和模型。')
                if owner is None:raise ValueError('请先打开房间并加载 VRM')
                if not worker.ready:raise ValueError('ARDY 正在准备，请稍候')
                controller.submit(str(data.get('message','')),face_dialogue=data.get('face_dialogue') is True)
                return self.send_json({'accepted':True})
            if path=='/api/v1/body-loop':
                if owner is None:raise ValueError('Open the VRM room first')
                with controller.lock:
                    controller.body_loop.configure(data.get('enabled'))
                    if data.get('enabled'):worker.playback('play')
                return self.send_json(controller.body_loop.snapshot())
            if path=='/api/v1/stop':controller.cancel();return self.send_json({'cancelled':True})
            if path=='/api/v1/autonomy':
                if data.get('enabled') and not ai_configured(load_config()):raise ValueError('未配置 AI 接口：自动规划需要 OpenAI 兼容接口。')
                if owner is None:raise ValueError('Open the desktop pet first')
                return self.send_json(controller.configure_autonomy(data.get('enabled')))
            if path=='/api/v1/gaze':
                mode=data.get('mode')
                if mode not in ('user','none'):raise ValueError('mode must be user or none')
                worker.expression(worker.face,mode,scope='manual')
                return self.send_json({'mode':mode})
            if path=='/api/v1/interaction':
                worker.note_interaction();return self.send_json({'accepted':True})
            if path=='/api/v1/motion-reset':
                if owner is None:raise ValueError('Open the desktop pet first')
                controller.cancel();worker.reset_motion()
                with controller.lock:
                    controller.body_constraints=[]
                    controller.install_plan([Step(action='reset-standing',label='复原站立',prompt='A person is standing.',duration_s=2,after_idle='stand')],'复原角色',source='manual')
                    controller.phase='已清除旧运动，正在复原站立姿态'
                return self.send_json({'accepted':True})
            if path=='/api/v1/recenter':
                if owner is None:raise ValueError('Open the VRM room first')
                with controller.lock:controller.body_loop.previous=None
                emit({'type':'root_recenter'})
                return self.send_json({'accepted':True})
            if path in ('/api/v1/motion-config','/api/v1/playback','/api/v1/motion-test'):
                if owner is None:raise ValueError('Open the VRM room first')
                if path=='/api/v1/motion-config':
                    from settings import validate
                    new=validate(data,worker.params)
                    if new['model']!=worker.params['model']:controller.cancel()
                    return self.send_json(worker.configure(data))
                if path=='/api/v1/playback':
                    if data.get('action')=='seek':controller.cancel()
                    if data.get('action')=='pause':
                        with controller.lock:controller.body_loop.invalidate()
                    if data.get('action')=='play':worker.note_interaction()
                    worker.playback(data.get('action'),data.get('frame'))
                    return self.send_json({'ok':True})
                if 'steps' in data:
                    if not isinstance(data['steps'],list) or not 1<=len(data['steps'])<=15:
                        raise ValueError('Motion test needs 1 to 15 steps')
                    steps=[Step.model_validate(item) for item in data['steps']]
                else:
                    step=Step(action=data.get('action','manual-test'),prompt=data.get('prompt'),
                              duration_s=float(data.get('duration_s',4)),repeat=data.get('repeat',1),
                              pose_intent=data.get('pose_intent','auto'),after_idle=data.get('after_idle','keep'),look_at=data.get('look_at','auto'),continuous=data.get('continuous',False),kind=data.get('kind','one_shot'),label=data.get('label','手动动作'))
                    steps=[step]
                if any(item.action.startswith('idle_') for item in steps) and not worker.params.get('allow_cached_idle_loops',False):
                    raise ValueError('请先开启“允许AI选择稳定待机循环”')
                controller.cancel(preserve_motion=True)
                worker.note_interaction()
                with controller.lock:
                    controller.auto_rounds=4
                    controller.request_started_at=time.monotonic();controller.request_first_played=False
                    worker.record(stage='manual_test',llm_plan_ms=None,llm_http_ms=None,request_to_first_motion_ms=None,
                                  first_motion_encode_ms=None,first_motion_cache_hit=None,validation_error=None,llm_attempt=None,
                                  llm_http_status=None,context_chars=None,llm_usage={},error=None)
                    controller.install_plan(steps,'手动动作计划',source='manual');controller.phase='准备手动动作测试'
                return self.send_json({'accepted':True})
            if path=='/api/v1/config':
                with controller.lock:controller.body_loop.invalidate()
                cfg=load_config()
                for field in ('base_url','model','auto_continue','json_mode','thinking_enabled','body_interval_s','idle_interval_s'):
                    if field in data:cfg[field]=data[field]
                if data.get('api_key'):cfg['api_key']=data['api_key']
                for field in ('body_interval_s','idle_interval_s'):
                    if field in data and (type(data[field]) not in (int,float) or not 6<=data[field]<=300):raise ValueError(field+' must be 6–300 seconds')
                if 'thinking_enabled' in data and type(data['thinking_enabled']) is not bool:raise ValueError('thinking_enabled must be a boolean')
                save_config(cfg);active_look.cancel('provider_configuration_changed');vision.set_owner(owner is not None);return self.send_json({'saved':True})
            return self.send_json({'error':'not_found'},404)
        except ValueError as exc:self.send_json({'error':str(exc)},400)
        except Exception:self.send_json({'error':'内部错误，请查看服务日志'},500)
    def log_message(self,*args):pass

def websocket(connection):
    global owner,capabilities
    query=parse_qs(urlparse(connection.request.path).query)
    origin=connection.request.headers.get('Origin')
    if query.get('token',[''])[0]!=TOKEN or (origin and origin!=f'http://127.0.0.1:{PORT}'):
        connection.close(1008,'unauthorized');return
    with client_lock:
        clients.add(connection)
        if query.get('takeover',[''])[0] in ('pet','face-preview') and owner is not connection:
            previous=owner
            owner=connection;worker.owner=True
            if previous is not None:
                try:previous.send(json.dumps({'type':'runtime_role','role':'observer'}))
                except Exception:pass
        if owner is None and query.get('observer',[''])[0]!='1':owner=connection;worker.owner=True;role='runtime'
        else:role='runtime' if owner is connection else 'observer'
    try:
        connection.send(json.dumps({'type':'welcome','role':role,'state':worker.state(),'messages':store.recent()}))
        for packet in face_generator.replay():connection.send(json.dumps(packet))
        if worker.ready:
            connection.send(json.dumps({'type':'backend_ready','skeleton':worker.metadata()}))
            packet=worker.snapshot_motion()
            if packet:connection.send(json.dumps(packet))
        for message in connection:
            if len(message)>300000:continue
            data=json.loads(message)
            if connection is owner:
                if data.get('type')=='look_result':
                    try:active_look.receive(data)
                    except (ValueError,TypeError):pass
                elif data.get('type')=='vision_frame':
                    try:vision.offer(data)
                    except (ValueError,TypeError):pass
                elif data.get('type')=='feedback':
                    perception=data.get('body_perception')
                    if isinstance(perception,dict):
                        alife.rendered_face={k:v for k,v in perception.items() if k in ('emotion','gaze','speaking','mouth_open','eyes_closed') and (isinstance(v,(str,bool)) or isinstance(v,(int,float)) and math.isfinite(v))}
                        alife.rendered_at=time.monotonic()
                    if worker.feedback(data):
                        world.observe(data)
                        motion_observer.offer(data)
                        trace.record('runtime_feedback', {k:v for k,v in data.items() if k!='motion_samples'})
                elif data.get('type')=='capabilities':
                    motion_observer.reset('runtime_model_changed')
                    active_look.cancel('runtime_model_changed')
                    vision.set_owner(True)
                    trace.record('runtime_capabilities', data)
                    capabilities=data.get('payload',{});world.capabilities=capabilities
                    for face_packet in face_generator.replay():connection.send(json.dumps(face_packet))
                    if worker.ready:
                        packet=worker.snapshot_motion()
                        if packet:connection.send(json.dumps(packet))
                        connection.send(json.dumps({'type':'play','playing':worker.playing,'epoch':worker.epoch}))
    except Exception:pass
    finally:
        with client_lock:
            clients.discard(connection)
            was_owner=owner is connection
            if was_owner:owner=None;worker.owner=False;capabilities={};world.capabilities={}
        # Cancelling now emits face_speech; do not emit while holding client_lock.
        if was_owner and owner is None:
            vision.set_owner(False);motion_observer.reset('runtime_disconnected');controller.cancel()

def main():
    with serve(websocket,'127.0.0.1',WS_PORT,max_size=300000) as ws:
        threading.Thread(target=ws.serve_forever,daemon=True).start()
        print(f'Character Agent: http://127.0.0.1:{PORT}/',flush=True)
        try:ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
        finally:face_generator.close();motion_observer.close();vision.close();controller.close();worker.close();trace.close()

if __name__=='__main__':main()

