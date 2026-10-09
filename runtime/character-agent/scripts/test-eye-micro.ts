import assert from 'node:assert/strict';
import * as THREE from 'three';
import {EyeMicro} from '../adapters/runtime-three/eye-micro';
let seed=42;const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/2**32;};
const eyes=[new THREE.Object3D(),new THREE.Object3D()];
const baseline=new THREE.Quaternion().setFromEuler(new THREE.Euler(.1,.2,0));
for(const e of eyes)e.quaternion.copy(baseline);
const micro=new EyeMicro({humanoid:{getRawBoneNode:(name:string)=>eyes[name==='leftEye'?0:1]}} as any,random);
let peak=0,largeMicro=false,lastGlance=0,glances=0;
for(let i=0;i<37500;i++){
 micro.beforeUpdate();assert(eyes.every(e=>e.quaternion.clone().normalize().angleTo(baseline)<1e-7),'relative rotation must not drift');
 micro.afterUpdate(.016,0);const s=micro.snapshot();peak=Math.max(peak,Math.abs(s.offsetDegrees[0]));
 if(s.glanceCount>glances){const now=(i+1)*.016;assert(now-lastGlance>=12&&now-lastGlance<=60.02,'glance intervals must be 12–60 seconds');lastGlance=now;glances=s.glanceCount;}
 if(!s.glancing&&Math.abs(s.offsetDegrees[0])>.7&&Math.abs(s.offsetDegrees[0])<1.8)largeMicro=true;
 assert(eyes[0].quaternion.equals(eyes[1].quaternion),'eyes move together');
 assert(Math.abs(s.offsetDegrees[0])<9.01&&Math.abs(s.offsetDegrees[1])<2.51,'bounded offsets');
}
assert(peak>4,'occasional side glance must be visible');assert(micro.snapshot().glanceCount>=10&&micro.snapshot().glanceCount<=50,'glances remain occasional');assert(largeMicro);
micro.beforeUpdate();micro.afterUpdate(.016,1);assert(eyes.every(e=>e.quaternion.clone().normalize().angleTo(baseline)<1e-7),'closed lids suppress motion');
micro.beforeUpdate();micro.enabled=false;micro.afterUpdate(.016,0);assert.deepEqual(micro.snapshot().offsetDegrees,[0,0]);assert(eyes.every(e=>e.quaternion.clone().normalize().angleTo(baseline)<1e-7));
const unsupported=new EyeMicro({humanoid:{getRawBoneNode:()=>null}} as any,random);unsupported.afterUpdate(.016,0);assert(!unsupported.supported);
console.log('PASS: visible random glances, bounded conjugate offsets, no drift over 600s, closed-eye suppression, disable and missing eyes.');
