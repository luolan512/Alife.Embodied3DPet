import * as THREE from 'three';
import type {VRM,VRMHumanBoneName} from '@pixiv/three-vrm';

// Body proxies only: hair/clothing cannot enlarge the contact footprint.
const parts:[VRMHumanBoneName,number][]=[['hips',.13],['spine',.11],['chest',.13],['head',.1],
  ['leftUpperArm',.065],['rightUpperArm',.065],['leftLowerArm',.045],['rightLowerArm',.045],
  ['leftHand',.05],['rightHand',.05],['leftUpperLeg',.09],['rightUpperLeg',.09],
  ['leftLowerLeg',.065],['rightLowerLeg',.065],['leftFoot',.075],['rightFoot',.075],
  ['leftToes',.055],['rightToes',.055]];

export function contactFootprint(points:{position:THREE.Vector3;radius:number}[],floorY=0){
  const finite=points.filter(p=>p.position.toArray().every(Number.isFinite));
  if(!finite.length)return undefined;
  const gap=Math.min(...finite.map(p=>Math.max(0,p.position.y-floorY-p.radius)));
  const near=finite.filter(p=>p.position.y-floorY-p.radius<=gap+.22);
  const minX=Math.min(...near.map(p=>p.position.x-p.radius));
  const maxX=Math.max(...near.map(p=>p.position.x+p.radius));
  const minZ=Math.min(...near.map(p=>p.position.z-p.radius));
  const maxZ=Math.max(...near.map(p=>p.position.z+p.radius));
  const softness=.09+Math.min(.35,gap*.3);
  return {x:(minX+maxX)/2,z:(minZ+maxZ)/2,width:Math.max(.22,maxX-minX)+softness*2,
    depth:Math.max(.18,maxZ-minZ)+softness*2,opacity:.28*Math.exp(-gap*4),gap};
}

/** Lightweight visual contact cue on the world floor; no pose or physics corrections. */
export class ContactShadow {
  private material=new THREE.ShaderMaterial({transparent:true,depthWrite:false,
    uniforms:{opacity:{value:0}},
    vertexShader:'varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
    fragmentShader:'varying vec2 vUv; uniform float opacity; void main(){float r=length((vUv-.5)*2.0);float a=exp(-4.0*r*r)*(1.0-smoothstep(.7,1.0,r));gl_FragColor=vec4(.12,.13,.16,a*opacity);}',
  });
  readonly mesh=new THREE.Mesh(new THREE.PlaneGeometry(1,1),this.material);
  private initialized=false;
  constructor(){this.mesh.rotation.x=-Math.PI/2;this.mesh.position.y=.003;this.mesh.visible=false;this.mesh.name='adaptive-contact-shadow';}
  reset(){this.initialized=false;this.mesh.visible=false;}
  dispose(){this.mesh.geometry.dispose();this.material.dispose();}
  update(vrm:VRM,dt:number){
    const points=parts.flatMap(([name,radius])=>{
      const bone=vrm.humanoid.getNormalizedBoneNode(name);
      if(!bone)return [];
      const scale=bone.getWorldScale(new THREE.Vector3());
      return [{position:bone.getWorldPosition(new THREE.Vector3()),radius:radius*Math.max(Math.abs(scale.x),Math.abs(scale.y),Math.abs(scale.z))}];
    });
    const footprint=contactFootprint(points);if(!footprint){this.mesh.visible=false;return;}
    const a=this.initialized?1-Math.exp(-8*Math.max(0,dt)):1;
    this.mesh.position.x=THREE.MathUtils.lerp(this.mesh.position.x,footprint.x,a);
    this.mesh.position.z=THREE.MathUtils.lerp(this.mesh.position.z,footprint.z,a);
    this.mesh.scale.x=THREE.MathUtils.lerp(this.mesh.scale.x,footprint.width,a);
    this.mesh.scale.y=THREE.MathUtils.lerp(this.mesh.scale.y,footprint.depth,a);
    this.material.uniforms.opacity.value=THREE.MathUtils.lerp(this.material.uniforms.opacity.value,footprint.opacity,a);
    this.mesh.visible=true;this.initialized=true;
  }
}
