import type {FaceWeights} from './generated-face';
const clamp=(v:number,lo=0,hi=1)=>Math.max(lo,Math.min(hi,v));
/** Continuous local muscle control; deliberately independent of CTEG's random baseline. */
export class Affect {
 target={valence:0,arousal:0,dominance:0,intensity:0,hold_s:8,decay_s:45};
 current={valence:0,arousal:0,dominance:0,intensity:0};age=0;version=0;
 mixture:Record<string,number>={};private mixTarget:Record<string,number>={};
 packet(p:any){
  if(p.version<this.version)return;
  const keys=Object.keys(this.target) as (keyof typeof this.target)[];
  if(keys.some(k=>typeof p[k]!=='number'||!Number.isFinite(p[k])))return;
  this.target={valence:clamp(p.valence,-1,1),arousal:clamp(p.arousal,-1,1),dominance:clamp(p.dominance,-1,1),intensity:clamp(p.intensity),hold_s:clamp(p.hold_s,0,120),decay_s:clamp(p.decay_s,2,300)};
  this.age=Math.max(0,p.age_s??0);this.version=p.version;
  const raw=p.mixture??{},labels=['neutral','happy','sad','angry','relaxed'];
  if(typeof raw==='object'&&Object.entries(raw).every(([k,v])=>labels.includes(k)&&typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=1))this.mixTarget={...raw};
 }
 update(dt:number){
  this.age+=dt;const strength=this.target.intensity*Math.exp(-Math.max(0,this.age-this.target.hold_s)/this.target.decay_s);
  const alpha=1-Math.exp(-Math.min(dt,.25)*4);
  // Smooth the vector itself, not just the displayed preset label.
  for(const k of ['valence','arousal','dominance','intensity'] as const)this.current[k]+=((k==='intensity'?strength:this.target[k])-this.current[k])*alpha;
  for(const k of new Set([...Object.keys(this.mixture),...Object.keys(this.mixTarget)]))this.mixture[k]=(this.mixture[k]??0)+((this.mixTarget[k]??0)-(this.mixture[k]??0))*alpha;
 }
 weights():FaceWeights{
  const muscles=(v:number,a:number,d:number)=>{
  const positive=Math.max(0,v),negative=Math.max(0,-v),activation=(a+1)/2,power=Math.max(0,d);
  const anger=negative*activation*(.25+.75*power);
  const base={browInnerUp:.6*negative*(1-activation)*(1-.6*power)+.15*positive*activation,browOuterUp:.18*activation*Math.abs(v),browDown:.7*anger,eyeWide:.2*activation**3,eyeSquint:.15*positive*activation+.3*anger,mouthSmile:.65*positive*(.6+.4*activation),mouthFrown:.42*negative*(1-.25*activation*power),mouthOpen:0,mouthPucker:0};
   return base;
  };
  const base=muscles(this.current.valence,this.current.arousal,this.current.dominance);
  const vectors:Record<string,number[]>={neutral:[0,.05,0],happy:[.85,.55,.25],sad:[-.75,-.5,-.5],angry:[-.8,.8,.6],relaxed:[.45,-.65,.05]};
  const mixTotal=Math.min(1,Object.values(this.mixture).reduce((a,b)=>a+b,0));
  const mixed:FaceWeights=Object.fromEntries(Object.entries(base).map(([k,v])=>[k,v*(1-mixTotal)]));
  for(const [label,weight]of Object.entries(this.mixture)){const vector=vectors[label];if(!vector)continue;for(const [k,value]of Object.entries(muscles(vector[0],vector[1],vector[2])))mixed[k]+=value*weight;}
  // Make moderate muscle intent readable while keeping intensity and decay linear.
  return Object.fromEntries(Object.entries(mixed).map(([k,value])=>[k,clamp(Math.pow(Math.max(0,value),.8)*this.current.intensity,0,.8)]));
 }
 snapshot(){return {target:this.target,current:{...this.current},age_s:this.age,version:this.version,mixture:{...this.mixture}};}
}
