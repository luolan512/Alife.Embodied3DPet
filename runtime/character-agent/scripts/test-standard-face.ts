import assert from 'node:assert/strict';
import * as THREE from 'three';
import {VRMExpression,VRMExpressionManager,VRMExpressionMorphTargetBind} from '@pixiv/three-vrm';
import {GeneratedFace} from '../adapters/runtime-three/generated-face';
// No model-specific morph names: only the standardized VRM expressions know their bindings.
const mesh=new THREE.Mesh(new THREE.BufferGeometry(),new THREE.MeshBasicMaterial());mesh.morphTargetDictionary={shape0:0,shape1:1,shape2:2,shape3:3,shape4:4};mesh.morphTargetInfluences=[0,0,0,0,0];
const manager=new VRMExpressionManager();for(const [index,name] of ['aa','ih','ou','oh','happy'].entries()){
 const expression=new VRMExpression(name);expression.addBind(new VRMExpressionMorphTargetBind({primitives:[mesh],index,weight:1}));expression.isBinary=true;expression.overrideMouth='block';manager.registerExpression(expression);
}
const scene=new THREE.Group();scene.add(mesh);const face=new GeneratedFace({scene,expressionManager:manager} as any);
assert.equal(face.mapping.mouthOpen[0],'VRM:aa');assert(face.snapshot().mouthSupported);
face.packet({type:'face_state',emotion:'happy',version:1,valence:.8,arousal:.4,dominance:.1,intensity:.8,hold_s:30,decay_s:45,age_s:0,mixture:{}});
face.packet({type:'face_speech',id:1,duration_s:2});let peak=0;
for(let i=0;i<100;i++){face.update(.016);manager.update();peak=Math.max(peak,mesh.morphTargetInfluences![0]);}
assert(peak>.2,'standard mouth must reach actual mesh despite binary source');assert(mesh.morphTargetInfluences![4]>.2,'standard happy must display');
face.packet({type:'face_speech',id:1,duration_s:0});for(let i=0;i<20;i++){face.update(.016);manager.update();}
for(let i=0;i<4;i++)assert.equal(mesh.morphTargetInfluences![i],0,'all articulation must close');assert(mesh.morphTargetInfluences![4]>.2,'emotion persists');
const empty=new GeneratedFace({scene:new THREE.Group(),expressionManager:new VRMExpressionManager()} as any);assert(!empty.snapshot().mouthSupported);assert(empty.warnings.some(w=>w.includes('没有有效嘴型')));
face.packet({type:'face_action',id:1,mouth_open:1,hold_s:1});
for(let i=0;i<20;i++){face.update(.016);manager.update();}
assert(mesh.morphTargetInfluences![0]>.95,'deliberate wide mouth bypasses speech closure and emotional gain');
face.packet({type:'face_speech',id:2,duration_s:0});
face.update(.1);manager.update();assert(mesh.morphTargetInfluences![0]>.95,'reply end must not cancel deliberate mouth pose');
face.update(1);manager.update();assert.equal(mesh.morphTargetInfluences![0],0,'expired mouth pose returns to closed mouth');
face.packet({type:'face_config',enabled:false,gain:.65});
face.packet({type:'face_action',id:2,mouth_open:1,hold_s:2});
for(let i=0;i<20;i++){face.update(.016);manager.update();}
assert(mesh.morphTargetInfluences![0]>.95,'mouth pose works without CTEG');
face.packet({type:'face_action',id:3,mouth_open:0,hold_s:2});
for(let i=0;i<20;i++){face.update(.016);manager.update();}
assert(mesh.morphTargetInfluences![0]<.001,'explicit close replaces open pose');
face.packet({type:'face_action',id:2,mouth_open:1,hold_s:2});face.update(.1);manager.update();assert(mesh.morphTargetInfluences![0]<.001,'stale action cannot reopen mouth');
face.packet({type:'face_action',id:4,mouth_open:1,hold_s:2});face.packet({type:'face_reset',id:10});face.update(.1);manager.update();assert.equal(mesh.morphTargetInfluences![0],0,'reset cancels deliberate pose');
face.packet({type:'face_action',id:5,eyes_closed:1,hold_s:1});
assert.deepEqual(face.eyelids(0,.2),{left:1,right:1},'deliberate closure overrides automatic blinking without CTEG');
face.packet({type:'face_action',id:6,mouth_open:1,hold_s:2});
assert.deepEqual(face.eyelids(0,0),{left:1,right:1},'mouth intent preserves eye hold');
face.packet({type:'face_action',id:7,eyes_closed:0,hold_s:1});
assert.deepEqual(face.eyelids(1,1),{left:0,right:0},'open eyes suppress automatic blink while held');
face.update(1.1);assert.deepEqual(face.eyelids(.4,.6),{left:.4,right:.6},'natural blinking resumes after hold');
face.packet({type:'face_action',id:8,eyes_closed:1,hold_s:2});face.packet({type:'face_reset',id:11});assert.deepEqual(face.eyelids(0,0),{left:0,right:0},'reset clears eye pose');
console.log('PASS: standard VRM bindings with arbitrary morph names, binary mouth conversion, persistent emotion, exact closure, missing-binding warning.');
