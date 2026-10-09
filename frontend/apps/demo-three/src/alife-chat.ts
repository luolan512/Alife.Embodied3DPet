import './chat.css';
import './alife-chat.css';
import {renderPlanProgress} from './plan-progress';
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
const host=(type:string)=>(window as any).chrome?.webview?.postMessage({type});
let token='',lastMessages='',busy=false;
async function api(path:string,body?:unknown){const r=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data;}
const error=(e:unknown)=>{$('connection').textContent=String(e);};
async function refresh(){
  const [dialogue,state]=await Promise.all([api('alife/dialogue'),api('alife/state')]);
  const signature=JSON.stringify(dialogue.messages);
  if(signature!==lastMessages){lastMessages=signature;const log=$('messages'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<70;log.replaceChildren(...dialogue.messages.slice(-100).map((m:any)=>{const box=document.createElement('div');box.className='message '+m.role;const label=document.createElement('small');label.textContent=m.role==='user'?'你':'回应';box.append(label,document.createTextNode(m.text));return box;}));if(atBottom)log.scrollTop=log.scrollHeight;}
  renderPlanProgress(state);$('connection').textContent=state.connected?'身体已连接':'身体未连接';
  const emotions:Record<string,string>={happy:'开心',sad:'难过',angry:'生气',neutral:'平静',relaxed:'放松'};
  const emotion=state.face?.affect?.emotion;const task=state.current?.label||state.current?.action||'保持姿态';
  $('phase').textContent=`${task} · ${emotions[emotion]||emotion||'表情准备中'} · ${state.speaking?'正在说话':'安静陪伴'}`;
  $('plan-text').textContent=JSON.stringify({当前身体意图:state.current,后续计划:state.next,任务:state.tasks,表情:state.face?.affect,眼嘴控制:state.face?.action,语义准确性:'片段播放完成不等于姿态已验证'},null,2);
}
const input=(id:string)=>$<HTMLInputElement>(id),select=(id:string)=>$<HTMLSelectElement>(id);
async function syncSettings(){if(document.hasFocus()&&document.activeElement?.matches('#generation-panel input,#generation-panel select'))return;const [m,e]=await Promise.all([api('motion-config'),api('text-encoder')]);input('history-frames').value=String(m.history_frames);input('denoising-steps').value=String(m.denoising_steps);select('inference-backend').value=m.inference_backend;select('text-encoder-mode').value=e.settings.mode;input('text-encoder-chunk').value=String(e.settings.layer_chunk);}
$('drag').onpointerdown=e=>{if(e.button===0&&!(e.target as HTMLElement).closest('button'))host('dragChat');};
$('close').onclick=()=>{if((window as any).chrome?.webview)host('closeChat');else window.close();};
$('settings-toggle').onclick=()=>{$<HTMLDetailsElement>('generation-panel').open=!$<HTMLDetailsElement>('generation-panel').open;void syncSettings().catch(error);};
for(const [id,action] of [['continue','play'],['pause','pause']] as const)$(id).onclick=()=>void api('playback',{action}).catch(error);
$('stop').onclick=()=>void api('alife/stop',{}).catch(error);
$('apply-generation').onclick=async()=>{try{await api('motion-config',{history_frames:Number(input('history-frames').value),denoising_steps:Number(input('denoising-steps').value),inference_backend:select('inference-backend').value});$('settings-status').textContent='已应用，与 Alife 和桌宠右键配置同步。';}catch(e){$('settings-status').textContent=String(e);}};
$('text-encoder-save').onclick=async()=>{try{await api('text-encoder',{mode:select('text-encoder-mode').value,layer_chunk:Number(input('text-encoder-chunk').value)});$('settings-status').textContent='编码方式已同步保存，重新启用模块后生效。';}catch(e){$('settings-status').textContent=String(e);}};
$('send').onsubmit=async e=>{e.preventDefault();const box=$<HTMLTextAreaElement>('message'),text=box.value.trim();if(!text||busy)return;busy=true;$<HTMLButtonElement>('submit').disabled=true;try{await api('alife/input',{text});box.value='';await refresh();}catch(e){error(e);}finally{busy=false;$<HTMLButtonElement>('submit').disabled=false;}};
$('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$<HTMLFormElement>('send').requestSubmit();}};
async function start(){token=(await fetch('/api/v1/session').then(r=>r.json())).token;await Promise.all([refresh(),syncSettings()]);let polling=false;setInterval(async()=>{if(polling)return;polling=true;try{await refresh();if($<HTMLDetailsElement>('generation-panel').open)await syncSettings();}catch(e){error(e);}finally{polling=false;}},1000);}
void start().catch(error);
