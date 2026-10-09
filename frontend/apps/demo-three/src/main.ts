import './style.css';
import {Room} from '../../../adapters/runtime-three/room';
import type {SkeletonMetadata,MotionPacket} from '../../../packages/contracts/motion';
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
const room=new Room($('viewport'));
const savedGaze=localStorage.getItem('character-camera-gaze');
if(savedGaze==='off'||savedGaze==='follow'||savedGaze==='auto')room.setCameraGazePolicy(savedGaze);
$<HTMLSelectElement>('camera-gaze').value=room.cameraGazePolicy;
room.onLookResult=packet=>{if(role==='runtime'&&socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(packet));};
let token='',socket:WebSocket,role='observer',aiReady=false,ready=false,meta:SkeletonMetadata|undefined;
let pending:MotionPacket[]=[],loaded=false,lastFeedback=0;
let playState={playing:false,epoch:0};
let viewingConversation='';
let lastState:any={},models:{id:string;name:string}[]=[];
let visionConfig:any={enabled:false,source:'eye'},lastVisionCapture=0,visionFailureShown=false;
const parameterNames=['denoising_steps','history_frames','replan_buffer_frames','trigger_frames','transition_lead_frames','cfg_text','cfg_constraint','seed'];
function populateParameters(cfg:any){
  for(const name of parameterNames)$<HTMLInputElement>(name).value=String(cfg[name]);
  $<HTMLSelectElement>('motion-model').value=cfg.model;$<HTMLSelectElement>('inference-backend').value=cfg.inference_backend||'trt-fp16';$<HTMLInputElement>('official-postprocess').checked=!!cfg.official_postprocess;$<HTMLInputElement>('auto_replan').checked=cfg.auto_replan;
  $<HTMLInputElement>('startup_default_prompt').checked=cfg.startup_default_prompt;
  $<HTMLInputElement>('restart_from_now').checked=cfg.restart_from_now;
}
function debugView(){
  const d=lastState.debug||{};
  const fields=[['LLM 完整计划','llm_plan_ms'],['首次动作文本编码','first_motion_encode_ms'],['最近动作推理','motion_inference_ms'],['含后处理生成','motion_generation_ms'],['本段准备总耗时','motion_prepare_ms'],['最近数据导出','packet_export_ms'],['最近预留切换等待','transition_wait_ms'],['发送到首次动作','request_to_first_motion_ms']];
  $('latency').replaceChildren(...fields.map(([label,key])=>{const div=document.createElement('div'),small=document.createElement('small'),strong=document.createElement('strong');
    small.textContent=label;strong.textContent=d[key]==null?'—':`${(d[key]/1000).toFixed(3)} s`;div.append(small,strong);return div;}));
  $('debug-stage').textContent=`阶段：${d.stage||'待机'} · 首次动作缓存：${d.first_motion_cache_hit==null?'—':d.first_motion_cache_hit?'命中':'未命中'} · LLM 尝试：${d.llm_attempt??'—'} · HTTP：${d.llm_http_status??'—'} · 上下文字符：${d.context_chars??'—'}`;
  $('debug-details').textContent=JSON.stringify({server:lastState,runtime:room.diagnostics()},null,2);
  const usage=lastState.usage;
  if(usage){
    const format=(scope:any)=>Object.entries(scope||{}).map(([role,raw])=>{
      const item=raw as any;
      return `${role==='vision'?'视觉':role==='body'?'身体循环':'对话'}：${item.total_tokens_reported??'未知'} tokens（输入 ${item.input_tokens_reported??'?'} / 输出 ${item.output_tokens_reported??'?'} / 缓存命中 ${item.cached_input_tokens_reported??'?'} / 思考 ${item.reasoning_tokens_reported??'?'}），${item.attempts} 次请求，${item.attempts_without_total_usage} 次用量未知`;
    }).join('；')||'尚无请求';
    $('usage-status').textContent=`本次运行 ${usage.run_id}，已运行 ${Math.round(usage.elapsed_s/60)} 分钟 · ${format(usage.current_run)}。历史累计：${format(usage.all_runs)}。仅统计接口报告值；日志 ${usage.directory}`;
  }
}
function drawTimeline(){
  const canvas=$<HTMLCanvasElement>('timeline'),ctx=canvas.getContext('2d')!;
  const width=canvas.clientWidth;canvas.width=Math.max(1,width*devicePixelRatio);canvas.height=64*devicePixelRatio;ctx.scale(devicePixelRatio,devicePixelRatio);
  ctx.fillStyle='#eff2f7';ctx.fillRect(0,0,width,64);
  const start=lastState.motion_start_frame??0,end=Math.max(lastState.motion_end_frame??0,...(lastState.timeline||[]).map((s:any)=>s.end));
  const x=(f:number)=>Math.max(0,Math.min(width,(f-start)/Math.max(1,end-start)*width));
  ctx.fillStyle='#94cbb7';ctx.fillRect(x(lastState.frame??0),43,Math.max(0,x(lastState.motion_end_frame??0)-x(lastState.frame??0)),15);
  ctx.font='11px sans-serif';for(const s of lastState.timeline||[]){if(s.end<start)continue;ctx.fillStyle='#b1a5df';ctx.fillRect(x(s.start),7,Math.max(2,x(s.end)-x(s.start)),27);ctx.fillStyle='#3e355f';ctx.fillText(s.label,x(s.start)+4,25);}
  const cursor=room.motion?.cursor??lastState.frame??0;ctx.fillStyle='#574698';ctx.fillRect(x(cursor),0,2,64);
  $<HTMLInputElement>('scrubber').min=String(start);$<HTMLInputElement>('scrubber').max=String(Math.max(start,lastState.motion_end_frame??0));
  if(document.activeElement!==$('scrubber'))$<HTMLInputElement>('scrubber').value=String(Math.floor(cursor));
  $('frame-label').textContent=`${Math.floor(cursor)} / ${lastState.motion_end_frame??0} 帧 · ${(cursor/20).toFixed(2)} 秒 · 保留从 ${start} 帧`;
}
async function api(path:string,body?:unknown){
  const response=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',
    headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;
}
function toast(text:string){$('toast').textContent=text;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,6000);}
async function refreshAIStatus(){const provider=await api('config');aiReady=!!provider.configured;enable();if(!aiReady)$('turn-status').textContent='未配置 AI 接口';}
function enable(){$<HTMLButtonElement>('send').disabled=!(aiReady&&ready&&loaded&&role==='runtime'&&!viewingConversation);}
function chat(messages:{role:string;content:string}[],force=false){
  if(viewingConversation&&!force)return;
  $('messages').replaceChildren();
  for(const message of messages){
    const div=document.createElement('div');div.className='message '+(message.role==='user'?'user':'assistant');
    const label=document.createElement('span');label.className='label';label.textContent=message.role==='user'?'YOU':'COMPANION';
    div.append(label,document.createTextNode(message.content));$('messages').append(div);
  }
  $('messages').scrollTop=$('messages').scrollHeight;
}
async function load(url:string,name:string){
  loaded=false;enable();$('loading').hidden=false;
  try{
    const cap=await room.load(url,name);if(!cap)return;
    loaded=true;$('loading').hidden=true;$('model-info').textContent=`VRM ${cap.vrm_version} · ${cap.bones} 骨骼`;
    if(meta)room.motion?.setSkeleton(meta);
    for(const packet of pending)room.motion?.accept(packet);pending=[];
    room.motion?.play(playState.playing,playState.epoch);
    if(room.motion)room.motion.speed=Number($<HTMLSelectElement>('play-speed').value);
    socket.send(JSON.stringify({type:'capabilities',payload:{model_name:name,vrm_version:cap.vrm_version,
      bones:cap.bones,expressions:cap.expressions,layers:['ardy-body','eyes-blink','face-expression','spring-bone'],object_interaction:false}}));
    enable();
  }catch(exc){$('loading').textContent=String(exc);toast(String(exc));}
}
async function start(){
  const session=await fetch('/api/v1/session').then(r=>r.json());token=session.token;await refreshAIStatus();setInterval(()=>void refreshAIStatus().catch(console.error),5000);
  models=await api('models');
  $<HTMLSelectElement>('model-select').replaceChildren(...models.map(m=>{const option=document.createElement('option');option.value=m.id;option.textContent=m.name;return option;}));
  populateParameters(await api('motion-config'));
  applyVisionStatus(await api('vision'));
  await populateConversations();
  socket=new WebSocket(`ws://127.0.0.1:${session.ws_port}/?token=${encodeURIComponent(token)}`);
  socket.onmessage=event=>{
    const data=JSON.parse(event.data);
    if(data.type==='welcome'){
      role=data.role;ready=data.state.backend_ready;chat(data.messages);
      playState={playing:data.state.playing,epoch:data.state.epoch};
      $('connection').textContent=role==='runtime'?'本地已连接':'观察页面';$('connection').className='status online';
      if(role==='runtime'){const model=models[0];if(model)void load(`/models/${model.id}.vrm`,model.name);}
      else $('loading').textContent='另一页面正在控制角色；关闭它后刷新可接管。';
    }else if(data.type==='runtime_role'){
      role=data.role;enable();$('connection').textContent='观察页面（桌宠正在控制角色）';
    }else if(data.type==='backend_ready'){
      ready=true;meta=data.skeleton as SkeletonMetadata;
      if(data.reset){room.resetMotion(meta);playState={playing:false,epoch:0};}else room.motion?.setSkeleton(meta);
      if(room.motion)room.motion.speed=Number($<HTMLSelectElement>('play-speed').value);
      enable();$('backend-status').textContent='ARDY · 持续运动已就绪';
    }else if(data.type==='backend_loading'){
      ready=false;enable();$('backend-status').textContent='正在重载 ARDY 模型…';
    }else if(data.type==='motion'){
      if(loaded)room.motion?.accept(data);else{pending.push(data);pending=pending.slice(-5);}
    }else if(data.type==='motion_trim'){
      room.motion?.trimAfter(data.frame,data.epoch);
      pending=pending.filter(p=>p.start<=data.frame).map(p=>{
        const length=data.frame-p.start+1;
        return {...p,roots:p.roots.slice(0,length),rotations:p.rotations.slice(0,length),joints:p.joints?.slice(0,length)};
      });
    }else if(data.type==='motion_reset'){
      pending=[];meta=data.skeleton;room.resetMotion(meta!);playState={playing:false,epoch:data.epoch};
      if(room.motion)room.motion.epoch=data.epoch;
    }else if(data.type==='play'){
      playState={playing:data.playing,epoch:data.epoch};room.motion?.play(data.playing,data.epoch);
    }else if(data.type==='seek'){
      room.motion?.seek(data.frame);
    }else if(data.type==='look_request'){if(role==='runtime')room.requestLook(data);}
    else if(data.type==='root_recenter')room.motion?.recenter();
    else if(data.type==='look_release'||data.type==='look_cancel')room.releaseLook(data.request_id);
    else if(data.type.startsWith('face_'))room.facePacket(data);
    else if(data.type==='expression')room.setExpression(data.emotion,data.look_at,data.look_target,data.gaze_scope);
    else if(data.type==='chat')chat(data.messages);
    else if(data.type==='state'){
      lastState=data.state;debugView();drawTimeline();
      if(data.state.vision)applyVisionStatus(data.state.vision);
      const memory=data.state.conversation_context;
      if(memory)$('context-status').textContent=`上下文：最近 ${memory.recent_messages}/${memory.recent_limit_messages} 条消息，无记忆摘要；完整聊天仅本地归档。`;
      const body=data.state.body_loop;
      if(body)$('body-status').textContent=`身体循环：${body.phase} · 反馈 ${body.observation_count} · 决策 ${body.decision_count}`;
      const world=data.state.world_state;
      if(world)$('spatial-status').textContent=`身体朝向 ${world.actor.facing} (${world.actor.heading_degrees??'—'}°) · 镜头 ${world.camera?.mode??'—'} · 北 −Z / 南 +Z`;
      if(ready)$('backend-status').textContent=`ARDY ${data.state.motion_parameters?.model??'core40'} · ${data.state.horizon_frames} 帧窗口 · ${data.state.playing?'播放中':'已暂停'}`;
      $('turn-status').textContent=aiReady?data.state.phase:'未配置 AI 接口';
      $('state').textContent=JSON.stringify({current:data.state.current,next:data.state.next,events:data.state.events,body_loop:data.state.body_loop},null,2);
    }else if(data.type==='backend_error'){toast(data.error);$('backend-status').textContent='运动服务异常：'+data.error;}
  };
  socket.onclose=()=>{ready=false;enable();$('connection').textContent='连接已断开，请刷新';};
}
async function submit(){
  if(viewingConversation)return;
  const input=$<HTMLTextAreaElement>('message');const message=input.value.trim();if(!message)return;
  try{await api('chat',{message});input.value='';$('turn-status').textContent='她正在想…';}
  catch(exc){toast(String(exc));$('turn-status').textContent=String(exc);}
}
$('chat-form').addEventListener('submit',event=>{event.preventDefault();void submit();});
$('message').addEventListener('keydown',event=>{if(event instanceof KeyboardEvent&&event.key==='Enter'&&!event.shiftKey){event.preventDefault();if(!$<HTMLButtonElement>('send').disabled)void submit();}});
document.querySelectorAll<HTMLButtonElement>('[data-prompt]').forEach(button=>button.onclick=()=>{$<HTMLTextAreaElement>('message').value=button.dataset.prompt||'';if(!$<HTMLButtonElement>('send').disabled)void submit();});
async function populateConversations(){
  const sessions=await api('conversations');
  const select=$<HTMLSelectElement>('conversation-archive');
  const current=document.createElement('option');current.value='';current.textContent='当前对话';
  select.replaceChildren(current,...sessions.filter((item:any)=>!item.active&&item.message_count>0).map((item:any)=>{
    const option=document.createElement('option');option.value=item.id;option.textContent=`${new Date(item.created_at*1000).toLocaleString()} · ${item.message_count} 条（归档，只读）`;return option;
  }));select.value=viewingConversation;
}
$('conversation-archive').onchange=async()=>{
  viewingConversation=$<HTMLSelectElement>('conversation-archive').value;enable();
  try{chat(await api('history'+(viewingConversation?'?conversation_id='+encodeURIComponent(viewingConversation):'')),true);}
  catch(exc){toast(String(exc));}
};
$('new-conversation').onclick=async()=>{
  const button=$<HTMLButtonElement>('new-conversation');button.disabled=true;
  try{await api('conversation/new',{});viewingConversation='';chat([],true);$<HTMLTextAreaElement>('message').value='';await populateConversations();enable();toast('旧记录已归档；对话和行动记忆已重置，身体暂停。');}
  catch(exc){toast(String(exc));}finally{button.disabled=false;}
};
$('camera-gaze').onchange=()=>{const policy=$<HTMLSelectElement>('camera-gaze').value as 'auto'|'off'|'follow';room.setCameraGazePolicy(policy);localStorage.setItem('character-camera-gaze',policy);};
$('recenter').onclick=()=>{if(role==='runtime')void api('recenter',{}).then(()=>toast('已重定位显示轨迹，动作继续播放')).catch(exc=>toast(String(exc)));};
$('body-start').onclick=()=>{void api('body-loop',{enabled:true}).catch(exc=>toast(String(exc)));};
$('body-disable').onclick=()=>{void api('body-loop',{enabled:false}).catch(exc=>toast(String(exc)));};
$('stop').onclick=()=>{void api('stop',{}).catch(exc=>toast(String(exc)));};
$('model-select').addEventListener('change',async()=>{
  if(role!=='runtime')return;const model=models.find(m=>m.id===$<HTMLSelectElement>('model-select').value);
  if(model){await api('stop',{});await load(`/models/${model.id}.vrm`,model.name);}
});
$('skeleton-debug').addEventListener('change',()=>room.showSkeleton($<HTMLInputElement>('skeleton-debug').checked));
for(const mode of ['full','face','eye'] as const)$('camera-'+mode).onclick=()=>room.cameraPreset(mode);
document.addEventListener('keydown',event=>{
  const target=event.target;
  if(event.altKey||event.ctrlKey||event.metaKey||(target instanceof HTMLElement&&target.closest('input,textarea,select,[contenteditable],dialog')))return;
  const mode=({'1':'full','2':'face','3':'eye'} as const)[event.key as '1'|'2'|'3'];
  if(mode){event.preventDefault();room.cameraPreset(mode);}
});
for(const action of ['play','pause'])$(action).onclick=()=>{void api('playback',{action}).catch(exc=>toast(String(exc)));};
async function seek(frame:number){try{await api('playback',{action:'seek',frame});}catch(exc){toast(String(exc));}}
$('scrubber').addEventListener('change',()=>void seek(Number($<HTMLInputElement>('scrubber').value)));
$('prev-frame').onclick=()=>void seek(Math.max(lastState.motion_start_frame??0,Math.floor(room.motion?.cursor??0)-1));
$('next-frame').onclick=()=>void seek(Math.min(lastState.motion_end_frame??0,Math.floor(room.motion?.cursor??0)+1));
$('play-speed').addEventListener('change',()=>{if(room.motion)room.motion.speed=Number($<HTMLSelectElement>('play-speed').value);});
$('motion-settings').addEventListener('submit',async event=>{
  event.preventDefault();const body:any={official_postprocess:$<HTMLInputElement>('official-postprocess').checked,inference_backend:$<HTMLSelectElement>('inference-backend').value,model:$<HTMLSelectElement>('motion-model').value,auto_replan:$<HTMLInputElement>('auto_replan').checked,startup_default_prompt:$<HTMLInputElement>('startup_default_prompt').checked,restart_from_now:$<HTMLInputElement>('restart_from_now').checked};
  for(const name of parameterNames)body[name]=Number($<HTMLInputElement>(name).value);
  try{populateParameters(await api('motion-config',body));$('motion-config-status').textContent='已应用；生成参数从下一次推理生效';}catch(exc){toast(String(exc));}
});
$('motion-test').addEventListener('submit',async event=>{event.preventDefault();try{await api('motion-test',{prompt:$<HTMLTextAreaElement>('motion-prompt').value,duration_s:Number($<HTMLInputElement>('motion-duration').value)});}catch(exc){toast(String(exc));}});
$('export-debug').onclick=()=>{const data={saved_at:new Date().toISOString(),server:lastState,runtime:room.diagnostics()};const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download='character-debug-'+Date.now()+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('model-file').addEventListener('change',async()=>{
  const file=$<HTMLInputElement>('model-file').files?.[0];if(!file||role!=='runtime')return;
  await api('stop',{});const url=URL.createObjectURL(file);try{await load(url,file.name);}finally{URL.revokeObjectURL(url);}
});
$('settings-toggle').onclick=async()=>{
  try{const cfg=await api('config');aiReady=!!cfg.configured;$<HTMLInputElement>('base-url').value=cfg.base_url;$<HTMLInputElement>('model').value=cfg.model;
    $<HTMLInputElement>('thinking-enabled').checked=cfg.thinking_enabled??false;
    $<HTMLInputElement>('body-interval').value=String(cfg.body_interval_s??20);$<HTMLInputElement>('idle-interval').value=String(cfg.idle_interval_s??45);
    $<HTMLInputElement>('auto').checked=cfg.auto_continue;$<HTMLInputElement>('json-mode').checked=cfg.json_mode;
    $<HTMLDialogElement>('settings').showModal();}catch(exc){toast(String(exc));}
};
$('settings-close').onclick=()=>$<HTMLDialogElement>('settings').close();
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();try{await api('config',{base_url:$<HTMLInputElement>('base-url').value,model:$<HTMLInputElement>('model').value,
    thinking_enabled:$<HTMLInputElement>('thinking-enabled').checked,body_interval_s:Number($<HTMLInputElement>('body-interval').value),idle_interval_s:Number($<HTMLInputElement>('idle-interval').value),api_key:$<HTMLInputElement>('api-key').value,auto_continue:$<HTMLInputElement>('auto').checked,json_mode:$<HTMLInputElement>('json-mode').checked});
    $<HTMLInputElement>('api-key').value='';$<HTMLDialogElement>('settings').close();toast('接口已保存');await refreshAIStatus();}catch(exc){$('settings-error').textContent=String(exc);}
});
let previous=performance.now(),frames=0,lastFPS=previous;
function applyVisionStatus(state:any){
  visionConfig=state.config;
  for(const [id,key] of [['vision-active','active_enabled'],['vision-background','background_enabled']])if(document.activeElement!==$(id))$<HTMLInputElement>(id).checked=visionConfig[key];
  if(document.activeElement!==$('vision-min'))$<HTMLInputElement>('vision-min').value=String(visionConfig.min_interval_s);
  if(document.activeElement!==$('vision-max'))$<HTMLInputElement>('vision-max').value=String(visionConfig.max_interval_s);
  if(document.activeElement!==$('vision-enabled'))$<HTMLInputElement>('vision-enabled').checked=visionConfig.enabled;
  if(document.activeElement!==$('vision-source'))$<HTMLSelectElement>('vision-source').value=visionConfig.source;
  const observation=lastState.world_state?.visual_observation;
  const look=lastState.active_look;
  $('vision-status').textContent=`视觉 ${!state.config.enabled?'总开关关闭':(state.config.active_enabled?'主动看 ':'')+(state.config.background_enabled?'后台摘要':'')} · ${look?.phase??state.phase} · ${look?.target?JSON.stringify(look.target):''} ${look?.question??''} · ${observation?.age_s??'—'} 秒前 · ${state.error??''}`;
  $('vision-result').textContent=observation?.summary??(observation?.status==='stale'?'观察已过期，等待更新':'尚未形成视觉观察');
}
async function updateVision(){
  try{applyVisionStatus(await api('vision',{enabled:$<HTMLInputElement>('vision-enabled').checked,source:$<HTMLSelectElement>('vision-source').value,active_enabled:$<HTMLInputElement>('vision-active').checked,background_enabled:$<HTMLInputElement>('vision-background').checked,
    min_interval_s:Number($<HTMLInputElement>('vision-min').value),max_interval_s:Number($<HTMLInputElement>('vision-max').value)}));}
  catch(exc){toast(String(exc));}
}
$('vision-enabled').addEventListener('change',()=>void updateVision());
$('vision-source').addEventListener('change',()=>void updateVision());
$('vision-active').addEventListener('change',()=>void updateVision());
$('vision-background').addEventListener('change',()=>void updateVision());
$('vision-apply').onclick=()=>void updateVision();
$('vision-force').onclick=()=>void api('perception-event',{type:'FORCE_OBSERVE',reason:'manual_force'}).catch(exc=>toast(String(exc)));
$('vision-observe').onclick=()=>void api('vision-observe',{}).catch(exc=>toast(String(exc)));
function animate(now:number){
  const dt=(now-previous)/1000;previous=now;room.update(dt);frames++;
  if(now-lastVisionCapture>500&&visionConfig.enabled&&loaded&&role==='runtime'&&socket?.readyState===WebSocket.OPEN&&socket.bufferedAmount<300000&&!document.hidden){
    lastVisionCapture=now;
    try{const frame=room.captureVision(visionConfig.source);if(frame){socket.send(JSON.stringify(frame));$<HTMLImageElement>('vision-preview').src=frame.image;}}
    catch(exc){if(!visionFailureShown){toast('视觉采集失败：'+String(exc));visionFailureShown=true;}}
  }
  if(now-lastFPS>1000){$('fps').textContent=Math.round(frames*1000/(now-lastFPS))+' FPS';frames=0;lastFPS=now;}
  if(now-lastFeedback>250&&loaded&&role==='runtime'&&socket?.readyState===WebSocket.OPEN){
    const feedback=room.motion?.feedback();if(feedback){const diagnostics=room.diagnostics();socket.send(JSON.stringify({type:'feedback',...feedback,joints:diagnostics.positions,spatial:diagnostics.spatial,motion_samples:room.motion?.drainObservations()}));
      $('actor-state').textContent=`位置 (${feedback.position[0].toFixed(2)}, ${feedback.position[2].toFixed(2)})`;
      $('runtime-status').textContent=feedback.waiting?'等待运动缓冲':`播放帧 ${feedback.frame} · 缓冲 ${feedback.buffered_seconds.toFixed(1)}s`;}
    lastFeedback=now;
  }
  requestAnimationFrame(animate);
}
Object.assign(window,{characterDiagnostics:()=>room.diagnostics()});
requestAnimationFrame(animate);void start().catch(exc=>toast(String(exc)));
