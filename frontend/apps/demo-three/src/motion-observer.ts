import './motion-observer.css';
const $=(id:string)=>document.getElementById(id)!;
let token='',sequence=-1,lastTarget='';
let points:{frame:number;values:Record<number,number>}[]=[];
const colors:Record<number,string>={2:'#8057c7',4:'#188476',8:'#db8531'};
const stages:Record<string,string>={source:'ARDY 源骨架',retarget:'重定向后',rendered:'最终播放'};
const labels:Record<string,string>={no_drift_signal:'未发现偏离',suspect:'待确认',possible_drift:'可能跑偏'};
async function api(path:string,body?:unknown){const response=await fetch('/api/v1/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const value=await response.json();if(!response.ok)throw Error(value.error||'请求失败');return value;}
function plot(){
  const canvas=$('chart') as HTMLCanvasElement;canvas.width=Math.max(300,canvas.clientWidth)*devicePixelRatio;canvas.height=220*devicePixelRatio;
  const ctx=canvas.getContext('2d')!;ctx.scale(devicePixelRatio,devicePixelRatio);const width=canvas.clientWidth,height=220,left=38,right=width-12;
  ctx.font='11px sans-serif';ctx.lineWidth=1;
  for(const value of [-1,-.5,0,.5,1]){const y=20+(1-value)*88;ctx.strokeStyle='#e7e1ee';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();ctx.fillStyle='#776a84';ctx.fillText(String(value),5,y+4);}
  const first=points[0]?.frame??0,last=points.at(-1)?.frame??first+1;
  for(const window of [2,4,8]){ctx.strokeStyle=colors[window];ctx.lineWidth=2;ctx.beginPath();let started=false;for(const point of points){const value=point.values[window];if(value===undefined)continue;const x=left+(point.frame-first)/Math.max(1,last-first)*(right-left),y=20+(1-value)*88;if(started)ctx.lineTo(x,y);else ctx.moveTo(x,y);started=true;}ctx.stroke();if(points.length===1&&points[0].values[window]!==undefined){ctx.fillStyle=colors[window];ctx.beginPath();ctx.arc(left,20+(1-points[0].values[window])*88,3,0,Math.PI*2);ctx.fill();}}
  ctx.fillStyle='#776a84';ctx.fillText(`播放帧 ${first} → ${last}`,left,height-8);
}
function render(state:any){
  ($('enabled') as HTMLInputElement).checked=state.enabled;
  const result=state.result,fresh=state.result_fresh;
  $('status').textContent=!state.enabled?'观察已关闭':state.error?`TMR 不可用：${state.error}`:fresh?`${labels[result.status]||result.status} · 播放帧 ${result.frame}`:state.phase==='loading_tmr'?'正在准备本地 TMR 模型…':'等待连续播放数据；暂停或缺帧时不会给出当前动作判断。';
  $('status').dataset.state=fresh?result.status:'unknown';
  $('target').textContent=`实际播放目标：${state.target||'未知'}`;
  const identity=state.target+':'+(result?.epoch??'');
  if(lastTarget!==identity){points=[];lastTarget=identity;}
  if(fresh&&state.evaluation_count!==sequence){sequence=state.evaluation_count;points.push({frame:result.frame,values:Object.fromEntries(result.windows.filter((r:any)=>r.stream==='rendered').map((r:any)=>[r.window_s,r.target_cosine]))});points=points.slice(-120);}
  plot();
  $('scores').replaceChildren(...(result?.windows??[]).map((row:any)=>{const tr=document.createElement('tr');for(const value of [stages[row.stream],`${row.window_s} 秒`,row.target_cosine.toFixed(3),`${row.drop.toFixed(3)}${row.reference_verified?'（已确认参考）':'（未确认参考）'}`,`${row.nearest_candidate} / ${row.candidate_cosine.toFixed(3)}`,fresh?labels[row.status]:'旧结果']){const td=document.createElement('td');td.textContent=value;tr.append(td);}return tr;}));
  $('geometry').replaceChildren(...Object.entries(result?.geometry??{}).map(([stage,value])=>{const g=value as any,p=document.createElement('p');p.textContent=`${stages[stage]}：躯干倾斜 ${g.torso_tilt_degrees}°，髋高变化 ${g.hip_delta_m}m，峰值关节速度 ${g.peak_joint_speed_m_s}m/s；${g.flags.join('、')||'未触发代理规则'}${fresh?'':'（旧结果）'}`;return p;}));
  document.querySelectorAll<HTMLButtonElement>('[data-mark]').forEach(button=>button.disabled=!state.fresh||(button.dataset.mark==='baseline_good'&&!fresh));
  $('details').textContent=JSON.stringify({phase:state.phase,error:state.error,inference_ms:result?.inference_ms,estimated_joints:result?.estimated_joints,calibration:result?.calibration,recorded_bytes:state.recorded_bytes,recording_limit_reached:state.recording_limit_reached,recent_events:state.recent_events},null,2);
}
($('enabled') as HTMLInputElement).onchange=async e=>{try{render(await api('motion-observer',{enabled:(e.target as HTMLInputElement).checked}));}catch(error){$('status').textContent=String(error);}};
document.querySelectorAll<HTMLButtonElement>('[data-mark]').forEach(button=>button.onclick=async()=>{try{const marker=await api('motion-observer-annotation',{kind:button.dataset.mark});$('annotation').textContent=`已记录：播放帧 ${marker.frame}`;}catch(error){$('annotation').textContent=String(error);}});
($('apply-generation') as HTMLButtonElement).onclick=async()=>{try{await api('motion-config',{history_frames:Number(($('history-frames') as HTMLInputElement).value),denoising_steps:Number(($('denoising-steps') as HTMLInputElement).value)});$('generation-settings-status').textContent='已保存，下一次生成生效。';}catch(error){$('generation-settings-status').textContent=String(error);}};
async function start(){
  token=(await fetch('/api/v1/session').then(r=>r.json())).token;
  const cfg=await api('motion-config');($('history-frames') as HTMLInputElement).value=String(cfg.history_frames);($('denoising-steps') as HTMLInputElement).value=String(cfg.denoising_steps);($('apply-generation') as HTMLButtonElement).disabled=false;
  async function poll(){try{
    const [observer,state]=await Promise.all([api('motion-observer'),api('state')]);render(observer);
    const d=state.debug??{};const seconds=(v:any)=>typeof v==='number'?`${(v/1000).toFixed(3)}秒`:'尚无记录';
    $('generation-timing').textContent=`最近单次推理：${seconds(d.motion_inference_ms)}；含后处理：${seconds(d.motion_generation_ms)}；文本准备：${seconds(d.kimodo_encode_ms??d.first_motion_encode_ms)}；本段准备总耗时：${seconds(d.motion_prepare_ms)}。实际历史 ${d.motion_history_frames??'—'} 帧 / 去噪 ${d.motion_denoising_steps??'—'} 步 / 生成 ${d.motion_generated_frames??'—'} 帧；后端 ${d.motion_inference_backend??d.inference_backend_active??'—'}。`;
  }catch(error){$('status').textContent=String(error);}setTimeout(poll,1000);}await poll();
}
window.addEventListener('resize',plot);void start().catch(error=>$('status').textContent=String(error));
