import assert from 'node:assert/strict';
import * as THREE from 'three';
import {VRMExpressionManager} from '@pixiv/three-vrm';
import {GeneratedFace} from '../adapters/runtime-three/generated-face';
function render(intensity:number,gain=1){
 const mesh=new THREE.Mesh(new THREE.BufferGeometry(),new THREE.MeshBasicMaterial());
 mesh.morphTargetDictionary={Fcl_MTH_Fun:0,Fcl_BRW_Sorrow:1,Fcl_EYE_Angry:2,Fcl_MTH_A:3};mesh.morphTargetInfluences=[0,0,0,0];
 const scene=new THREE.Group();scene.add(mesh);const manager=new VRMExpressionManager();const face=new GeneratedFace({scene,expressionManager:manager} as any);
 face.gain=gain;face.packet({type:'face_state',emotion:'happy',version:1,valence:.6,arousal:.3,dominance:.1,intensity,hold_s:30,decay_s:45,age_s:0,mixture:{}});
 for(let i=0;i<120;i++){face.update(.016);manager.update();}
 return mesh.morphTargetInfluences;
}
const quiet=render(.2),ordinary=render(.6),strong=render(1),off=render(.6,0);
assert(ordinary[0]>.24&&ordinary[0]<.5,'ordinary emotion must make a visible smile without saturation');
assert(quiet[0]<ordinary[0]&&ordinary[0]<strong[0]&&strong[0]<1,'emotion levels retain distinct amplitude');
assert(Math.abs(ordinary[0]/quiet[0]-3)<.01,'intensity remains linear');
assert.equal(ordinary[3],0,'emotional smile must not open mouth');assert(off.every(v=>v===0),'zero gain disables expression');
console.log('PASS: ordinary emotional intent is visible, strength remains layered, zero gain and silent mouth closure preserved.');
