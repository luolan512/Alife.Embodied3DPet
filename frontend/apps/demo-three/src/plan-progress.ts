type Receipt={id:string;label:string;action:string;prompt:string;kind:string;duration_s:number;repeat:number;status:string;elapsed_s?:number;phase?:string;continuation?:string;progress?:number;playback_total_s?:number};
type Entry={id:string;goal:string;source:string;created_at:number;status:string;steps:Receipt[]};
const statusNames:Record<string,string>={queued:'下一步',preparing:'准备中',running:'执行中',looping:'持续循环',completed:'已播完',interrupted:'已切换',cancelled:'已停止',removed:'已移除',error:'执行失败'};
const phaseNames:Record<string,string>={action:'任务动作',settling:'收尾补帧',loop:'循环续帧',paused:'暂停',preparing:'预生成 / 等待衔接'};
let previous='';
let previousCurrentId:string|undefined,previousPlanId:string|undefined;
const rows=new Map<string,HTMLElement>();
function node(tag:string,text?:string,className?:string){const el=document.createElement(tag);if(text)el.textContent=text;if(className)el.className=className;return el;}
export function renderPlanProgress(state:any){
 const plans:Entry[]=state.plan_progress?.plans||[],currentId=state.plan_progress?.current_step_id;
 const active=plans.flatMap(p=>p.steps).find(s=>s.id===currentId);
 const activePlanId=plans.find(p=>p.steps.some(s=>s.id===currentId))?.id;
 const mode=document.getElementById('progress-mode')!;
 mode.textContent=active?`${phaseNames[active.phase||'preparing']||'执行中'} · ${active.continuation==='cached_idle'?'缓存复用':'ARDY'}${state.pending_request?' · AI思考中':''}`:state.pending_request?'AI正在编排计划':'等待新计划';
 const behavior=state.behavior_state||{};
 const ahead=behavior.motion_lookahead||{};
 if(ahead.ready)mode.textContent+=' · 下一步运动已预生成';
 else if(ahead.preparing)mode.textContent+=' · 提前生成下一步运动';
 if(behavior.waiting_motion_live)mode.textContent+=' · 延续上一动作，等待下一段';
 else if(behavior.waiting_for_next)mode.textContent+=' · 上一步已结束，保持末姿态等下一步';
 else if(behavior.static_pending)mode.textContent+=' · 等待姿势稳定';
 else if(behavior.static_protection==='freeze')mode.textContent+=state.frame>=behavior.snapshot_frame?' · 快照定格':' · 收尾后定格';
 else if(behavior.static_protection==='anchor')mode.textContent+=' · 快照约束续帧';
 const signature=JSON.stringify(plans.map(p=>({...p,steps:p.steps.map(({elapsed_s,progress,playback_total_s,...s})=>s)})))+currentId;
 const list=document.getElementById('plan-list')!;
 if(signature!==previous){
  previous=signature;const open=new Set(Array.from(list.querySelectorAll<HTMLDetailsElement>('details[open]')).map(e=>e.dataset.id));
  const scroll=list.scrollTop;rows.clear();const entries=plans.slice().reverse().map(plan=>{
   const group=document.createElement('details');group.className='plan-entry';group.dataset.id=plan.id;
   group.open=(open.has(plan.id)&&plan.id!==previousPlanId)||plan.id===activePlanId||plans.length===1;
   const summary=node('summary');summary.append(node('strong',plan.goal||'动作计划'));
   const stamp=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(plan.created_at*1000));
   summary.append(node('small',stamp+' · '+(plan.status==='looping'?'循环中':plan.status==='active'?'执行中':'历史')));group.append(summary);
   const shots=plan.steps.filter(s=>s.kind!=='loop'),done=shots.filter(s=>s.status==='completed').length;
   if(shots.length){group.append(node('small',`任务动作已播完 ${done}/${shots.length}`,'plan-count'));const total=document.createElement('progress');total.max=shots.length;total.value=done;total.className='plan-total';group.append(total);}
   const rail=node('ol',undefined,'plan-rail');
   for(const step of plan.steps){
    const row=node('li',undefined,'plan-step '+step.status);row.dataset.stepId=step.id;row.title=step.prompt;
    row.append(node('span',statusNames[step.status]||step.status,'step-status'),node('strong',step.label||step.action));
    row.append(node('small',step.kind==='loop'?`Loop · 持续 · 参考窗口 ${step.duration_s}s`:`One-shot · 预计 ${step.duration_s}s${step.repeat>1?' × '+step.repeat:''}`));
    if(step.kind==='loop'){row.append(node('div',undefined,'loop-track'));}
    else {const bar=document.createElement('progress');bar.max=1;bar.value=step.status==='completed'?1:0;row.append(bar);}
    row.append(node('small',undefined,'step-clock'));rows.set(step.id,row);rail.append(row);
   }
   group.append(rail);return group;
  });list.replaceChildren(...entries);if(!plans.length)list.append(node('p','收到动作任务后，计划与历史会显示在这里。','empty-plan'));list.scrollTop=currentId&&currentId!==previousCurrentId?0:scroll;
  if(activePlanId)previousPlanId=activePlanId;previousCurrentId=currentId;
 }
 for(const plan of plans)for(const step of plan.steps){
  const row=rows.get(step.id);if(!row)continue;const clock=row.querySelector('.step-clock')!;
  row.dataset.phase=step.phase||'';
  clock.textContent=step.id===currentId?`${phaseNames[step.phase||'preparing']||''} · 已播放 ${step.elapsed_s||0}s${step.kind!=='loop'&&step.playback_total_s?' / 播放段 '+step.playback_total_s+'s':''}`:step.elapsed_s!==undefined?`实际播放 ${step.elapsed_s}s`:step.status==='queued'?'等待前一步':'';
  const bar=row.querySelector('progress');if(bar&&step.id===currentId)bar.value=step.progress||0;
 }
}
