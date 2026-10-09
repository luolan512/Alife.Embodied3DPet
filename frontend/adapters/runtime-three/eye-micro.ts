import * as THREE from 'three';
import type {VRM} from '@pixiv/three-vrm';

/** Conjugate micro movements and occasional glances, relative to current gaze. */
export class EyeMicro {
  enabled=true;
  private eyes:THREE.Object3D[];
  private applied=new THREE.Quaternion();
  private angle=new THREE.Vector2();
  private target=new THREE.Vector2();
  private until=0;
  private time=0;
  private eventAge=0;
  private appliedLast=false;
  private glanceIn:number;
  private glanceUntil=0;
  private returningUntil=0;
  private glanceCount=0;
  supported:boolean;
  constructor(vrm:VRM,private random= Math.random){
    this.glanceIn=12+this.random()*48;
    this.eyes=['leftEye','rightEye'].map(name=>vrm.humanoid.getRawBoneNode(name as 'leftEye'|'rightEye')).filter((eye):eye is THREE.Object3D=>!!eye);
    this.supported=this.eyes.length===2;
  }
  beforeUpdate(){
    if(this.appliedLast){const inverse=this.applied.clone().invert();for(const eye of this.eyes)eye.quaternion.multiply(inverse);this.appliedLast=false;}
  }
  afterUpdate(dt:number,blink:number){
    if(!this.enabled||!this.supported){this.angle.set(0,0);this.target.set(0,0);this.glanceUntil=0;this.returningUntil=0;return;}
    const elapsed=Math.max(0,dt);dt=Math.min(.05,elapsed);this.time+=elapsed;this.until-=elapsed;this.eventAge+=elapsed;this.glanceIn-=elapsed;
    if(this.glanceUntil&&this.time>=this.glanceUntil){
      this.glanceUntil=0;this.returningUntil=this.time+.24;this.target.set(0,0);this.eventAge=0;
    }
    if(!this.glanceUntil&&this.time>=this.returningUntil&&this.glanceIn<=0&&blink<.5){
      // Briefly look sideways, then return to the existing head/user gaze.
      const side=this.random()<.5?-1:1;
      this.target.set(THREE.MathUtils.degToRad(side*(4+this.random()*5)),THREE.MathUtils.degToRad((this.random()*2-1)*2.5));
      this.glanceUntil=this.time+.35+this.random()*.55;this.glanceIn=12+this.random()*48;this.eventAge=0;this.glanceCount++;
    }else if(!this.glanceUntil&&this.time>=this.returningUntil&&this.until<=0){
      this.until=.16+this.random()*.24;this.eventAge=0;
      // Mostly subtle; occasional larger adjustments make the eyes readable.
      const direction=this.random()*Math.PI*2,amplitude=THREE.MathUtils.degToRad(this.random()<.15?.8+this.random():.08+.42*this.random());
      this.target.set(Math.cos(direction)*amplitude,Math.sin(direction)*amplitude*.65);
    }
    const settle=1-Math.exp(-dt*(this.glanceUntil?24:this.time<this.returningUntil?18:this.eventAge<.08?55:9));this.angle.lerp(this.target,settle);
    const visibility=1-THREE.MathUtils.clamp(blink,0,1);
    const tremor=THREE.MathUtils.degToRad(.008)*visibility;
    this.applied.setFromEuler(new THREE.Euler((this.angle.y+Math.sin(this.time*31)*tremor)*visibility,(this.angle.x+Math.sin(this.time*37)*tremor)*visibility,0,'YXZ'));
    for(const eye of this.eyes)eye.quaternion.multiply(this.applied);
    this.appliedLast=true;
  }
  snapshot(){return {enabled:this.enabled,supported:this.supported,intervalSeconds:[.16,.4],amplitudeDegrees:1.8,microAmplitudeDegrees:[.08,.5],largerAdjustmentProbability:.15,glanceAmplitudeDegrees:[4,9],glanceIntervalSeconds:[12,60],glanceHoldSeconds:[.35,.9],glancing:this.glanceUntil>this.time,glanceCount:this.glanceCount,tremorDegrees:.008,offsetDegrees:[THREE.MathUtils.radToDeg(this.angle.x),THREE.MathUtils.radToDeg(this.angle.y)]};}
}
