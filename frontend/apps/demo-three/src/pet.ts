import './pet.css';
import faceControls from './face-controls.html?raw';
import headControls from './head-controls.html?raw';
import {Room} from '../../../adapters/runtime-three/room';
import type {MotionPacket,SkeletonMetadata} from '../../../packages/contracts/motion';
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
$('generation-controls').insertAdjacentHTML('beforebegin',faceControls);
$('generation-controls').removeAttribute('open');
$('panel').insertAdjacentHTML('beforebegin',headControls);
const room=new Room($('pet-viewport'),true);
room.previewIdle=true;
const savedBrightness=Math.max(60,Math.min(260,Number(localStorage.getItem('pet-brightness')||180)));
$<HTMLInputElement>('brightness').value=String(savedBrightness);room.setDesktopBrightness(savedBrightness/100);
$<HTMLInputElement>('heading').checked=localStorage.getItem('pet-heading')!=='free';
const savedScale=Math.max(35,Math.min(800,Number(new URLSearchParams(location.search).get('scale')||localStorage.getItem('pet-scale')||100)));
$<HTMLInputElement>('scale').value=String(savedScale);room.camera.zoom=(window as any).chrome?.webview?1:savedScale/100;room.camera.updateProjectionMatrix();
const host=(type:string,data:Record<string,unknown>={})=>{(window as any).chrome?.webview?.postMessage({type,...data});};
let autonomous=false;
let gazeMode=localStorage.getItem('pet-gaze')||'user';
let token='',socket:WebSocket,role='observer',ready=false,loaded=false;
let meta:SkeletonMetadata|undefined,pending:MotionPacket[]=[],playState={playing:false,epoch:0};
let generationDebug:any={};
function generationTiming(d:any){generationDebug=d;const seconds=(v:any)=>typeof v==='number'?`${(v/1000).toFixed(3)}秒`:'尚无记录';$('generation-timing').textContent=`最近一次动作推理：${seconds(d.motion_inference_ms)}；含后处理：${seconds(d.motion_generation_ms)}；文本准备：${seconds(d.kimodo_encode_ms??d.first_motion_encode_ms)}（排队 ${seconds(d.text_queue_wait_ms)} / 实际编码 ${seconds(d.text_encode_compute_ms)}）；本段准备总耗时：${seconds(d.motion_prepare_ms)}；请求到首段播放：${seconds(d.request_to_first_motion_ms)}。实际历史 ${d.motion_history_frames??'—'} 帧 / 去噪 ${d.motion_denoising_steps??'—'} 步 / 生成 ${d.motion_generated_frames??'—'} 帧；实际后端 ${d.motion_inference_backend??d.inference_backend_active??'—'}。`; }
let faceConfig:any={};
let pendingFace:{packet:any;received:number}[]=[];
let cfg:any={},lastFeedback=0,last=performance.now(),currentModel='';
let textEncoderRestartPending=false;
function textEncoderRuntime(runtime:any){
  if(!runtime||textEncoderRestartPending)return;
  const backend=runtime.active_backend==='vulkan'?`Vulkan · ${runtime.layer_chunk===32?'整模型常驻':runtime.layer_chunk+' 层分块'}`:runtime.active_backend==='cpu'?'CPU':'等待首次编码';
  $('text-encoder-status').textContent=`当前：${backend}${runtime.fallback_reason?' · '+runtime.fallback_reason:''}。保存设置后重启桌宠生效。`;
}
let targetZoom=room.camera.zoom,lastMask=0,lastInteraction=0;
let bigHead=false,nativeAppliedPercent=innerHeight/780*100,nativeAnchorHeight=innerHeight,nativeViewportHeight=innerHeight,maskDirty=true;
let lastHeadAnchor=0;
const native=!!(window as any).chrome?.webview;
let modelPercent=savedScale;
function setModelScale(percent:number,notify=true){
  if(bigHead||!Number.isFinite(percent))return;
  modelPercent=Math.max(35,Math.min(800,percent));
  $<HTMLInputElement>('scale').value=String(Math.round(modelPercent));localStorage.setItem('pet-scale',String(modelPercent));
  targetZoom=native?modelPercent/nativeAppliedPercent*nativeAnchorHeight/innerHeight:modelPercent/100;
  maskDirty=true;if(native&&notify)host('modelScale',{percent:modelPercent});
}
function setBigHead(enabled:boolean,notify=true){
  bigHead=enabled;document.body.classList.toggle('big-head',enabled);$('head-bar').hidden=!enabled;
  $('big-head').textContent=enabled?'还原':'全屏';closeMenu();
  targetZoom=enabled?1:native?modelPercent*7.8/innerHeight:modelPercent/100;room.camera.zoom=targetZoom;room.camera.updateProjectionMatrix();
  if(loaded){if(enabled)room.focusFacePreview(.72);else room.cameraPreset('full');}
  maskDirty=true;publishRegions();if(native&&notify)host('bigHead',{enabled});
}
(window as any).chrome?.webview?.addEventListener('message',(event:any)=>{
  const data=event.data;
  if(data.type==='alifeResult')void api('alife/runtime-result',data).catch(()=>{});
  if(data.type==='alifeInteraction')void api('alife/interaction',{text:data.text}).catch(()=>{});
  if(data.type==='scalePreview')setModelScale(data.percent,false);
  if(data.type==='scaleApplied'){nativeAppliedPercent=data.windowPercent;nativeAnchorHeight=data.height??innerHeight;targetZoom=modelPercent/nativeAppliedPercent*nativeAnchorHeight/innerHeight;room.camera.updateProjectionMatrix();maskDirty=true;}
  if(data.type==='bigHead')setBigHead(data.enabled,false);
});
window.addEventListener('resize',()=>{if(native&&!bigHead){room.camera.zoom*=nativeViewportHeight/innerHeight;targetZoom=modelPercent*7.8/innerHeight;room.camera.updateProjectionMatrix();}nativeViewportHeight=innerHeight;maskDirty=true;publishRegions();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&bigHead){e.preventDefault();setBigHead(false);}});

function interact(){if(token&&performance.now()-lastInteraction>1000){lastInteraction=performance.now();void api('interaction',{}).catch(()=>{});}}
function status(text:string){$('status').textContent=text;}
function notice(text:string){$('notice').textContent=text;$('notice').hidden=!text;}
function updateEnabled(){$<HTMLButtonElement>('generate').disabled=!(ready&&loaded&&role==='runtime');}
async function api(path:string,body?:unknown){
  const response=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data;
}
async function load(url:string,name:string){
  const previousFace=room.generatedFace?.snapshot();
  if(previousFace){
    const received=performance.now();
    pendingFace.push({packet:{type:'face_config',...faceConfig},received});
    pendingFace.push({packet:{type:'face_state',...previousFace.affect.target,emotion:room.generatedFace?.emotion,age_s:previousFace.affect.age_s,version:previousFace.affect.version,mixture:previousFace.affect.mixture},received});
    pendingFace.push({packet:{type:'face_restore',id:previousFace.id,frame:previousFace.current,emotion:room.generatedFace?.emotion,gain:room.generatedFace?.gain},received});
    pendingFace.push({packet:{type:'face_speech',id:previousFace.id,duration_s:previousFace.speechRemaining},received});
  }
  loaded=false;updateEnabled();notice('正在加载角色…');
  const cap=await room.load(url,name);if(!cap)return;
  currentModel=name;loaded=true;maskDirty=true;if(role==='runtime'){gazeMode=localStorage.getItem('pet-gaze')||'user';void api('gaze',{mode:gazeMode}).catch(()=>{});}if(bigHead)room.focusFacePreview(.72);room.generatedFace!.speed=Number($<HTMLInputElement>('face-speed').value);room.generatedFace!.speechRate=Number($<HTMLInputElement>('face-speech-rate').value);
  if(room.eyeMicro)room.eyeMicro.enabled=$<HTMLInputElement>('eye-micro').checked;
  for(const {packet,received} of pendingFace){const age=(performance.now()-received)/1000;room.facePacket(packet.type==='face_speech'?{...packet,duration_s:Math.max(0,packet.duration_s-age)}:packet.type==='face_action'?{...packet,hold_s:Math.max(0,packet.hold_s-age)}:packet.type==='face_state'?{...packet,age_s:(packet.age_s??0)+age}:packet);}pendingFace=[];
  if(meta)room.motion?.setSkeleton(meta);
  if(room.motion){room.motion.lockHeading=$<HTMLInputElement>('heading').checked;room.motion.avoidBodyPenetration=$<HTMLInputElement>('body-avoidance').checked;room.motion.oneEuroEnabled=$<HTMLInputElement>('one-euro').checked;}
  for(const packet of pending)room.motion?.accept(packet);pending=[];
  room.motion?.play(playState.playing,playState.epoch);
  socket.send(JSON.stringify({type:'capabilities',payload:{model_name:name,vrm_version:cap.vrm_version,bones:cap.bones,expressions:cap.expressions,layers:['ardy-body','desktop-root-lock','eyes-blink','face-expression','continuous-affect','cteg-face','text-speech','reply-followup','spring-bone'],object_interaction:false}}));
  notice(ready?'':'ARDY 正在加载…');status('手动输入动作即可开始');updateEnabled();
}
function syncMode(){
  $<HTMLInputElement>('inactive-minutes').disabled=$<HTMLSelectElement>('behavior-mode').value!=='hybrid';
  const select=$<HTMLSelectElement>('preset'),enabled=$<HTMLInputElement>('cached-idle-loops').checked;
  for(const option of select.options)if(option.value.startsWith('idle_'))option.disabled=!enabled;
  if(!enabled&&select.value.startsWith('idle_'))select.value='stand';
}
async function save(){cfg=await api('motion-config',{behavior_mode:$<HTMLSelectElement>('behavior-mode').value,inactive_minutes:Number($<HTMLInputElement>('inactive-minutes').value),settle_buffer_s:Number($<HTMLInputElement>('settle-buffer').value),static_protection:$<HTMLSelectElement>('static-protection').value,allow_cached_idle_loops:$<HTMLInputElement>('cached-idle-loops').checked});syncMode();}
function eventOptions(){return {pose_intent:$<HTMLSelectElement>('pose-intent').value,repeat:Number($<HTMLInputElement>('event-repeat').value),after_idle:$<HTMLSelectElement>('after-idle').value,continuous:$<HTMLInputElement>('continuous').checked};}
function publishRegions(){
 const rect=(el:HTMLElement)=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};};
 const header=$('panel-drag').getBoundingClientRect(),buttons=$('panel-drag').querySelector('div')!.getBoundingClientRect();
 host('uiRegions',{menuOpen:!$('toolbar').hidden,regions:['toolbar','panel','head-bar'].filter(id=>!$(id).hidden).map(id=>rect($(id))),dragRegions:!bigHead&&!$('panel').hidden?[{x:header.x,y:header.y,width:Math.max(0,buttons.left-header.left),height:header.height}]:[]});
}
function openMenuAt(x:number,y:number){
 const ring=$('toolbar');ring.hidden=!ring.hidden;
 if(!ring.hidden){const radius=Math.min(112,innerWidth/2-4,innerHeight/2-4);ring.style.left=Math.max(radius,Math.min(innerWidth-radius,x))+'px';ring.style.top=Math.max(radius,Math.min(innerHeight-radius,y))+'px';ring.style.transform=`translate(-50%,-50%) scale(${Math.min(1,(innerWidth-8)/216,(innerHeight-8)/216)})`;}
 publishRegions();
}
function layoutRing(){const buttons=Array.from($('toolbar').querySelectorAll<HTMLButtonElement>('button')).filter(b=>!b.hidden);buttons.forEach((button,i)=>{const angle=-Math.PI/2+i*Math.PI*2/buttons.length;button.style.transform=`translate(${Math.cos(angle)*78}px,${Math.sin(angle)*78}px)`;});}


function closeMenu(){$('toolbar').hidden=true;publishRegions();}
function togglePanel(){void syncSharedSettings().catch(e=>status(String(e))); const panel=$('panel');panel.hidden=!panel.hidden;if(!panel.hidden)closeMenu();publishRegions();}
document.documentElement.addEventListener('mouseleave',e=>{if(e.clientX<0||e.clientY<0||e.clientX>=innerWidth||e.clientY>=innerHeight)closeMenu();});
$('settings').onclick=togglePanel;
$('panel-close').onclick=()=>{$('panel').hidden=true;publishRegions();};
$('panel-refresh').onclick=()=>location.reload();
async function loadAIConfig(){
 const provider=await api('config');
 $<HTMLInputElement>('ai-base-url').value=provider.base_url||'';
 $<HTMLInputElement>('ai-model').value=provider.model||'';
 $('ai-status').textContent=provider.configured?`已配置：${provider.model} · ${provider.has_api_key?'已保存密钥':'未使用密钥'}`:'未配置 AI 接口';
}
$('ai-save').onclick=async()=>{try{
 const base=$<HTMLInputElement>('ai-base-url').value.trim(),model=$<HTMLInputElement>('ai-model').value.trim();
 if(!/^https?:\/\//.test(base)||!model)throw Error('请填写 http:// 或 https:// 接口地址和模型 ID。');
 await api('config',{base_url:base,model,api_key:$<HTMLInputElement>('ai-key').value});
 $<HTMLInputElement>('ai-key').value='';await loadAIConfig();
}catch(e){$('ai-status').textContent=String(e);}};
$('ai-disable').onclick=async()=>{try{await api('autonomy',{enabled:false});await api('config',{base_url:'',model:'',auto_continue:false});await loadAIConfig();}catch(e){$('ai-status').textContent=String(e);}};
const panelHeader=$('panel-drag');
let panelDrag:{id:number;x:number;y:number;left:number;top:number}|undefined;
panelHeader.onpointerdown=e=>{
  if(e.button!==0||(e.target as HTMLElement).closest('button'))return;
  e.preventDefault();interact();
  if((window as any).chrome?.webview){host('drag');return;}
  const rect=$('panel').getBoundingClientRect();
  panelDrag={id:e.pointerId,x:e.clientX,y:e.clientY,left:rect.left,top:rect.top};
  $('panel').style.width=rect.width+'px';$('panel').style.right='auto';
  panelHeader.setPointerCapture(e.pointerId);
};
panelHeader.onpointermove=e=>{
  if(!panelDrag||panelDrag.id!==e.pointerId)return;
  const panel=$('panel'),width=panel.getBoundingClientRect().width;
  panel.style.left=Math.max(0,Math.min(innerWidth-width,panelDrag.left+e.clientX-panelDrag.x))+'px';
  panel.style.top=Math.max(0,Math.min(innerHeight-52,panelDrag.top+e.clientY-panelDrag.y))+'px';
  panel.style.maxHeight=Math.max(52,innerHeight-parseFloat(panel.style.top)-8)+'px';publishRegions();
};
panelHeader.onpointerup=panelHeader.onpointercancel=()=>{panelDrag=undefined;publishRegions();};
document.oncontextmenu=e=>{e.preventDefault();if(!bigHead&&!room.hitsAvatar(e.clientX,e.clientY)&&$('toolbar').hidden)return;interact();openMenuAt(e.clientX,e.clientY);};
$('pet-viewport').onpointerdown=e=>{if(!native&&!bigHead&&e.button===0&&room.hitsAvatar(e.clientX,e.clientY)){e.preventDefault();interact();}};
$('pet-viewport').addEventListener('wheel',e=>{
  if(bigHead)return;if(!room.hitsAvatar(e.clientX,e.clientY))return;e.preventDefault();
  const factor=e.deltaY<0?1.06:1/1.06;
  interact();setModelScale(modelPercent*factor);
},{passive:false});
$('chat').onclick=()=>{if((window as any).chrome?.webview)host('chat');else window.open('/chat.html','pet-chat','width=420,height=600');};
$('big-head').onclick=()=>setBigHead(!bigHead);
$('head-back').onclick=()=>setBigHead(false);
$('head-chat').onclick=()=>{$('chat').click();};
$('head-settings').onclick=togglePanel;
$('auto').onclick=()=>void api('autonomy',{enabled:!autonomous}).catch(e=>status(String(e)));
$('reset').onclick=()=>void api('motion-reset',{}).catch(e=>status(String(e)));
$('close').onclick=()=>host('close');
$('generate').onclick=async()=>{
  try{await save();await api('motion-test',{prompt:$<HTMLTextAreaElement>('prompt').value,duration_s:Number($<HTMLInputElement>('action-duration').value),...eventOptions()});status('正在生成新动作…');}catch(e){status(String(e));}
};
$('preset-play').onclick=async()=>{try{await save();const action=$<HTMLSelectElement>('preset').value;const options=eventOptions();await api('motion-test',{action,duration_s:Number($<HTMLInputElement>('action-duration').value),...options,after_idle:options.after_idle==='keep'&&['stand','sit','lie'].includes(action)?action:options.after_idle,look_at:action==='look_user'?'user':'auto'});}catch(e){status(String(e));}};
$('apply-generation').onclick=async()=>{try{cfg=await api('motion-config',{history_frames:Number($<HTMLInputElement>('history-frames').value),denoising_steps:Number($<HTMLInputElement>('denoising-steps').value),inference_backend:$<HTMLSelectElement>('inference-backend').value});$('generation-settings-status').textContent='已保存，下一次生成生效；请用同一动作对比。';}catch(e){$('generation-settings-status').textContent=String(e);}};
$('text-encoder-save').onclick=async()=>{try{const saved=await api('text-encoder',{mode:$<HTMLSelectElement>('text-encoder-mode').value,layer_chunk:Number($<HTMLInputElement>('text-encoder-chunk').value)});textEncoderRestartPending=saved.restart_required;$('text-encoder-status').textContent='已保存。关闭桌宠后重新运行 start.cmd 生效。';}catch(e){$('text-encoder-status').textContent=String(e);}};
$('gaze').onchange=()=>{gazeMode=$<HTMLInputElement>('gaze').checked?'user':'none';localStorage.setItem('pet-gaze',gazeMode);void api('gaze',{mode:gazeMode}).catch(e=>status(String(e)));};
$('pause').onclick=()=>void api('stop',{}).catch(e=>status(String(e)));
$('play').onclick=()=>void api('playback',{action:'play'}).catch(e=>status(String(e)));
$('stop').onclick=()=>void api('stop',{}).catch(e=>status(String(e)));
for(const id of ['behavior-mode','inactive-minutes','settle-buffer','static-protection','cached-idle-loops'])$(id).onchange=()=>{interact();void save().catch(e=>status(String(e)));};
$('heading').onchange=()=>{const locked=$<HTMLInputElement>('heading').checked;localStorage.setItem('pet-heading',locked?'locked':'free');if(room.motion)room.motion.lockHeading=locked;};
$('scale').oninput=()=>setModelScale(Number($<HTMLInputElement>('scale').value));
$<HTMLInputElement>('one-euro').checked=localStorage.getItem('pet-one-euro')!=='off';
$('one-euro').onchange=()=>{const enabled=$<HTMLInputElement>('one-euro').checked;localStorage.setItem('pet-one-euro',enabled?'on':'off');if(room.motion)room.motion.oneEuroEnabled=enabled;};
$<HTMLInputElement>('body-avoidance').checked=localStorage.getItem('pet-body-avoidance')!=='off';
$('body-avoidance').onchange=()=>{const enabled=$<HTMLInputElement>('body-avoidance').checked;localStorage.setItem('pet-body-avoidance',enabled?'on':'off');if(room.motion)room.motion.avoidBodyPenetration=enabled;};
$('brightness').oninput=()=>{const value=$<HTMLInputElement>('brightness').value;localStorage.setItem('pet-brightness',value);room.setDesktopBrightness(Number(value)/100);};
$('model').onchange=()=>{const select=$<HTMLSelectElement>('model');localStorage.setItem('pet-model',select.value);void load('/models/'+select.value+'.vrm',select.selectedOptions[0].text).catch(e=>status(String(e)));};
$('file').onchange=async()=>{const file=$<HTMLInputElement>('file').files?.[0];if(!file)return;const url=URL.createObjectURL(file);try{await load(url,file.name);}catch(e){status(String(e));}finally{URL.revokeObjectURL(url);}};
Object.assign(window,{
  petTogglePanel:togglePanel,
  petOpenMenuAt:openMenuAt,
  petCloseMenu:closeMenu,
  petHitTest:(x:number,y:number)=>{
    for(const id of ['toolbar','panel']){const el=$(id);if(el.hidden)continue;const r=el.getBoundingClientRect();if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom)return true;}
    return room.hitsAvatar(x,y);
  },
  petDiagnostics:()=>({ready,loaded,role,currentModel,menuVisible:!$('toolbar').hidden,modelZoom:room.camera.zoom,modelPercent,bigHead,nativeAppliedPercent,framesRendered:frameCount,maskCaptures,nativeWidth:innerWidth,nativeHeight:innerHeight,transparent:room.renderer.getClearAlpha()===0,desktopMode:room.desktopMode,face:room.generatedFace?.snapshot(),...room.diagnostics()})
});
async function start(){
  // Config is loaded after the local session token is available below.
  setModelScale(savedScale);updateEnabled();const session=await fetch('/api/v1/session').then(r=>r.json());token=session.token;await loadAIConfig();
  const models=await api('models');cfg=await api('motion-config');$<HTMLInputElement>('history-frames').value=String(cfg.history_frames);$<HTMLInputElement>('denoising-steps').value=String(cfg.denoising_steps);
  const textEncoder=await api('text-encoder');$<HTMLSelectElement>('text-encoder-mode').value=textEncoder.settings.mode;$<HTMLInputElement>('text-encoder-chunk').value=String(textEncoder.settings.layer_chunk);
  $('text-encoder-status').textContent=textEncoder.vulkan_available?'保存后重启桌宠生效。':'GPU 编码器未构建；可运行 build.cmd -TextEncoderOnly -TextBackend vulkan。';
  $<HTMLSelectElement>('behavior-mode').value=cfg.behavior_mode||'hybrid';
  $<HTMLInputElement>('inactive-minutes').value=String(cfg.inactive_minutes||30);$<HTMLInputElement>('settle-buffer').value=String(cfg.settle_buffer_s||6);syncMode();
  $<HTMLSelectElement>('static-protection').value=cfg.static_protection||'freeze';
  $<HTMLInputElement>('cached-idle-loops').checked=!!cfg.allow_cached_idle_loops;syncMode();
  $<HTMLSelectElement>('model').replaceChildren(...models.map((m:any)=>{const option=document.createElement('option');option.value=m.id;option.textContent=m.name;return option;}));
  const selected=models.find((m:any)=>m.id===(new URLSearchParams(location.search).get('model')||localStorage.getItem('pet-model')))||models.find((m:any)=>m.id==='alice')||models[0];
  if(selected){$<HTMLSelectElement>('model').value=selected.id;localStorage.setItem('pet-model',selected.id);}
  socket=new WebSocket(`ws://127.0.0.1:${session.ws_port}/?token=${encodeURIComponent(token)}${new URLSearchParams(location.search).get("observer")==="1"?"&observer=1":"&takeover=pet"}`);
  socket.onmessage=event=>{
    const data=JSON.parse(event.data);
    if(data.type==='welcome'){
      generationTiming(data.state.debug||{});
      role=data.role;ready=data.state.backend_ready;playState={playing:data.state.playing,epoch:data.state.epoch};
      if(selected)void load('/models/'+selected.id+'.vrm',selected.name).catch(e=>notice(String(e)));
    }else if(data.type==='backend_ready'){
      ready=true;meta=data.skeleton;if(data.reset){room.resetMotion(meta!);if(bigHead)room.focusFacePreview(.72);playState={playing:false,epoch:0};}else room.motion?.setSkeleton(meta!);
      if(room.motion){room.motion.lockHeading=$<HTMLInputElement>('heading').checked;room.motion.avoidBodyPenetration=$<HTMLInputElement>('body-avoidance').checked;room.motion.oneEuroEnabled=$<HTMLInputElement>('one-euro').checked;}
      notice(loaded?'':'正在加载角色…');updateEnabled();
    }else if(data.type==='backend_loading'){ready=false;notice('正在加载 ARDY…');updateEnabled();}
    else if(data.type==='backend_error')notice(data.error);
    else if(data.type==='runtime_role'){role=data.role;updateEnabled();}
    else if(data.type==='motion_reset'){
      pending=[];meta=data.skeleton;room.resetMotion(meta!);if(bigHead)room.focusFacePreview(.72);maskDirty=true;playState={playing:false,epoch:data.epoch};
      if(room.motion){room.motion.epoch=data.epoch;room.motion.lockHeading=$<HTMLInputElement>('heading').checked;room.motion.avoidBodyPenetration=$<HTMLInputElement>('body-avoidance').checked;room.motion.oneEuroEnabled=$<HTMLInputElement>('one-euro').checked;}
    }
    else if(data.type==='alife_control')host('alifeControl',{...data,type:'alifeControl'});
    else if(data.type==='dialogue_reply'){if(native&&!bigHead&&$<HTMLInputElement>('speech-bubble').checked)host('speechBubble',{text:data.text});}
    else if(data.type.startsWith('face_')){if(loaded)room.facePacket(data);else pendingFace=[...pendingFace,{packet:data,received:performance.now()}].slice(-300);if(data.type==='face_config'){faceConfig=data;for(const key of ['enabled','continuous','guidance'])$<HTMLInputElement>('face-'+key).checked=data[key]??true;$<HTMLInputElement>('face-gain').value=String(data.gain);}if(data.type==='face_batch'&&data.first)$('face-timing').textContent=`表情首批：${Math.round(data.latency_ms??0)} 毫秒`;}
    else if(data.type==='expression'){
      room.setExpression(data.emotion,data.gaze_scope==='manual'?data.look_at:undefined,data.look_target,data.gaze_scope);
      if(data.gaze_scope==='manual'&&['user','none'].includes(data.look_at)){gazeMode=data.look_at;$<HTMLInputElement>('gaze').checked=gazeMode==='user';if(room.motion)room.motion.lookAt=gazeMode==='user';}
    }
    else if(data.type==='motion_preparing')status('准备动作，继续播放当前片段：'+data.prompt);
    else if(data.type==='motion'){if(loaded)room.motion?.accept(data);else pending=[...pending,data].slice(-5);}
    else if(data.type==='motion_trim'){
      room.motion?.trimAfter(data.frame,data.epoch);
      pending=pending.filter(p=>p.start<=data.frame).map(p=>({...p,roots:p.roots.slice(0,data.frame-p.start+1),rotations:p.rotations.slice(0,data.frame-p.start+1),joints:p.joints?.slice(0,data.frame-p.start+1)}));
    }else if(data.type==='play'){playState={playing:data.playing,epoch:data.epoch};room.motion?.play(data.playing,data.epoch);}
    else if(data.type==='seek')room.motion?.seek(data.frame);
    else if(data.type==='state'){
      generationTiming(data.state.debug||{});
      textEncoderRuntime(data.state.text_encoder);
      gazeMode=data.state.behavior_state?.gaze??gazeMode;$<HTMLInputElement>('gaze').checked=gazeMode==='user';if(room.motion)room.motion.lookAt=gazeMode==='user';
      autonomous=!!data.state.autonomy?.enabled;$('auto').textContent=autonomous?'AI ●':'AI';
      $('auto').title=autonomous?'停止 AI 自动规划':'开始 AI 自动规划';
      status(`${data.state.pending_request?'AI 思考中 · ':''}${data.state.phase||''} · ${data.state.playing?'播放中':'已暂停'} · 第 ${data.state.frame} 帧 · ${data.state.prompt||'等待手动动作'}`);
    }
  };
  socket.onclose=()=>{ready=false;updateEnabled();notice('连接已断开，请重启桌宠');};
}
let frameCount=0,maskCaptures=0;
function animate(now:number){
  const zooming=Math.abs(room.camera.zoom-targetZoom)>.003;
  const fps=document.hidden?10:zooming?60:bigHead?45:30;
  if(now-last<1000/fps-1){requestAnimationFrame(animate);return;}frameCount++;
  const dt=Math.max(0,(now-last)/1000);room.camera.zoom+=(targetZoom-room.camera.zoom)*(1-Math.exp(-12*dt));room.camera.updateProjectionMatrix();room.desktopZoomFraming=bigHead?0:Math.max(0,Math.min(1,(room.camera.zoom-1)/3));room.update(dt);last=now;
  if(loaded&&native&&now-lastMask>(zooming?80:333)&&(playState.playing||zooming||maskDirty)){
    const regions=['toolbar','panel','notice','head-bar'].map(id=>$(id)).filter(el=>!el.hidden).map(el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
    host('hitMask',{...room.desktopHitMask(),regions});lastMask=now;maskDirty=false;maskCaptures++;
  }
  if(now-lastFeedback>250&&loaded&&role==='runtime'&&socket?.readyState===WebSocket.OPEN){
    const feedback=room.motion?.feedback();if(feedback){const d=room.diagnostics();socket.send(JSON.stringify({type:'feedback',...feedback,joints:d.positions,spatial:d.spatial,body_perception:{emotion:room.generatedFace?.emotion,gaze:gazeMode,speaking:room.generatedFace?.snapshot().speaking,mouth_open:room.generatedFace?.snapshot().current.mouthOpen,eyes_closed:room.generatedFace?.snapshot().current.blink},motion_samples:room.motion?.drainObservations()}));}lastFeedback=now;
  }
  if(loaded&&!$('panel').hidden&&now-lastFaceStatus>200){const f=room.generatedFace?.snapshot();if(f){$('face-status').textContent=`${f.speaking?'说话中':'已收口'} · ${room.generatedFace?.emotion} · 情绪强度 ${f.affect.current.intensity.toFixed(2)} · 连续表情 ${f.windows} 段 · ${faceConfig.device==='cuda'?'GPU':faceConfig.device==='cpu'?'CPU':'设备加载中'}`;$('face-diagnostics').textContent=JSON.stringify(f,null,2);if(f.warnings.length)$('face-status').textContent+=' · '+f.warnings.join(' ');}lastFaceStatus=now;}
  if(native&&loaded&&!bigHead&&now-lastHeadAnchor>100){const anchor=room.headScreenAnchor();if(anchor)host('headAnchor',anchor);lastHeadAnchor=now;}
  requestAnimationFrame(animate);
}
let lastFaceStatus=0;
$<HTMLInputElement>('speech-bubble').checked=localStorage.getItem('pet-speech-bubble')==='on';
host('bubbleConfig',{enabled:true});$<HTMLInputElement>('speech-bubble').checked=true;
$('speech-bubble').onchange=()=>{const enabled=$<HTMLInputElement>('speech-bubble').checked;localStorage.setItem('pet-speech-bubble',enabled?'on':'off');host('bubbleConfig',{enabled});};
$<HTMLInputElement>('eye-micro').checked=localStorage.getItem('pet-eye-micro')!=='off';
$('eye-micro').onchange=()=>{const enabled=$<HTMLInputElement>('eye-micro').checked;localStorage.setItem('pet-eye-micro',enabled?'on':'off');if(room.eyeMicro)room.eyeMicro.enabled=enabled;};
for(const key of ['enabled','continuous','guidance','gain'])$('face-'+key).onchange=()=>void api('face',{enabled:$<HTMLInputElement>('face-enabled').checked,continuous:$<HTMLInputElement>('face-continuous').checked,guidance:$<HTMLInputElement>('face-guidance').checked,gain:Number($<HTMLInputElement>('face-gain').value)}).catch(e=>status(String(e)));
for(const key of ['speed','speech-rate']){const input=$<HTMLInputElement>('face-'+key);input.value=localStorage.getItem('pet-face-'+key)||input.value;input.oninput=()=>{localStorage.setItem('pet-face-'+key,input.value);if(room.generatedFace){room.generatedFace.speed=Number($<HTMLInputElement>('face-speed').value);room.generatedFace.speechRate=Number($<HTMLInputElement>('face-speech-rate').value);}};}
$('face-apply').onclick=()=>void api('face-state',{emotion:$<HTMLSelectElement>('face-emotion').value,state:Object.fromEntries(['valence','arousal','dominance','intensity','hold_s','decay_s'].map(k=>[k,Number($<HTMLInputElement>('face-'+k).value)]))}).catch(e=>status(String(e)));
$('face-reset').onclick=()=>void api('face-reset',{}).catch(e=>status(String(e)));
$('face-close-mouth').onclick=()=>void api('face-speech-stop',{}).catch(e=>status(String(e)));
if(new URLSearchParams(location.search).get('big-head')==='1')setBigHead(true);
if(new URLSearchParams(location.search).get('panel')==='open')togglePanel();
requestAnimationFrame(animate);void start().catch(e=>notice(String(e)));

$("auto").hidden=true;$("auto").onclick=null;layoutRing();


async function syncSharedSettings(){
  if(!token||(document.hasFocus()&&document.activeElement?.matches('#panel input,#panel select,#panel textarea')))return;
  const [m,e]=await Promise.all([api('motion-config'),api('text-encoder')]);cfg=m;
  $<HTMLInputElement>('history-frames').value=String(m.history_frames);$<HTMLInputElement>('denoising-steps').value=String(m.denoising_steps);$<HTMLSelectElement>('inference-backend').value=m.inference_backend;
  $<HTMLSelectElement>('text-encoder-mode').value=e.settings.mode;$<HTMLInputElement>('text-encoder-chunk').value=String(e.settings.layer_chunk);
  $<HTMLSelectElement>('behavior-mode').value=m.behavior_mode;$<HTMLInputElement>('inactive-minutes').value=String(m.inactive_minutes);$<HTMLInputElement>('settle-buffer').value=String(m.settle_buffer_s);$<HTMLSelectElement>('static-protection').value=m.static_protection;$<HTMLInputElement>('cached-idle-loops').checked=!!m.allow_cached_idle_loops;
}
let settingsSyncBusy=false;setInterval(async()=>{if($('panel').hidden||settingsSyncBusy)return;settingsSyncBusy=true;try{await syncSharedSettings();}catch(e){status(String(e));}finally{settingsSyncBusy=false;}},1000);
