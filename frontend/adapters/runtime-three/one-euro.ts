import * as THREE from 'three';

// Casiez et al.: cutoff = minCutoff + beta * filtered speed.
const alpha=(cutoff:number,dt:number)=>1/(1+1/(2*Math.PI*cutoff*dt));
export class OneEuroScalar {
  private raw?:number;
  private value=0;
  private speed=0;
  constructor(private minCutoff=1,private beta=.5,private derivativeCutoff=1){}
  filter(input:number,dt:number){
    if(this.raw===undefined||dt<=0||dt>.25){this.raw=input;this.value=input;this.speed=0;return input;}
    this.speed+=alpha(this.derivativeCutoff,dt)*((input-this.raw)/dt-this.speed);
    this.raw=input;
    this.value+=alpha(this.minCutoff+this.beta*Math.abs(this.speed),dt)*(input-this.value);
    return this.value;
  }
}

/** Rotation equivalent: angular speed in rad/s, shortest-arc SLERP, no Euler wrapping. */
export class OneEuroQuaternion {
  private raw?:THREE.Quaternion;
  private value=new THREE.Quaternion();
  private speed=0;
  constructor(private minCutoff=1,private beta=.5,private derivativeCutoff=1){}
  filter(input:THREE.Quaternion,dt:number){
    if(!this.raw||dt<=0||dt>.25){this.raw=input.clone().normalize();this.value.copy(this.raw);this.speed=0;return this.value;}
    const speed=this.raw.angleTo(input)/dt;
    this.speed+=alpha(this.derivativeCutoff,dt)*(speed-this.speed);
    this.raw.copy(input).normalize();
    this.value.slerp(this.raw,alpha(this.minCutoff+this.beta*this.speed,dt)).normalize();
    return this.value;
  }
}

export class SkeletonOneEuro {
  private root=[new OneEuroScalar(),new OneEuroScalar(),new OneEuroScalar()];
  private rotations=new Map<THREE.Object3D,OneEuroQuaternion>();
  private lastCursor?:number;
  reset(){this.root=[new OneEuroScalar(),new OneEuroScalar(),new OneEuroScalar()];this.rotations.clear();this.outputs.clear();this.outputRoot=undefined;this.lastCursor=undefined;}
  apply(root:THREE.Object3D,nodes:THREE.Object3D[],cursor:number,dt:number){
    // Pause/end-of-buffer redraws retain the same filtered pose without inventing time.
    if(this.lastCursor!==undefined&&(cursor<this.lastCursor||dt>.25))this.reset();
    const held=cursor===this.lastCursor;
    const filterDt=this.lastCursor===undefined?0:dt;
    const position=root.position.toArray();
    if(!held)this.outputRoot=position.map((v,i)=>this.root[i].filter(v,filterDt));
    root.position.fromArray(this.outputRoot??position);
    for(const node of nodes){
      let filter=this.rotations.get(node);
      if(!filter){filter=new OneEuroQuaternion();this.rotations.set(node,filter);}
      if(!held)this.outputs.set(node,filter.filter(node.quaternion,filterDt).clone());
      const output=this.outputs.get(node);if(output)node.quaternion.copy(output);
    }
    this.lastCursor=cursor;root.updateMatrixWorld(true);
  }
  private outputRoot?:number[];
  private outputs=new Map<THREE.Object3D,THREE.Quaternion>();
}
