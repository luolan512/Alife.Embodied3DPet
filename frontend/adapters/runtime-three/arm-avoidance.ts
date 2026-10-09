import * as THREE from 'three';
import type {VRM,VRMHumanBoneName} from '@pixiv/three-vrm';

export function closestSegmentPoint(point:THREE.Vector3,a:THREE.Vector3,b:THREE.Vector3){
 const axis=b.clone().sub(a),length=axis.lengthSq();
 return a.clone().addScaledVector(axis,length>1e-10?THREE.MathUtils.clamp(point.clone().sub(a).dot(axis)/length,0,1):0);
}

/** A bounded VRM display correction. No impulses, mesh collisions or ARDY feedback. */
export class ArmAvoidance {
 private report={active:false,corrections:0,max_degrees:0,remaining_penetration_m:0};
 constructor(private vrm:VRM){}
 diagnostics(){return {...this.report};}
 apply(enabled:boolean){
  this.report={active:false,corrections:0,max_degrees:0,remaining_penetration_m:0};
  if(!enabled)return;
  const bone=(name:VRMHumanBoneName)=>this.vrm.humanoid.getNormalizedBoneNode(name);
  const hips=bone('hips'),neck=bone('neck'),left=bone('leftUpperArm'),right=bone('rightUpperArm');
  if(!hips||!neck||!left||!right)return;
  this.vrm.scene.updateMatrixWorld(true);
  const a=hips.getWorldPosition(new THREE.Vector3()),b=neck.getWorldPosition(new THREE.Vector3());
  const axis=b.clone().sub(a),length=axis.length();if(length<.1)return;
  // Calibrate to the loaded avatar rather than using source skeleton proportions.
  const width=left.getWorldPosition(new THREE.Vector3()).distanceTo(right.getWorldPosition(new THREE.Vector3()));
  const radius=THREE.MathUtils.clamp(width*.28,length*.14,length*.28);
  const armRadius=radius*.22,margin=radius*.055;
  const lateral=right.getWorldPosition(new THREE.Vector3()).sub(left.getWorldPosition(new THREE.Vector3())).normalize();
  const front=new THREE.Vector3().crossVectors(lateral,axis.clone().normalize()).normalize();
  const originals=new Map<THREE.Object3D,THREE.Quaternion>();
  // Use distal forearm points; shoulder and upper-arm attachment naturally touch the torso.
  for(const side of ['left','right'] as const){
   const upper=bone(`${side}UpperArm`),lower=bone(`${side}LowerArm`),hand=bone(`${side}Hand`);
   if(!upper||!lower||!hand)continue;
   originals.set(upper,upper.quaternion.clone());originals.set(lower,lower.quaternion.clone());
   for(let iteration=0;iteration<3;iteration++){
    let deepest=0,target:THREE.Vector3|undefined,point:THREE.Vector3|undefined;
    const elbow=lower.getWorldPosition(new THREE.Vector3()),end=hand.getWorldPosition(new THREE.Vector3());
    for(const t of [.35,.7,1]){
     const sample=elbow.clone().lerp(end,t),center=closestSegmentPoint(sample,a,b);
     const radial=sample.clone().sub(center),distance=radial.length();
     const depth=radius+armRadius-distance-margin;
     if(depth<=deepest)continue;
     // Resolve a deep intersection, but leave ordinary surface contact untouched.
     const outward=distance>.005?radial.divideScalar(distance):front.clone();
     deepest=depth;point=sample;target=sample.clone().addScaledVector(outward,depth*.8);
    }
    if(!point||!target)break;
    // Rotate the upper arm, keeping segment lengths and the original elbow bend intact.
    // Every frame starts from ARDY's mapped pose; this correction cannot accumulate.
    const pivot=upper.getWorldPosition(new THREE.Vector3());
    const from=point.clone().sub(pivot).normalize(),to=target.clone().sub(pivot).normalize();
    const delta=new THREE.Quaternion().setFromUnitVectors(from,to);
    const desired=upper.getWorldQuaternion(new THREE.Quaternion()).premultiply(delta);
    const parent=upper.parent?.getWorldQuaternion(new THREE.Quaternion())||new THREE.Quaternion();
    const local=parent.invert().multiply(desired).normalize();
    const baseline=originals.get(upper)!,angle=baseline.angleTo(local),limit=THREE.MathUtils.degToRad(12);
    if(angle>limit){const proposed=local.clone();local.copy(baseline).slerp(proposed,limit/angle);}
    upper.quaternion.copy(local);upper.updateMatrixWorld(true);
    this.report.corrections++;this.report.max_degrees=Math.max(this.report.max_degrees,THREE.MathUtils.radToDeg(baseline.angleTo(local)));
   }
   const elbow=lower.getWorldPosition(new THREE.Vector3()),end=hand.getWorldPosition(new THREE.Vector3());
   for(const t of [.35,.7,1]){
    const sample=elbow.clone().lerp(end,t);
    this.report.remaining_penetration_m=Math.max(this.report.remaining_penetration_m,Math.max(0,radius+armRadius-margin-sample.distanceTo(closestSegmentPoint(sample,a,b))));
   }
  }
  this.report.active=this.report.corrections>0;
 }
}
