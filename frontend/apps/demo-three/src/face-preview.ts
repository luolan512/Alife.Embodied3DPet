import {Room} from '../../../adapters/runtime-three/room';
import type {SkeletonMetadata} from '../../../packages/contracts/motion';
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
const room=new Room($('viewport'));room.previewIdle=true;
let token='',wsPort=0,socket:WebSocket,role='observer',loaded=false,ready=false,pendingReply=false,generating=false;
let last=performance.now(),feedbackAt=0,debugAt=0,firstLatency:number|undefined,totalMs:number|undefined;
let meta:SkeletonMetadata|undefined,playState={playing:false,epoch:0},serverState:any={},capabilities:any;
async function api(path:string,body?:unknown){const response=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data;}
function error(e:unknown){$('error').textContent=String(e instanceof Error?e.message:e);}
function buttons(){const online=socket?.readyState===WebSocket.OPEN;$<HTMLButtonElement>('send').disabled=!(online&&loaded&&ready&&!pendingReply);$<HTMLButtonElement>('generate').disabled=!(online&&loaded&&!generating);$('connection').textContent=online?`${role==='runtime'?'本页控制动作':'观察动作（其他页面控制）'} · ARDY ${ready?'就绪':'准备中'}`:'连接中…';}
function sendCapabilities(){if(role==='runtime'&&capabilities&&socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'capabilities',payload:{...capabilities,layers:['ardy-body','face-state','cteg-motion','eyes-blink','spring-bone'],object_interaction:false}}));}
async function load(){loaded=false;buttons();const select=$<HTMLSelectElement>('model');capabilities=await room.load('/models/'+select.value+'.vrm',select.selectedOptions[0].textContent!);if(meta)room.motion?.setSkeleton(meta);room.motion?.play(playState.playing,playState.epoch);room.focusFacePreview();if(room.generatedFace){room.generatedFace.speed=Number($<HTMLInputElement>('speed').value);room.generatedFace.speechRate=Number($<HTMLInputElement>('speech-rate').value);}loaded=true;sendCapabilities();buttons();}
function chat(messages:{role:string;content:string}[]){$('messages').replaceChildren();for(const message of messages){const row=document.createElement('div');row.className='message '+message.role;const label=document.createElement('span');label.className='label';label.textContent=message.role==='user'?'你':'角色';row.append(label,document.createTextNode(message.content));$('messages').append(row);}$('messages').scrollTop=$('messages').scrollHeight;}
function syncAffect(data:any){for(const key of ['valence','arousal','dominance','intensity']){const value=Number(data[key]);$<HTMLInputElement>(key).value=String(value);$(key+'-value').textContent=value.toFixed(2);}$<HTMLInputElement>('hold-s').value=String(data.hold_s);$<HTMLInputElement>('decay-s').value=String(data.decay_s);}
function connect(takeover=false){const previous=socket;socket=new WebSocket(`ws://127.0.0.1:${wsPort}/?token=${encodeURIComponent(token)}${takeover?'&takeover=face-preview':''}`);if(previous){previous.onclose=null;previous.close();}socket.onopen=buttons;socket.onclose=()=>{pendingReply=false;buttons();error('连接已关闭，刷新可重新连接。');};socket.onmessage=e=>{
 const data=JSON.parse(e.data);
 if(data.type==='welcome'){role=data.role;ready=data.state.backend_ready;playState={playing:data.state.playing,epoch:data.state.epoch};chat(data.messages);sendCapabilities();buttons();}
 else if(data.type==='runtime_role'){role=data.role;buttons();}
 else if(data.type==='backend_ready'){ready=true;meta=data.skeleton;if(data.reset)room.resetMotion(meta!);else room.motion?.setSkeleton(meta!);buttons();}
 else if(data.type==='backend_loading'){ready=false;buttons();}
 else if(data.type==='motion')room.motion?.accept(data);
 else if(data.type==='motion_trim')room.motion?.trimAfter(data.frame,data.epoch);
 else if(data.type==='motion_reset'){meta=data.skeleton;room.resetMotion(meta!);playState={playing:false,epoch:data.epoch};if(room.motion)room.motion.epoch=data.epoch;}
 else if(data.type==='play'){playState={playing:data.playing,epoch:data.epoch};room.motion?.play(data.playing,data.epoch);}
 else if(data.type==='seek')room.motion?.seek(data.frame);
 else if(data.type==='root_recenter')room.motion?.recenter();
 else if(data.type==='expression')room.setExpression(data.emotion,data.look_at,data.look_target,data.gaze_scope);
 else if(data.type==='chat')chat(data.messages);
 else if(data.type==='state'){serverState=data.state;pendingReply=!!data.state.pending_request;buttons();}
 else if(data.type?.startsWith('face_')){
  room.facePacket(data);
  if(data.type==='face_config'){for(const k of ['enabled','continuous','guidance'])$<HTMLInputElement>(k).checked=data[k]??true;$<HTMLInputElement>('gain').value=String(data.gain);$('gain-value').textContent=data.gain.toFixed(2);}
  if(data.type==='face_state')syncAffect(data);
  if(data.type==='face_pending'){firstLatency=undefined;totalMs=undefined;}
  if(data.type==='face_batch'&&data.first)firstLatency=data.latency_ms;
  if(data.type==='face_done')totalMs=data.total_ms;
  if(data.type==='face_error')error(data.error);
 }
};}
$('chat-form').onsubmit=e=>{e.preventDefault();const message=$<HTMLTextAreaElement>('chat-input').value.trim();if(!message||pendingReply||!ready)return;$('error').textContent='';pendingReply=true;buttons();void api('chat',{message,face_dialogue:true}).then(()=>$<HTMLTextAreaElement>('chat-input').value='').catch(e=>{pendingReply=false;buttons();error(e);});};
$<HTMLTextAreaElement>('chat-input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$<HTMLFormElement>('chat-form').requestSubmit();}};
$('stop').onclick=()=>void Promise.all([api('stop',{}),api('face-reset',{})]).catch(error);
$('takeover').onclick=()=>connect(true);
$('face-view').onclick=()=>room.focusFacePreview();$('full-view').onclick=()=>room.cameraPreset('full');
$<HTMLSelectElement>('model').onchange=()=>void load().catch(error);
const examples:Record<string,string>={happy:'太好了，见到你真的很开心！',angry:'这太过分了，我真的很生气。',sad:'有点失落，本来很期待的。',surprised:'真的吗？我完全没想到！'};
$<HTMLSelectElement>('examples').onchange=()=>{const choice=$<HTMLSelectElement>('examples').value;if(examples[choice]){$<HTMLTextAreaElement>('text').value=examples[choice];$<HTMLSelectElement>('emotion').value=choice==='surprised'?'neutral':choice;}};
$('generate').onclick=async()=>{generating=true;buttons();$('error').textContent='';$('translation').textContent='准备表演描述…';try{const result=await api('face-test',{text:$<HTMLTextAreaElement>('text').value,emotion:$<HTMLSelectElement>('emotion').value,speaking_s:$<HTMLInputElement>('speaking').checked?8:0});$('translation').textContent=`内部描述：${result.description}${result.translated?` · 中文转换 ${Math.round(result.translation_ms)}ms`:''}`;}catch(e){error(e);}finally{generating=false;buttons();}};
$('reset').onclick=()=>void api('face-reset',{}).catch(error);
for(const key of ['enabled','continuous','guidance'])$<HTMLInputElement>(key).onchange=()=>void api('face',{[key]:$<HTMLInputElement>(key).checked}).catch(error);
$<HTMLInputElement>('gain').oninput=()=>{const gain=Number($<HTMLInputElement>('gain').value);$('gain-value').textContent=gain.toFixed(2);if(room.generatedFace)room.generatedFace.gain=gain;};
$<HTMLInputElement>('gain').onchange=()=>void api('face',{gain:Number($<HTMLInputElement>('gain').value)}).catch(error);
for(const key of ['speed','speech-rate'])$<HTMLInputElement>(key).oninput=()=>{const value=Number($<HTMLInputElement>(key).value);$(key+'-value').textContent=value.toFixed(1)+(key==='speed'?'×':'次/秒');if(room.generatedFace){if(key==='speed')room.generatedFace.speed=value;else room.generatedFace.speechRate=value;}};
for(const key of ['valence','arousal','dominance','intensity'])$<HTMLInputElement>(key).oninput=()=>$(key+'-value').textContent=Number($<HTMLInputElement>(key).value).toFixed(2);
$('apply-state').onclick=()=>{const state=Object.fromEntries(['valence','arousal','dominance','intensity'].map(k=>[k,Number($<HTMLInputElement>(k).value)]));state.hold_s=Number($<HTMLInputElement>('hold-s').value);state.decay_s=Number($<HTMLInputElement>('decay-s').value);void api('face-state',{emotion:$<HTMLSelectElement>('emotion').value,state}).catch(error);};
$<HTMLInputElement>('mix-ratio').oninput=()=>$('mix-ratio-value').textContent=Math.round(Number($<HTMLInputElement>('mix-ratio').value)*100)+'%';
$('apply-mixture').onclick=()=>{const first=$<HTMLSelectElement>('mix-first').value,second=$<HTMLSelectElement>('mix-second').value,ratio=Number($<HTMLInputElement>('mix-ratio').value);const mixture:Record<string,number>={[first]:ratio};mixture[second]=(mixture[second]??0)+1-ratio;void api('face-state',{emotion:first,state:{mixture,intensity:Number($<HTMLInputElement>('intensity').value),hold_s:Number($<HTMLInputElement>('hold-s').value),decay_s:Number($<HTMLInputElement>('decay-s').value)}}).catch(error);};
$('save-config').onclick=()=>void api('config',{base_url:$<HTMLInputElement>('base-url').value.trim(),model:$<HTMLInputElement>('ai-model').value.trim(),api_key:$<HTMLInputElement>('api-key').value}).then(()=>{$<HTMLInputElement>('api-key').value='';$('error').textContent='接口已保存，下一条消息生效。';}).catch(error);
function render(now:number){const dt=(now-last)/1000;last=now;room.update(dt);if(now-debugAt>150){debugAt=now;const face=room.generatedFace?.snapshot();$('debug').textContent=JSON.stringify({face,role,body:room.motion?.feedback(),phase:serverState.phase,events:serverState.debug_events?.slice(-5)},null,2);if(face){const current=face.affect.current;$('affect-status').textContent=`愉悦 ${current.valence.toFixed(2)} · 激动 ${current.arousal.toFixed(2)} · 强势 ${current.dominance.toFixed(2)} · 强度 ${current.intensity.toFixed(2)} · 已播放 ${face.windows} 段`;const modes:Record<string,string>={waiting:'等待下一次交流',preparing:'续段准备中，保留当前情绪',playing:'表情播放中',hold:'保持情绪，等待续段',releasing:'渐回中性'};$('status').textContent=(pendingReply?'AI 正在回复…':modes[face.mode])+(face.speaking?' · 模拟说话':'')+(firstLatency===undefined?'':` · 首批 ${Math.round(firstLatency)}ms`)+(totalMs===undefined?'':` / 整段 ${Math.round(totalMs)}ms`)+` · ${serverState.phase??''}`;}}
 if(now-feedbackAt>250&&loaded&&role==='runtime'&&socket?.readyState===WebSocket.OPEN){feedbackAt=now;const feedback=room.motion?.feedback();if(feedback){const diagnostics=room.diagnostics();socket.send(JSON.stringify({type:'feedback',...feedback,joints:diagnostics.positions,spatial:diagnostics.spatial,motion_samples:room.motion?.drainObservations()}));}}
 requestAnimationFrame(render);}
requestAnimationFrame(render);
async function init(){const session=await fetch('/api/v1/session').then(r=>r.json());token=session.token;wsPort=session.ws_port;const [models,cfg]=await Promise.all([api('models'),api('config')]);for(const model of models){const option=document.createElement('option');option.value=model.id;option.textContent=model.name;$('model').append(option);}$<HTMLSelectElement>('model').value=models.some((m:any)=>m.id==='alice')?'alice':models[0].id;$<HTMLInputElement>('base-url').value=cfg.base_url??'';$<HTMLInputElement>('ai-model').value=cfg.model??'';await load();connect();}
void init().catch(error);
