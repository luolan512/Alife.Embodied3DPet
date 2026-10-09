import * as THREE from 'three';
import type {VRM,VRMHumanBoneName} from '@pixiv/three-vrm';

// HumanML3D/SMPL22 order. Coordinates remain right-handed Y-up metres.
export const observationSourceNames=['Hips','LeftUpLeg','RightUpLeg','Spine','LeftLeg','RightLeg','Spine2','LeftFoot','RightFoot','Spine3','LeftToeBase','RightToeBase','Neck','LeftShoulder','RightShoulder','Head','LeftArm','RightArm','LeftForeArm','RightForeArm','LeftHand','RightHand'];
const targetNames:VRMHumanBoneName[]=['hips','leftUpperLeg','rightUpperLeg','spine','leftLowerLeg','rightLowerLeg','chest','leftFoot','rightFoot','upperChest','leftToes','rightToes','neck','leftShoulder','rightShoulder','head','leftUpperArm','rightUpperArm','leftLowerArm','rightLowerArm','leftHand','rightHand'];
export function observedVRMJoints(vrm:VRM):{joints:number[][];estimated:string[]}|undefined{
  const estimated:string[]=[];
  const get=(name:VRMHumanBoneName)=>vrm.humanoid.getNormalizedBoneNode(name)?.getWorldPosition(new THREE.Vector3());
  const values=targetNames.map((name,i)=>{
    let point=get(name);
    if(!point&&name==='upperChest'){
      const chest=get('chest'),neck=get('neck');
      if(chest&&neck){point=chest.lerp(neck,.35);estimated.push(name);}
    }
    if(!point&&(i===10||i===11)){
      const foot=vrm.humanoid.getNormalizedBoneNode(i===10?'leftFoot':'rightFoot');
      if(foot){point=foot.getWorldPosition(new THREE.Vector3()).add(new THREE.Vector3(0,0,.12).applyQuaternion(foot.getWorldQuaternion(new THREE.Quaternion())));estimated.push(name);}
    }
    return point?.toArray();
  });
  if(values.some(v=>!v||!v.every(Number.isFinite)))return;
  return {joints:values as number[][],estimated};
}
