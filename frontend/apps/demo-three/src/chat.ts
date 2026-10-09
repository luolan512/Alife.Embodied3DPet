import './chat.css';
import {renderPlanProgress} from './plan-progress';
const $=<T extends HTMLElement=HTMLElement>(id:string)=>document.getElementById(id) as T;
let token='',socket:WebSocket,automatic=false,busy=false,lastMessages='',aiReady=false;
const host=(type:string)=>{(window as any).chrome?.webview?.postMessage({type});};
async function api(path:string,body?:unknown){const r=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data;}
async function refreshProvider(){
  const cfg=await api('config');aiReady=!!cfg.configured;
  $('provider').textContent=aiReady?`${cfg.model} · 对话与动作`:'未配置 AI 接口';
  $<HTMLButtonElement>('submit').disabled=busy||!aiReady;
  $<HTMLButtonElement>('auto').disabled=!aiReady;
  $<HTMLTextAreaElement>('message').placeholder=aiReady?'想说什么？也可以要求动作':'未配置 AI 接口，请在“动作与配置”中设置';
  if(!aiReady)$('phase').textContent='未配置 AI 接口；手动动作仍可使用。';
}
function error(e:unknown){$('phase').textContent=String(e);}
function messages(items:any[]){
  const signature=JSON.stringify(items);if(signature===lastMessages)return;lastMessages=signature;
  $('messages').replaceChildren(...items.slice(-100).map(m=>{const div=document.createElement('div');div.className='message '+m.role;const label=document.createElement('small');label.textContent=m.role==='user'?'你':'小小伙伴';div.append(label,document.createTextNode(m.content));return div;}));$('messages').scrollTop=$('messages').scrollHeight;
}
$('drag').onpointerdown=e=>{if(e.button===0&&(e.target as HTMLElement).tagName!=='BUTTON')host('dragChat');};
$('close').onclick=()=>{if((window as any).chrome?.webview)host('closeChat');else window.close();};
let personaDefault='';
function personaCount(){$('persona-count').textContent=`${$<HTMLTextAreaElement>('persona-prompt').value.length}/2000`;}
$('persona-open').onclick=async()=>{try{const profile=await api('persona');personaDefault=profile.default_prompt;$<HTMLTextAreaElement>('persona-prompt').value=profile.prompt;$<HTMLInputElement>('persona-enabled').checked=profile.enabled;$('persona-panel').hidden=false;$('persona-status').textContent='';personaCount();}catch(e){error(e);}};
$('persona-close').onclick=()=>{$('persona-panel').hidden=true;};
$('persona-prompt').oninput=personaCount;
$('persona-default').onclick=()=>{$<HTMLTextAreaElement>('persona-prompt').value=personaDefault;$<HTMLInputElement>('persona-enabled').checked=true;$('persona-status').textContent='已填入示例，点击保存后生效。';personaCount();};
$('persona-save').onclick=async()=>{const button=$<HTMLButtonElement>('persona-save');button.disabled=true;try{const profile=await api('persona',{enabled:$<HTMLInputElement>('persona-enabled').checked,prompt:$<HTMLTextAreaElement>('persona-prompt').value});$<HTMLTextAreaElement>('persona-prompt').value=profile.prompt;personaCount();$('persona-status').textContent='已保存，下次回复与自主规划使用此设定。';}catch(e){$('persona-status').textContent=String(e);}finally{button.disabled=false;}};
$('auto').onclick=()=>void api('autonomy',{enabled:!automatic}).catch(error);
$('stop').onclick=()=>void api('stop',{}).catch(error);
$('reset').onclick=()=>void api('motion-reset',{}).catch(error);
$('new').onclick=()=>void api('conversation/new',{}).then(()=>api('history')).then(messages).catch(error);
$('send').onsubmit=async e=>{e.preventDefault();const input=$<HTMLTextAreaElement>('message'),text=input.value.trim();if(!aiReady){error('未配置 AI 接口，请在“动作与配置”中设置。');return;}if(!text||busy)return;busy=true;$<HTMLButtonElement>('submit').disabled=true;try{await api('chat',{message:text,face_dialogue:true});if(input.value.trim()===text)input.value='';$('phase').textContent='AI 正在规划，当前动作继续播放…';}catch(e){error(e);}finally{busy=false;$<HTMLButtonElement>('submit').disabled=!aiReady;}};
$('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$<HTMLFormElement>('send').requestSubmit();}};
async function start(){
  const session=await fetch('/api/v1/session').then(r=>r.json());token=session.token;
  await refreshProvider();$<HTMLButtonElement>('persona-open').disabled=false;setInterval(()=>void refreshProvider().catch(error),5000);
  messages(await api('history'));
  socket=new WebSocket(`ws://127.0.0.1:${session.ws_port}/?token=${encodeURIComponent(token)}&observer=1`);
  socket.onmessage=e=>{
    const data=JSON.parse(e.data);
    if(data.type==='welcome')messages(data.messages);
    else if(data.type==='chat')messages(data.messages);
    else if(data.type==='state'){
      const state=data.state;automatic=!!state.autonomy?.enabled;$('auto').textContent=automatic?'停止 AI 自动规划':'开始 AI 自动规划';
      $<HTMLButtonElement>('submit').disabled=busy||!aiReady;
      $('phase').textContent=aiReady?state.phase:'未配置 AI 接口；手动动作仍可使用。';
      renderPlanProgress(state);
      const observer=state.motion_observer,result=observer?.result;
      const names:Record<string,string>={possible_drift:'可能偏离目标',suspect:'出现偏离信号',no_drift_signal:'未检测到偏离信号'};
      const candidate=result?.windows?.find((r:any)=>r.stream==='rendered')?.nearest_candidate;
      $('motion-feedback').textContent=!observer?.enabled?'动作观察：已关闭':observer.result_fresh?`动作观察：${names[result.status]||result.status} · 最近候选 ${candidate||'未知'}`:`动作观察：${observer?.error?'模型不可用':observer?.phase==='loading_tmr'?'准备模型':'等待连续播放数据'}`;
      $('plan-text').textContent=JSON.stringify({当前:state.current,后续:state.next,实际动作条件:state.prompt},null,2);
    }
  };
  socket.onclose=()=>error('连接已断开，请重新打开聊天窗口');
}
void start().catch(error);
