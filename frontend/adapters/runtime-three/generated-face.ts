import * as THREE from 'three';
import {VRMExpression,VRMExpressionMorphTargetBind,type VRM} from '@pixiv/three-vrm';
import {Affect} from './affect';

export type FaceWeights=Record<string,number>;
const names:Record<string,string[]>={
 browInnerUp:['Fcl_BRW_Sorrow','browInnerUp','BrowInnerLeft','BrowInnerRight','困る'],browOuterUp:['Fcl_BRW_Surprised','browOuterUpLeft','browOuterUpRight','BrowUpLeft','BrowUpRight'],
 browDown:['Fcl_BRW_Angry','browDownLeft','browDownRight','BrowDownLeft','BrowDownRight','怒り'],eyeWide:['Fcl_EYE_Surprised','eyeWideLeft','eyeWideRight','EyeWideLeft','EyeWideRight'],
 eyeSquint:['Fcl_EYE_Angry','eyeSquintLeft','eyeSquintRight','EyeCompLeft','EyeCompRight','笑い'],mouthSmile:['__miku_closed_smile','Fcl_MTH_Fun','Fcl_MTH_Up','mouthSmileLeft','mouthSmileRight','MouthSmile'],
 mouthFrown:['Fcl_MTH_Down','mouthFrownLeft','mouthFrownRight','MouthTrouble','∧'],mouthOpen:['Fcl_MTH_A','jawOpen','あ','A'],mouthPucker:['Fcl_MTH_U','mouthPucker','う','U'],
 mouthI:['Fcl_MTH_I','い','I'],mouthO:['Fcl_MTH_O','お','O'],
};
const clamp=(v:number)=>Math.max(0,Math.min(1,v));
const canonical=(name:string)=>name.toLowerCase().replace(/[^\p{L}\p{N}]/gu,'');
const mouthPresets:Record<string,string[]>={mouthOpen:['aa','a','jawOpen','mouthOpen','vrc.v_aa','viseme_aa','mouth_a'],mouthI:['ih','i','ee','e','vrc.v_ih','viseme_ih'],mouthPucker:['ou','u','vrc.v_ou','viseme_ou'],mouthO:['oh','o','vrc.v_oh','viseme_oh']};

export class GeneratedFace {
 enabled=true;gain=1;id=0;mode='waiting';time=0;frames:FaceWeights[]=[];done=false;cursor=0;
 speed=1.5;speechRate=3.8;
 guidance=true;affect=new Affect();reference:FaceWeights={};continuation=false;windows=0;
 private speechEventId=-1;
 private actionId=-1;
 private mouthAction?:{value:number;until:number};
 private eyeAction?:{value:number;until:number};
 private miku=false;
 modelDetail:FaceWeights={};
 current:FaceWeights={};start:FaceWeights={};hold:FaceWeights={};speakingUntil=0;blendTime=0;holdTime=0;received=0;
 mapping:Record<string,string[]>={};emotion='neutral';error='';
 private binds:Record<string,string[]>={};
 private rawTargets=new Map<string,{mesh:THREE.Mesh;index:number}[]>();
 private fallbackEmotions:Record<string,string>={};
 warnings:string[]=[];
 constructor(private vrm:VRM){
  const raw=new Map<string,{mesh:THREE.Mesh;index:number}[]>();
  vrm.scene.traverse(o=>{if(o instanceof THREE.Mesh&&o.morphTargetDictionary){for(const [name,index]of Object.entries(o.morphTargetDictionary)){const list=raw.get(name)??[];list.push({mesh:o,index});raw.set(name,list);}}});
  // Miku has a closed frown but no closed smile. Reverse that local lip deformation;
  // never borrow the open-mouth ワ or a whole-face emotional preset.
  this.miku=raw.has('困る')&&raw.has('笑い')&&raw.has('∧');
  if(this.miku)raw.set('__miku_closed_smile',raw.get('∧')!);
  this.rawTargets=raw;
  for(const [channel,candidates]of Object.entries(names)){
   this.binds[channel]=[];this.mapping[channel]=[];
   for(const candidate of candidates){const name=[...raw.keys()].find(n=>canonical(n)===canonical(candidate))??candidate;if(channel==='mouthSmile'&&name.startsWith('Fcl_')&&this.mapping[channel].some(n=>n.startsWith('Fcl_')))continue;const matches=raw.get(name);if(!matches)continue;
    const key='__cteg_'+name;let expression=vrm.expressionManager!.getExpression(key);
    if(!expression){expression=new VRMExpression(key);for(const {mesh,index}of matches)expression.addBind(new VRMExpressionMorphTargetBind({primitives:[mesh],index,weight:name==='__miku_closed_smile'?-1:1}));vrm.expressionManager!.registerExpression(expression);}
    this.binds[channel].push(key);this.mapping[channel].push(name);
   }
  }
  const manager=vrm.expressionManager;
  if(manager){
   const existing=Object.entries(manager.expressionMap??{});
   const clone=(source:VRMExpression,key:string)=>{const expression=new VRMExpression(key);expression.isBinary=false;for(const bind of source.binds)expression.addBind(bind);manager.registerExpression(expression);return key;};
   for(const [channel,aliases] of Object.entries(mouthPresets)){
    if(this.binds[channel]?.length)continue;
    const source=existing.find(([name,expression])=>expression.binds.length>0&&aliases.some(a=>canonical(name)===canonical(a)));
    if(source){this.binds[channel]=[clone(source[1],'__cteg_mouth_'+channel)];this.mapping[channel]=['VRM:'+source[0]];this.rawTargets.set('VRM:'+source[0],source[1].binds.flatMap(bind=>bind instanceof VRMExpressionMorphTargetBind?bind.primitives.map(mesh=>({mesh,index:bind.index})):[]));continue;}
    const name=[...raw.keys()].find(n=>aliases.some(a=>canonical(n)===canonical(a)||(canonical(a).length>=6&&canonical(n).endsWith(canonical(a)))));
    if(name){const expression=new VRMExpression('__cteg_mouth_'+channel);for(const {mesh,index} of raw.get(name)!)expression.addBind(new VRMExpressionMorphTargetBind({primitives:[mesh],index,weight:1}));manager.registerExpression(expression);this.binds[channel]=[expression.expressionName];this.mapping[channel]=[name];}
   }
   // Standard whole-face expressions are a fallback for models without local muscle shapes.
   {
    const local:Record<string,boolean>={happy:!!this.binds.mouthSmile.length,sad:!!(this.binds.browInnerUp.length||this.binds.mouthFrown.length),angry:!!this.binds.browDown.length,relaxed:!!this.binds.mouthSmile.length};
    for(const [label,aliases] of Object.entries({happy:['happy','joy'],sad:['sad','sorrow'],angry:['angry'],relaxed:['relaxed','fun']})){
     if(local[label])continue;
     const source=existing.find(([name,expression])=>expression.binds.length>0&&aliases.includes(name.toLowerCase()));
     if(source)this.fallbackEmotions[label]=clone(source[1],'__cteg_emotion_'+label);
    }
    if(Object.keys(this.fallbackEmotions).length)this.warnings.push('使用 VRM 标准整脸表情兜底；精细眉眼与闭口笑容取决于模型自身配置。');
    else if(!Object.values(local).some(Boolean))this.warnings.push('模型没有有效情绪表情绑定；仅有嘴型时不能生成眉眼或笑容。');
   }
  }
  if(!this.binds.mouthOpen.length){
   // A model with only another vowel can still articulate, but report the approximation.
   const alternative=['mouthI','mouthO','mouthPucker'].find(c=>this.binds[c].length);
   if(alternative){this.binds.mouthOpen=this.binds[alternative];this.mapping.mouthOpen=this.mapping[alternative];this.binds[alternative]=[];this.mapping[alternative]=[];this.warnings.push('缺少 A/aa 嘴型，使用已有元音近似说话。');}
   else this.warnings.push('模型没有有效嘴型绑定；需配置 aa/A 或 jawOpen 形变后才能张嘴，运行时无法凭空生成嘴部几何。');
  }
 }
 packet(data:any){
  if(data.type==='face_action'){
   const mouth=data.mouth_open,eyes=data.eyes_closed;
   if(data.id<this.actionId||(mouth==null&&eyes==null)||[mouth,eyes].some(v=>v!=null&&(!Number.isFinite(v)||v<0||v>1))||!Number.isFinite(data.hold_s)||data.hold_s<0||data.hold_s>120)return;
   this.actionId=data.id;
   if(mouth!=null)this.mouthAction=data.hold_s>0?{value:mouth,until:this.time+data.hold_s}:undefined;
   if(eyes!=null)this.eyeAction=data.hold_s>0?{value:eyes,until:this.time+data.hold_s}:undefined;
   if((this.mode==='waiting'||this.mode==='releasing')&&(this.mouthAction||this.eyeAction))this.mode='hold';return;
  }
  if(data.type==='face_config'){this.enabled=data.enabled;this.gain=data.gain;this.guidance=data.guidance??true;if(!this.enabled)this.reset(true);return;}
  if(data.type==='face_state'){this.affect.packet(data);this.emotion=data.emotion;if(this.mode==='waiting'&&data.intensity>0)this.mode='hold';return;}
  if(data.type==='face_speech'){if(data.id<this.speechEventId)return;this.speechEventId=data.id;this.speakingUntil=this.time+Math.max(0,Number(data.duration_s)||0);if(this.mode==='waiting'&&data.duration_s>0)this.mode='hold';return;}
  if(data.type==='face_error'){this.error=data.error;this.enabled=false;this.reset(true);return;}
  if(data.type==='face_reset'){this.mouthAction=undefined;this.eyeAction=undefined;this.frames=[];this.hold={};this.done=false;this.speakingUntil=0;this.mode='releasing';this.id=data.id;return;}
  if(data.type==='face_restore'){if(data.id<this.id)return;this.id=data.id;this.frames=[];this.hold={...data.frame,mouthOpen:0,mouthPucker:0};this.reference={...this.hold};if(!this.guidance)this.current={...this.hold};this.mode='hold';this.emotion=data.emotion;this.gain=data.gain;this.speakingUntil=0;return;}
  if(data.type==='face_target'){this.emotion=data.emotion;return;}
  if(data.type==='face_pending'){
   if(data.id<this.id)return;this.id=data.id;this.done=false;this.frames=[];this.cursor=0;this.mode='preparing';this.emotion=data.emotion;this.received=0;this.continuation=!!data.continuation;
   return;
  }
  if(data.id!==this.id)return;
  if(data.type==='face_batch'){
   if(!Array.isArray(data.frames)||data.offset!==this.frames.length||this.frames.length+data.frames.length>240)return;
   if(data.frames.some((f:any)=>Object.values(f).some(v=>typeof v!=='number'||!Number.isFinite(v))))return;
   if(data.first){this.start={...this.current};this.reference={...data.frames[0]};this.blendTime=0;this.mode='playing';if(!data.continuation&&this.speechEventId!==data.id&&data.speaking_s>0)this.speakingUntil=this.time+data.speaking_s;this.gain=data.gain;this.windows++;}
   this.frames.push(...data.frames);this.received=this.frames.length;
  }else if(data.type==='face_done')this.done=true;
 }
 reset(preserveAction=false){if(!preserveAction){this.mouthAction=undefined;this.eyeAction=undefined;}this.frames=[];this.current={};this.hold={};this.start={};this.mode=this.mouthAction||this.eyeAction?'hold':'waiting';this.speakingUntil=0;this.done=false;this.clear();}
 eyelids(left:number,right:number){const value=this.eyeAction&&this.time<this.eyeAction.until?this.eyeAction.value:null;return {left:value??left,right:value??right};}
 clear(){for(const key of [...Object.values(this.binds).flat(),...Object.values(this.fallbackEmotions)])this.vrm.expressionManager?.setValue(key,0);}
 update(dt:number,blink=0){
  const elapsed=Math.max(0,dt);dt=Math.min(elapsed,.25);this.time+=elapsed;this.affect.update(elapsed);
  if(this.mouthAction&&this.time>=this.mouthAction.until)this.mouthAction=undefined;
  if(this.eyeAction&&this.time>=this.eyeAction.until)this.eyeAction=undefined;
  if(!this.enabled&&!this.mouthAction){this.clear();this.current={};return false;}
  let target:FaceWeights={...this.hold};
  if(this.mode==='playing'&&this.frames.length){
   this.cursor=Math.min(this.cursor+dt*24*this.speed,this.frames.length-1);
   const i=Math.floor(this.cursor),a=this.frames[i],b=this.frames[Math.min(i+1,this.frames.length-1)],mix=this.cursor-i;
   target=Object.fromEntries(Object.keys(a).map(k=>[k,a[k]+(b[k]-a[k])*mix]));
   if(this.done&&this.cursor>=this.frames.length-1){this.hold={...target};this.holdTime=0;this.mode='hold';}
  }else if(this.mode==='hold'){
   this.holdTime+=dt;
   const variation=1+.045*Math.sin(this.holdTime*.87)+.025*Math.sin(this.holdTime*1.43+.8);
   target=Object.fromEntries(Object.entries(this.hold).map(([k,v])=>[k,v*variation]));
  }
  if(this.guidance&&this.mode!=='releasing'&&this.mode!=='waiting'){
   const muscles=this.affect.weights(),strength=this.affect.current.intensity;
   this.modelDetail=Object.fromEntries(Object.keys(muscles).map(k=>[k,Math.max(-.14,Math.min(.14,((target[k]??0)-(this.reference[k]??0))/.015*.14))*strength]));
   target=Object.fromEntries(Object.entries(muscles).map(([k,base])=>[k,Math.max(0,base+(this.modelDetail[k]??0))]));
   const v=this.affect.current.valence;
   if(Object.values(this.affect.mixture).reduce((a,b)=>a+b,0)<.01){
    if(v<-.2)target.mouthSmile*=Math.max(0,1+v*2);
    if(v>.2){target.mouthFrown*=Math.max(0,1-v*2);target.browDown*=Math.max(0,1-v*2);}
   }
  }
  if(this.mode==='playing'){this.blendTime+=dt;const blend=Math.min(1,this.blendTime/(this.continuation ? .65 : .25));for(const k of Object.keys(target))target[k]=(this.start[k]??0)*(1-blend)+target[k]*blend;}
  if(this.mode==='releasing'){target={};if(Object.values(this.current).every(v=>v<.001)){this.reset();return false;}}
  if(this.mode==='waiting')return false;
  // The emotional mouth corners remain; articulation closes after the reply.
  const speaking=this.enabled&&this.time<this.speakingUntil;
  if(speaking){
   const phase=this.time*this.speechRate*2*Math.PI,beat=.5+.5*Math.sin(phase),pause=Math.sin(this.time*1.7)>.97?.15:1;
   target.mouthOpen=Math.min(.6,Math.max((target.mouthOpen??0)*.35,(.06+.48*beat)*pause));
   target.mouthPucker=(target.mouthPucker??0)*.4+.10*(.5+.5*Math.sin(phase*.69));
   target.mouthI=.10*(.5+.5*Math.sin(phase*.91+1));target.mouthO=.08*(.5+.5*Math.sin(phase*.8+2));
  }else{
   target.mouthOpen=0;target.mouthPucker=0;target.mouthI=0;target.mouthO=0;
  }
  if(!this.enabled)target={};
  if(this.mouthAction){target.mouthOpen=this.mouthAction.value;target.mouthPucker=0;target.mouthI=0;target.mouthO=0;}
  for(const key of Object.keys(names)){const alpha=1-Math.exp(-dt*(['mouthOpen','mouthPucker','mouthI','mouthO'].includes(key)?38:18));this.current[key]=(this.current[key]??0)+((target[key]??0)-(this.current[key]??0))*alpha;if(!speaking&&!this.mouthAction&&['mouthOpen','mouthPucker','mouthI','mouthO'].includes(key)&&(this.time>=this.speakingUntil+.12||this.current[key]<.001))this.current[key]=0;}
  const manager=this.vrm.expressionManager!;
  for(const [label,key] of Object.entries(this.fallbackEmotions))manager.setValue(key,!this.enabled||this.mode==='releasing'?0:clamp((Object.keys(this.affect.mixture).length?this.affect.mixture[label]??0:label===this.emotion?1:0)*this.affect.current.intensity*this.gain*.6));
  const calibration:FaceWeights=this.miku?{browInnerUp:2.3,browOuterUp:2,browDown:2.4,eyeSquint:3.5,mouthSmile:2.4,mouthFrown:2.2}:{};
  for(const [channel,keys]of Object.entries(this.binds))for(const key of keys)manager.setValue(key,clamp((this.current[channel]??0)*(channel==='mouthOpen'&&this.mouthAction?1:this.gain)*(calibration[channel]??1))*(channel==='eyeWide'||channel==='eyeSquint'?1-clamp(blink):1));
  return Object.values(this.binds).some(keys=>keys.length>0)||Object.keys(this.fallbackEmotions).length>0;
 }
 snapshot(){const action=this.mouthAction?{mouth_open:this.mouthAction.value,remaining_s:Math.max(0,this.mouthAction.until-this.time)}:null;const morphs=Object.fromEntries(Object.values(this.mapping).flat().map(name=>[name,this.rawTargets.get(name)?.map(({mesh,index})=>mesh.morphTargetInfluences?.[index]??0)]));return {enabled:this.enabled,eyelids:this.eyelids(0,0),eyeRemaining:this.eyeAction?Math.max(0,this.eyeAction.until-this.time):0,eyeBindings:Object.fromEntries(['blink','blinkLeft','blinkRight'].map(k=>[k,this.vrm.expressionManager?.getExpression(k)?.binds.flatMap(b=>b instanceof VRMExpressionMorphTargetBind?b.primitives.map(m=>m.morphTargetInfluences?.[b.index]??0):[])??[]])),action,id:this.id,mode:this.mode,cursor:this.cursor,frames:this.received,speaking:this.time<this.speakingUntil,speechRemaining:Math.max(0,this.speakingUntil-this.time),current:{...this.current},mapping:this.mapping,morphs,warnings:this.warnings,standardEmotions:Object.keys(this.fallbackEmotions),mouthSupported:!!this.binds.mouthOpen.length,profile:this.miku?"miku-local":"local",modelDetail:{...this.modelDetail},error:this.error,affect:this.affect.snapshot(),guidance:this.guidance,windows:this.windows};}
}
