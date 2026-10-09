import * as THREE from 'three';
import type { VRM, VRMHumanBoneName } from '@pixiv/three-vrm';
import type { MotionPacket, SkeletonMetadata } from '../../packages/contracts/motion';
import {ArmAvoidance} from './arm-avoidance';
import {SkeletonOneEuro} from './one-euro';
import {observedVRMJoints,observationSourceNames} from './motion-observation';

const mapping: Record<string, VRMHumanBoneName> = {
  Hips:'hips', Spine:'spine', Spine2:'chest', Spine3:'upperChest', Neck:'neck', Head:'head',
  RightShoulder:'rightShoulder',RightArm:'rightUpperArm',RightForeArm:'rightLowerArm',RightHand:'rightHand',
  LeftShoulder:'leftShoulder',LeftArm:'leftUpperArm',LeftForeArm:'leftLowerArm',LeftHand:'leftHand',
  RightUpLeg:'rightUpperLeg',RightLeg:'rightLowerLeg',RightFoot:'rightFoot',RightToeBase:'rightToes',
  LeftUpLeg:'leftUpperLeg',LeftLeg:'leftLowerLeg',LeftFoot:'leftFoot',LeftToeBase:'leftToes',
};
type Frame={root:number[];q:number[][];joints?:number[][];prompt?:string};

/** Generic ARDY stream playback; VRM rest calibration lives exclusively in this adapter. */
export class VRMMotionPlayer {
  private frames=new Map<number,Frame>();
  private rest=new Map<string,THREE.Quaternion>();
  private correction=new THREE.Quaternion();
  private correctionInverse=new THREE.Quaternion();
  private rotation=new THREE.Quaternion();
  private desired=new THREE.Quaternion();
  private parent=new THREE.Quaternion();
  private rootInitialY:number|undefined;
  private targetInitialHipY=0;
  private scale=1;
  private floorY:number;
  private bindings:{source:number;bone:VRMHumanBoneName;node:THREE.Object3D}[]=[];
  skeleton?:SkeletonMetadata;
  epoch=0;cursor=0;end=-1;playing=false;lookAt=false;received=0;applied=0;
  private rootOffset=new THREE.Vector3();
  private mappedRoot=new THREE.Vector3(0,0,1.35);
  private boundaryLimited=false;
  private rootPlacements=new Map<number,THREE.Vector3>();
  private restorePlacement?:THREE.Vector3;
  waiting=false;
  speed=1;
  desktopMode=false;
  lockHeading=true;
  avoidBodyPenetration=true;
  private smoothing=new SkeletonOneEuro();
  private smoothingEnabled=true;
  private smoothingIdentity='';
  get oneEuroEnabled(){return this.smoothingEnabled;}
  set oneEuroEnabled(value:boolean){if(value!==this.smoothingEnabled){this.smoothingEnabled=value;this.smoothing.reset();this.resetObservationTape();}}
  private armAvoidance:ArmAvoidance;
  private desktopYaw=0;
  private desktopCorrection=new THREE.Quaternion();
  private headingWeight=1;
  private lastSourceYaw?:number;
  private motionRelease=0;
  private gazeWeight=0;
  /** Procedural orientation layer for purposeful looking; root trajectory remains ARDY's. */
  headingOffset=0;
  lookTarget=new THREE.Vector3(0,1.35,5.5);
  private gazeYaw=0;
  private gazePitch=0;
  private gazeFeedback={active:false,yaw_offset_degrees:0,pitch_offset_degrees:0,limited:false};
  sourceJoints:number[][]=[];
  private rawObservation:number[][]=[];
  private retargetObservation:number[][]=[];
  private observationPrompt='';
  private observationFrame=-1;
  private observationEpoch=-1;
  private observationGeneration=0;
  private observationTape:Record<string,unknown>[]=[];
  private lateral=new THREE.Vector3();
  constructor(private vrm:VRM) {
    this.armAvoidance=new ArmAvoidance(vrm);
    vrm.scene.updateMatrixWorld(true);
    this.floorY=vrm.scene.position.y;
    for(const [name,bone] of Object.entries(mapping)){
      const node=vrm.humanoid.getNormalizedBoneNode(bone==='upperChest'&&!vrm.humanoid.getNormalizedBoneNode(bone)?'chest':bone);
      if(node)this.rest.set(name,node.getWorldQuaternion(new THREE.Quaternion()));
    }
    const left=vrm.humanoid.getNormalizedBoneNode('leftUpperArm');
    const right=vrm.humanoid.getNormalizedBoneNode('rightUpperArm');
    if(left&&right)this.lateral.copy(left.getWorldPosition(new THREE.Vector3())).sub(right.getWorldPosition(new THREE.Vector3()));
    this.correctionInverse.copy(this.correction).invert();
  }
  setSkeleton(meta:SkeletonMetadata){
    this.smoothing.reset();
    this.skeleton=meta;
    const sourceL=meta.rest_positions[meta.names.indexOf('LeftArm')];
    const sourceR=meta.rest_positions[meta.names.indexOf('RightArm')];
    if(sourceL&&sourceR){
      this.correction.setFromAxisAngle(new THREE.Vector3(0,1,0),(sourceL[0]-sourceR[0])*this.lateral.x<0?Math.PI:0);
      this.correctionInverse.copy(this.correction).invert();
    }
    this.bindings=[];
    for(let i=0;i<meta.names.length;i++){
      let bone=mapping[meta.names[i]];
      if(meta.names[i]==='Spine3'&&!this.vrm.humanoid.getNormalizedBoneNode('upperChest'))bone='chest';
      const node=bone&&this.vrm.humanoid.getNormalizedBoneNode(bone);
      if(node){
        if(!this.rest.has(meta.names[i]))this.rest.set(meta.names[i],node.getWorldQuaternion(new THREE.Quaternion()));
        // A missing upperChest collapses the extra source spine segment into chest.
        this.bindings=this.bindings.filter(b=>b.node!==node);
        this.bindings.push({source:i,bone,node});
      }
    }
  }
  accept(packet:MotionPacket){
    if(packet.epoch<this.epoch)return;
    if(packet.roots.length!==packet.rotations.length)return;
    this.epoch=packet.epoch;
    for(const i of this.frames.keys())if(i>=packet.start)this.frames.delete(i);
    packet.roots.forEach((root,i)=>this.frames.set(packet.start+i,{root,q:packet.rotations[i],joints:packet.joints?.[i],prompt:packet.prompt}));
    this.end=packet.start+packet.roots.length-1;
    if(this.received===0)this.cursor=packet.cursor??packet.start;
    this.received+=packet.roots.length;
    for(const i of this.frames.keys())if(i<this.cursor-40)this.frames.delete(i);
  }
  play(playing:boolean,epoch:number){if(epoch<this.epoch)return;this.epoch=epoch;this.playing=playing;}
  trimAfter(frame:number,epoch:number){
    if(epoch<this.epoch)return;
    this.epoch=epoch;this.playing=false;this.resetObservationTape();
    for(const i of this.frames.keys())if(i>frame)this.frames.delete(i);
    for(const i of this.rootPlacements.keys())if(i>frame)this.rootPlacements.delete(i);
    this.end=frame;this.cursor=Math.max(0,Math.min(this.cursor,frame));this.waiting=false;
  }
  seek(frame:number){this.cursor=THREE.MathUtils.clamp(frame,this.start,this.end);this.playing=false;this.restorePlacement=this.rootPlacements.get(Math.floor(this.cursor))?.clone();this.resetObservationTape();}
  recenter(){this.rootOffset.set(-this.mappedRoot.x,0,1.35-this.mappedRoot.z);this.rootPlacements.clear();this.restorePlacement=undefined;}
  get start(){return this.frames.size?Math.min(...this.frames.keys()):0;}
  mappingDiagnostics(){return {calibration:'normalized T-pose',yaw_correction_degrees:Math.round(new THREE.Euler().setFromQuaternion(this.correction).y*180/Math.PI),
    bindings:this.bindings.map(b=>({source:this.skeleton?.names[b.source],target:b.bone}))};}
  update(dt:number){
    if(!this.skeleton||this.end<0)return;
    if(this.playing)this.cursor=Math.min(this.end,this.cursor+dt*this.skeleton.fps*this.speed);
    this.waiting=this.playing&&this.end-this.cursor<1;
    const index=Math.floor(this.cursor),a=this.frames.get(index),b=this.frames.get(index+1)||a;
    if(!a||!b)return;
    const alpha=this.cursor-index;
    const root=new THREE.Vector3().fromArray(a.root).lerp(new THREE.Vector3().fromArray(b.root),alpha);
    this.desktopCorrection.identity();
    if(this.desktopMode&&this.lockHeading){
      const hips=this.skeleton.names.indexOf('Hips');
      if(hips>=0&&a.q[hips]&&b.q[hips]){
        const q=new THREE.Quaternion().fromArray(a.q[hips]).slerp(new THREE.Quaternion().fromArray(b.q[hips]),alpha);
        q.premultiply(this.correction).multiply(this.correctionInverse);
        const forward=new THREE.Vector3(0,0,1).applyQuaternion(q);
        if(Math.hypot(forward.x,forward.z)>.15)this.desktopYaw=Math.atan2(forward.x,forward.z);
        const delta=this.lastSourceYaw===undefined?0:Math.atan2(Math.sin(this.desktopYaw-this.lastSourceYaw),Math.cos(this.desktopYaw-this.lastSourceYaw));
        this.lastSourceYaw=this.desktopYaw;
        if(Math.abs(this.desktopYaw)>.75||Math.abs(delta)/Math.max(dt,.001)>1.1||Math.abs(forward.y)>.6)this.motionRelease=1.2;
        else this.motionRelease=Math.max(0,this.motionRelease-dt);
        this.headingWeight=THREE.MathUtils.damp(this.headingWeight,this.motionRelease>0?0:1,5,dt);
        this.desktopCorrection.setFromAxisAngle(new THREE.Vector3(0,1,0),-this.desktopYaw*this.headingWeight);
      }
    }
    if(this.rootInitialY===undefined){
      this.rootInitialY=root.y;
      const hip=this.vrm.humanoid.getNormalizedBoneNode('hips');
      const y=hip?.getWorldPosition(new THREE.Vector3()).y||.9;
      this.targetInitialHipY=y;
      this.scale=Math.max(.5,Math.min(2,y/Math.max(.5,root.y)));
    }
    const displacement=new THREE.Vector3(root.x,0,root.z-1.35).applyQuaternion(this.correction).multiplyScalar(this.scale);
    this.mappedRoot.set(displacement.x,0,1.35+displacement.z);
    if(this.restorePlacement){this.rootOffset.copy(this.restorePlacement).sub(this.mappedRoot);this.rootOffset.y=0;this.restorePlacement=undefined;}
    const candidate=this.mappedRoot.clone().add(this.rootOffset);
    const x=this.desktopMode?0:THREE.MathUtils.clamp(candidate.x,-3.2,3.2),z=this.desktopMode?1.35:THREE.MathUtils.clamp(candidate.z,-3.2,3.2);
    this.boundaryLimited=!this.desktopMode&&(x!==candidate.x||z!==candidate.z);
    // Rebase the mapping at contact: an inward source delta immediately moves inward,
    // even after the model's unbounded trajectory has travelled far outside the room.
    if(!this.desktopMode){this.rootOffset.x+=x-candidate.x;this.rootOffset.z+=z-candidate.z;}
    this.vrm.scene.position.set(x,this.floorY+(root.y-this.rootInitialY)*this.scale,z);
    this.rootPlacements.set(index,new THREE.Vector3(x,0,z));
    for(const i of this.rootPlacements.keys())if(i<this.start)this.rootPlacements.delete(i);
    this.vrm.scene.updateMatrixWorld(true);
    if(a.joints)this.sourceJoints=a.joints.map((point,i)=>{
      const p=new THREE.Vector3().fromArray(point).lerp(new THREE.Vector3().fromArray(b.joints?.[i]??point),alpha);
      if(this.desktopMode){
        const local=p.sub(root).applyQuaternion(this.correction).applyQuaternion(this.desktopCorrection).multiplyScalar(this.scale);
        return [local.x+x,this.targetInitialHipY+(root.y-this.rootInitialY!)*this.scale+local.y,z+local.z];
      }
      const v=new THREE.Vector3(p.x,0,p.z-1.35).applyQuaternion(this.correction).multiplyScalar(this.scale);
      return [v.x+this.rootOffset.x,this.targetInitialHipY+(p.y-this.rootInitialY!)*this.scale,1.35+v.z+this.rootOffset.z];
    });
    this.rawObservation=observationSourceNames.map(name=>{
      const i=this.skeleton!.names.indexOf(name),p=a.joints?.[i],q=b.joints?.[i]??p;
      return p&&q?new THREE.Vector3().fromArray(p).lerp(new THREE.Vector3().fromArray(q),alpha).toArray():[];
    });
    this.observationPrompt=a.prompt??'';
    const smoothingIdentity=JSON.stringify([this.epoch,this.observationPrompt]);
    if(smoothingIdentity!==this.smoothingIdentity){this.smoothing.reset();this.smoothingIdentity=smoothingIdentity;}
    for(const binding of this.bindings){
      const qa=a.q[binding.source],qb=b.q[binding.source];if(!qa||!qb)continue;
      this.rotation.fromArray(qa).slerp(new THREE.Quaternion().fromArray(qb),alpha).normalize();
      this.desired.copy(this.correction).multiply(this.rotation).multiply(this.correctionInverse)
        .multiply(this.rest.get(this.skeleton.names[binding.source])!);
      if(this.desktopMode)this.desired.premultiply(this.desktopCorrection);
      binding.node.parent?.getWorldQuaternion(this.parent);
      binding.node.quaternion.copy(this.parent.invert()).multiply(this.desired).normalize();
      binding.node.updateMatrixWorld(true);
    }
    this.retargetObservation=observedVRMJoints(this.vrm)?.joints??[];
    this.armAvoidance.apply(this.desktopMode&&this.avoidBodyPenetration);
    const head=this.vrm.humanoid.getNormalizedBoneNode('head');
    const headForward=new THREE.Vector3().fromArray(this.facing('head'));
    const bodyForward=new THREE.Vector3().fromArray(this.facing('hips'));
    const headTurn=Math.acos(THREE.MathUtils.clamp(headForward.dot(bodyForward),-1,1));
    const allowed=this.desktopMode&&this.lookAt&&this.motionRelease<=0&&headTurn<.65&&bodyForward.z>.75;
    this.gazeWeight=THREE.MathUtils.damp(this.gazeWeight,allowed?1:0,5,dt);
    if(head&&this.gazeWeight>.001){
      const direction=this.lookTarget.clone().sub(head.getWorldPosition(new THREE.Vector3())).normalize();
      const angle=headForward.angleTo(direction);
      const correction=new THREE.Quaternion().setFromUnitVectors(headForward,direction);
      const amount=this.gazeWeight*Math.min(1,.5/Math.max(angle,.001));
      const blended=new THREE.Quaternion().slerp(correction,amount);
      const desired=head.getWorldQuaternion(new THREE.Quaternion()).premultiply(blended);
      const parent=head.parent!.getWorldQuaternion(new THREE.Quaternion()).invert();
      head.quaternion.copy(parent.multiply(desired));head.updateMatrixWorld(true);
    }
    const corrected=new THREE.Vector3().fromArray(this.facing('head'));
    const yawDelta=Math.atan2(corrected.x,corrected.z)-Math.atan2(headForward.x,headForward.z);
    this.gazeFeedback={active:this.gazeWeight>.01,
      yaw_offset_degrees:THREE.MathUtils.radToDeg(Math.atan2(Math.sin(yawDelta),Math.cos(yawDelta))),
      pitch_offset_degrees:THREE.MathUtils.radToDeg(Math.asin(THREE.MathUtils.clamp(corrected.y,-1,1))-Math.asin(THREE.MathUtils.clamp(headForward.y,-1,1))),
      limited:this.lookAt&&!allowed};
    if(this.smoothingEnabled)this.smoothing.apply(this.vrm.scene,this.bindings.map(binding=>binding.node),this.cursor,dt);
    this.applied++;
  }
  facing(bone:'hips'|'head'='hips'){
    const node=this.vrm.humanoid.getNormalizedBoneNode(bone),rest=this.rest.get(bone==='hips'?'Hips':'Head');
    if(!node||!rest)return [0,0,1];
    const delta=node.getWorldQuaternion(new THREE.Quaternion()).multiply(rest.clone().invert());
    const forward=new THREE.Vector3(0,0,1).applyQuaternion(delta);
    if(bone==='hips'){forward.y=0;if(forward.lengthSq()<.001)return [0,0,1];}
    return forward.normalize().toArray();
  }
  feedback(){
    const hip=this.vrm.humanoid.getNormalizedBoneNode('hips');
    const p=hip?.getWorldPosition(new THREE.Vector3())||this.vrm.scene.position;
    return {frame:Math.floor(this.cursor),position:[this.vrm.scene.position.x,p.y,this.vrm.scene.position.z],
      buffered_seconds:Math.max(0,this.end-this.cursor)/(this.skeleton?.fps||20),waiting:this.waiting,
      applied:this.applied,received:this.received,epoch:this.epoch,retained_frames:this.frames.size,
      one_euro:{enabled:this.smoothingEnabled,min_cutoff_hz:1,beta:.5,derivative_cutoff_hz:1},
      root_mapping:{source_mapped:this.mappedRoot.toArray(),offset:this.rootOffset.toArray(),boundary_limited:this.boundaryLimited}};
  }
  captureObservation(){
    if(!this.skeleton||this.rawObservation.length!==22||this.rawObservation.some(p=>p.length!==3))return;
    if(this.observationEpoch!==this.epoch){this.observationTape=[];this.observationFrame=-1;this.observationEpoch=this.epoch;}
    // One sample per source frame. Pause/repaint never invents performed motion.
    const frame=Math.floor(this.cursor);
    if(frame<=this.observationFrame)return;
    const final=observedVRMJoints(this.vrm);if(!final)return;
    this.observationFrame=frame;
    this.observationTape.push({frame,epoch:this.epoch,fps:this.skeleton.fps,prompt:this.observationPrompt,playback_generation:this.observationGeneration,
      source:this.rawObservation,retarget:this.retargetObservation,rendered:final.joints,
      estimated_joints:final.estimated,body_avoidance:this.armAvoidance.diagnostics()});
    if(this.observationTape.length>32)this.observationTape.shift();
  }
  private resetObservationTape(){this.smoothing.reset();this.observationGeneration++;this.observationTape=[];this.observationFrame=-1;}
  drainObservations(){const samples=this.observationTape;this.observationTape=[];return samples;}
  gazeDiagnostics(){return {...this.gazeFeedback,target:this.lookTarget.toArray()};}
  collisionDiagnostics(){return this.armAvoidance.diagnostics();}
}
