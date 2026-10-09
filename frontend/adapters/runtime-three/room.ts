import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils, type VRM, type VRMHumanBoneName } from '@pixiv/three-vrm';
import { roomLayout, type Capabilities, type Command, type ObjectId } from '../../packages/contracts/legacy';
import { PetExpressionController } from '@pet-emotion';
import { VRMMotionPlayer } from './motion';
import {ContactShadow} from './contact-shadow';
import {GeneratedFace} from './generated-face';
import {EyeMicro} from './eye-micro';

const expressionMap: Record<string, string> = { neutral: 'neutral', happy: 'happy', sad: 'sad', angry: 'angry', relaxed: 'relaxed', wink_left: 'blinkLeft', wink_right: 'blinkRight' };
const WALK_SPEED = 1.1;
const ARRIVE_EPS = 0.07;
const REACH_TIME = 0.45;

const clamp = (v: number, lo = -roomLayout.bounds, hi = roomLayout.bounds) => Math.min(hi, Math.max(lo, v));
function angleDelta(from: number, to: number) { let d = (to - from) % (Math.PI * 2); if (d > Math.PI) d -= Math.PI * 2; if (d < -Math.PI) d += Math.PI * 2; return d; }

type Waypoint = { x: number; z: number };

export class Room {
  generatedFace?:GeneratedFace;
  eyeMicro?:EyeMicro;
  previewIdle=false;
  desktopZoomFraming=0;
  private previewFollowFace=false;
  private previewFaceDistance=1.3;
  motion?: VRMMotionPlayer;
  readonly scene = new THREE.Scene();
  readonly camera = new THREE.PerspectiveCamera(32, 1, 0.05, 100);
  readonly renderer: THREE.WebGLRenderer;
  readonly controls: OrbitControls;
  private vrm?: VRM;
  private contactShadow=new ContactShadow();
  private bases = new Map<string, THREE.Quaternion>();
  private command?: Command;
  private actionTime = 0;
  private actionDone = true;
  private time = 0;
  private walkPhase = 0;
  private expression = new PetExpressionController();
  private face = 'neutral';
  private faceWeight = 0;
  private currentFaceWeight = 0;
  private loadGeneration = 0;
  private resize: ResizeObserver;
  private quaternion = new THREE.Quaternion();
  private euler = new THREE.Euler();
  private tmpVec = new THREE.Vector3();
  private revision = Date.now();
  private desktopLights:{light:THREE.Light;base:number}[]=[];
  private baseY = 0;
  /** Model rotation that makes it face the viewer at yaw 0. rotateVRM0 sets this for VRM 0.x; we must not overwrite it. */
  private baseYaw = 0;

  /** Character world state owned by the renderer; the service only mirrors it. */
  private actor: { x: number; z: number; yaw: number; targetYaw: number } = { x: roomLayout.spawn.x, z: roomLayout.spawn.z, yaw: 0, targetYaw: 0 };
  private carrying: ObjectId | null = null;
  private waypoints: Waypoint[] = [];
  private walking = false;
  private moveSpeed = WALK_SPEED;
  private reach = 0;
  private phase: 'idle' | 'approach' | 'reach' | 'done' = 'idle';
  private smallCube!: THREE.Mesh;
  private placed = false;
  private debugGroup=new THREE.Group();
  private debugLabels:{bone:VRMHumanBoneName;sprite:THREE.Sprite}[]=[];
  private sourceLines:THREE.LineSegments;
  private skeletonVisible=false;
  private cameraMode:'full'|'face'|'eye'='full';
  private observerPosition=new THREE.Vector3(0,1.35,5.5);
  private gazeTarget:string|number[]='camera';
  cameraGazePolicy:'auto'|'off'|'follow'='auto';
  private requestedGaze:'auto'|'user'|'none'='auto';
  private ambientGaze:'auto'|'user'|'none'='auto';
  private ambientTarget:string|number[]='camera';
  private visionTarget=new THREE.WebGLRenderTarget(512,288);
  private visionCamera=new THREE.PerspectiveCamera(75,512/288,.02,100);
  private visionPixels=new Uint8Array(512*288*4);
  private visionCanvas=document.createElement('canvas');
  private visionThumbnail=document.createElement('canvas');
  onLookResult?: (packet:Record<string,unknown>)=>void;
  private activeLook?:{request_id:string;target:string|number[];question:string;turn_body:boolean;deadline:number;stableAt:number;lastForward?:THREE.Vector3;lastEye?:THREE.Vector3;lastSample?:number;sent:boolean;previousLookAt:boolean};

  constructor(private container: HTMLElement,readonly desktopMode=false) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: desktopMode });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, desktopMode?1:1.5));
    this.renderer.shadowMap.enabled = true; this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace; this.renderer.toneMapping = desktopMode?THREE.NoToneMapping:THREE.ACESFilmicToneMapping; this.renderer.toneMappingExposure = desktopMode?1:1.2;
    container.append(this.renderer.domElement);
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(new Float32Array(27*6),3));
    geometry.setAttribute('color',new THREE.Float32BufferAttribute(new Float32Array(27*6),3));
    this.sourceLines=new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({vertexColors:true,depthTest:false,depthWrite:false}));
    this.sourceLines.renderOrder=10;
    this.sourceLines.frustumCulled=false;this.debugGroup.add(this.sourceLines);
    for(const [bone,text,color] of [['rightHand','R hand','#269ddd'],['leftHand','L hand','#f0655d'],['rightFoot','R foot','#269ddd'],['leftFoot','L foot','#f0655d']] as const){
      const canvas=document.createElement('canvas');canvas.width=192;canvas.height=48;
      const ctx=canvas.getContext('2d')!;ctx.fillStyle=color;ctx.fillRect(0,0,192,48);ctx.fillStyle='white';ctx.font='bold 28px sans-serif';ctx.textAlign='center';ctx.fillText(text,96,34);
      const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(canvas),depthTest:false}));sprite.scale.set(.28,.07,1);sprite.renderOrder=20;
      this.debugLabels.push({bone,sprite});this.debugGroup.add(sprite);
    }
    this.debugGroup.visible=false;this.scene.add(this.debugGroup);
    this.scene.background = new THREE.Color('#e9edf4'); this.scene.fog = new THREE.Fog('#e9edf4', 7, 15);
    this.camera.position.set(0, 1.35, 5.5);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement); this.controls.target.set(0, .95, 1.35);
    this.controls.enableDamping = true; this.controls.minDistance = 1; this.controls.maxDistance = 6; this.controls.maxPolarAngle = Math.PI * .49;
    this.scene.add(new THREE.HemisphereLight('#ffffff', desktopMode?'#c8c8d2':'#7a8497', desktopMode?.8:2.2));
    const sun = new THREE.DirectionalLight(desktopMode?'#ffffff':'#fff3e2', desktopMode?1:2.7); sun.position.set(-2, 4, 3); sun.castShadow = true; sun.shadow.mapSize.set(1024, 1024); sun.shadow.bias = -.0004; this.scene.add(sun);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(16, 16), new THREE.MeshStandardMaterial({ color: '#dbe4ee', roughness: 1 })); floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; this.scene.add(floor);
    const grid = new THREE.GridHelper(8, 16, '#c2cddd', '#ced8e5'); grid.position.y = .001; this.scene.add(grid);
    const p = roomLayout.platform;
    const platform = new THREE.Mesh(new THREE.CylinderGeometry(p.radius, p.radius + .02, p.top, 64), new THREE.MeshStandardMaterial({ color: '#eff5fc', roughness: .7 })); platform.position.y = p.top / 2; platform.receiveShadow = true; this.scene.add(platform);
    const big = roomLayout.objects.big_cube;
    const bigCube = new THREE.Mesh(new THREE.BoxGeometry(big.size, big.size, big.size), new THREE.MeshStandardMaterial({ color: '#bac9ed', roughness: .75 })); bigCube.position.set(big.x, big.size / 2, big.z); bigCube.castShadow = true; bigCube.receiveShadow = true; this.scene.add(bigCube);
    const small = roomLayout.objects.small_cube;
    this.smallCube = new THREE.Mesh(new THREE.BoxGeometry(small.size, small.size, small.size), new THREE.MeshStandardMaterial({ color: '#9acdc0', roughness: .7 })); this.smallCube.position.set(small.x, small.size / 2, small.z); this.smallCube.castShadow = true; this.smallCube.receiveShadow = true; this.scene.add(this.smallCube);
    if(desktopMode){
      this.desktopLights=this.scene.children.filter((o):o is THREE.Light=>o instanceof THREE.Light).map(light=>({light,base:light.intensity}));
      this.setDesktopBrightness(1.8);
      this.scene.background=null;this.scene.fog=null;this.renderer.setClearColor(0,0);
      this.renderer.shadowMap.enabled=false;this.controls.enabled=false;
      for(const child of this.scene.children)if(child instanceof THREE.Mesh||child instanceof THREE.GridHelper)child.visible=false;
    }
    this.resize = new ResizeObserver(() => this.fit()); this.resize.observe(container); this.fit();
  }
  private fit() { const w = this.container.clientWidth, h = this.container.clientHeight; if (!w || !h) return; this.renderer.setSize(w, h); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();this.renderer.render(this.scene,this.camera); }

  async load(url: string, name: string): Promise<Capabilities | null> {
    const generation = ++this.loadGeneration;
    this.stop(); this.expression.reset();
    const loader = new GLTFLoader(); loader.register(parser => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync(url);
    const vrm = gltf.userData.vrm as VRM | undefined;
    if (!vrm) { VRMUtils.deepDispose(gltf.scene); throw new Error('文件不包含有效的 VRM 数据'); }
    if (generation !== this.loadGeneration) { VRMUtils.deepDispose(vrm.scene); return null; }
    const required = ['hips', 'spine', 'head', 'rightUpperArm', 'rightLowerArm'] as const;
    for (const bone of required) if (!vrm.humanoid.getNormalizedBoneNode(bone)) { VRMUtils.deepDispose(vrm.scene); throw new Error(`缺少必要骨骼：${bone}`); }
    if (this.vrm) { this.scene.remove(this.vrm.scene); VRMUtils.deepDispose(this.vrm.scene); }
    this.vrm = vrm; VRMUtils.rotateVRM0(vrm); this.baseYaw = vrm.scene.rotation.y; this.bases.clear();
    for (const bone of Object.keys(vrm.humanoid.normalizedHumanBones)) {
      const node = vrm.humanoid.getNormalizedBoneNode(bone as VRMHumanBoneName); if (node) this.bases.set(bone, node.quaternion.clone());
    }
    vrm.scene.traverse(obj => { if (obj instanceof THREE.Mesh) { obj.castShadow = true; obj.receiveShadow = false; obj.frustumCulled = false; } });
    this.scene.add(vrm.scene); vrm.humanoid.resetNormalizedPose(); vrm.update(0);
    vrm.firstPerson?.setup();
    const bounds = new THREE.Box3().setFromObject(vrm.scene); const height = bounds.max.y - bounds.min.y;
    if (Number.isFinite(height) && height > .1) { const scale = 1.65 / height; vrm.scene.scale.multiplyScalar(scale); this.baseY = .052 - bounds.min.y * scale; }
    this.resetWorld();
    this.motion=new VRMMotionPlayer(vrm);
    this.generatedFace=new GeneratedFace(vrm);
    this.eyeMicro=new EyeMicro(vrm);
    this.motion.desktopMode=this.desktopMode;
    this.cameraPreset('full');
    this.revision = Math.max(Date.now(), this.revision + 1);
    const expressions = Object.keys(expressionMap).filter(key => key === 'neutral' || !!vrm.expressionManager?.getExpression(expressionMap[key])) as Capabilities['expressions'];
    const objects = Object.entries(roomLayout.objects).map(([id, o]) => ({ id: id as ObjectId, x: o.x, z: o.z, grabbable: o.grabbable }));
    return { model_name: name, model_revision: this.revision, vrm_version: String(vrm.meta.metaVersion), actions: ['wave', 'nod', 'think', 'look_at', 'set_expression', 'move_to', 'turn', 'pick_up', 'put_down', 'stop'], expressions, bones: this.bases.size, objects };
  }
  private resetWorld() {
    this.contactShadow.reset();
    const s = roomLayout.objects.small_cube;
    this.actor = { x: roomLayout.spawn.x, z: roomLayout.spawn.z, yaw: 0, targetYaw: 0 };
    this.carrying = null; this.waypoints = []; this.walking = false; this.reach = 0; this.phase = 'idle'; this.placed = false;
    this.smallCube.position.set(s.x, s.size / 2, s.z); this.smallCube.rotation.set(0, 0, 0);
    this.applyTransform();
  }
  private applyTransform() {
    const vrm = this.vrm; if (!vrm) return;
    vrm.scene.position.set(this.actor.x, this.baseY, this.actor.z);
    vrm.scene.rotation.y = this.baseYaw + this.actor.yaw;
  }
  private rotate(bone: VRMHumanBoneName, x: number, y: number, z: number) {
    const node = this.vrm?.humanoid.getNormalizedBoneNode(bone), base = this.bases.get(bone);
    if (node && base) node.quaternion.copy(base).multiply(this.quaternion.setFromEuler(this.euler.set(x, y, z)));
  }
  /** Resolve a semantic target (named object or explicit coordinates) to a floor position. */
  private resolveTarget(args: Command['args'], margin = 0): Waypoint {
    if (args.target) return { x: clamp(args.target.x), z: clamp(args.target.z) };
    const id = args.target_id ?? 'platform';
    if (id === 'platform') return this.approachPoint(roomLayout.platform, roomLayout.platform.radius + 0.45);
    const obj = roomLayout.objects[id as keyof typeof roomLayout.objects];
    if (!obj) return { x: clamp(this.actor.x), z: clamp(this.actor.z) };
    return this.approachPoint(obj, obj.size / 2 + (margin || 0.42));
  }
  private approachPoint(obj: { x: number; z: number }, margin: number): Waypoint {
    const dx = obj.x - this.actor.x, dz = obj.z - this.actor.z; const len = Math.hypot(dx, dz) || 1e-6;
    return { x: clamp(obj.x - dx / len * margin), z: clamp(obj.z - dz / len * margin) };
  }
  /** Route around the platform pedestal so the character walks around it instead of through it. */
  private pathTo(target: Waypoint): Waypoint[] {
    const o = roomLayout.platform; const from = { x: this.actor.x, z: this.actor.z };
    const dx = target.x - from.x, dz = target.z - from.z; const len = Math.hypot(dx, dz) || 1e-6;
    const t = Math.min(1, Math.max(0, ((o.x - from.x) * dx + (o.z - from.z) * dz) / (len * len)));
    const px = from.x + t * dx, pz = from.z + t * dz;
    if (Math.hypot(o.x - px, o.z - pz) >= o.radius + 0.32) return [target];
    const side = ((o.x - from.x) * dz - (o.z - from.z) * dx) >= 0 ? 1 : -1;
    const nx = -dz / len, nz = dx / len; const off = o.radius + 0.5;
    return [{ x: clamp(o.x + nx * off * side), z: clamp(o.z + nz * off * side) }, target];
  }
  private startMove(args: Command['args'], margin = 0) {
    const target = this.resolveTarget(args, margin);
    this.waypoints = this.pathTo(target); this.moveSpeed = args.speed ?? WALK_SPEED; this.walking = this.waypoints.length > 0;
  }

  start(command: Command) {
    if (!this.vrm || command.model_revision !== this.revision) throw new Error('stale_model');
    this.command = command; this.actionTime = 0; this.actionDone = false; this.reach = 0; this.phase = 'idle';
    this.face = command.args.emotion || 'neutral'; this.faceWeight = command.args.intensity;
    this.expression.set(this.face.startsWith('wink') ? 'happy' : this.face, this.faceWeight);
    switch (command.action) {
      case 'move_to': this.startMove(command.args); break;
      case 'turn': { const t = this.resolveTarget(command.args, 0); this.actor.targetYaw = Math.atan2(t.x - this.actor.x, t.z - this.actor.z); break; }
      case 'pick_up': this.phase = 'approach'; this.startMove({ ...command.args, target_id: 'small_cube' }, 0.42); break;
      case 'put_down': this.phase = 'approach'; this.startMove({ ...command.args, target_id: 'platform' }); break;
      default: break;
    }
  }
  stop() { this.command = undefined; this.faceWeight = 0; this.actionTime = 0; this.waypoints = []; this.walking = false; this.reach = 0; this.phase = 'idle'; this.actionDone = true; }

  private stepMovement(dt: number) {
    if (!this.waypoints.length) { this.walking = false; return; }
    const wp = this.waypoints[0];
    const dx = wp.x - this.actor.x, dz = wp.z - this.actor.z; const dist = Math.hypot(dx, dz);
    if (dist < ARRIVE_EPS) { this.waypoints.shift(); this.walking = this.waypoints.length > 0; return; }
    const desired = Math.atan2(dx, dz);
    this.actor.yaw += angleDelta(this.actor.yaw, desired) * Math.min(1, dt * 7);
    const step = Math.min(this.moveSpeed * dt, dist);
    this.actor.x = clamp(this.actor.x + Math.sin(this.actor.yaw) * step);
    this.actor.z = clamp(this.actor.z + Math.cos(this.actor.yaw) * step);
    this.walkPhase += dt * 8; this.walking = true;
  }
  private stepTurn(dt: number) {
    if (this.command?.action !== 'turn') return;
    this.actor.yaw += angleDelta(this.actor.yaw, this.actor.targetYaw) * Math.min(1, dt * 6);
  }
  private stepGrab(dt: number) {
    const c = this.command; if (!c || (c.action !== 'pick_up' && c.action !== 'put_down')) return;
    if (this.phase === 'approach') { if (!this.waypoints.length) { this.phase = 'reach'; this.reach = 0; } return; }
    if (this.phase === 'reach') {
      this.reach = Math.min(1, this.reach + dt / REACH_TIME);
      if (this.reach >= 1) {
        if (c.action === 'pick_up') { this.carrying = 'small_cube'; this.placed = false; }
        else { const p = roomLayout.platform, s = roomLayout.objects.small_cube; this.carrying = null; this.placed = true; this.smallCube.position.set(p.x, p.top + s.size / 2, p.z); this.smallCube.rotation.set(0, 0, 0); }
        this.phase = 'done'; this.actionDone = true;
      }
    }
  }
  private attachCarried() {
    if (this.carrying !== 'small_cube') return;
    const hand = this.vrm?.humanoid.getNormalizedBoneNode('rightHand');
    if (!hand) return;
    hand.getWorldPosition(this.tmpVec); this.tmpVec.y += 0.02;
    this.smallCube.position.copy(this.tmpVec);
  }

  private pose() {
    const vrm = this.vrm; if (!vrm) return;
    for (const [bone, q] of this.bases) vrm.humanoid.getNormalizedBoneNode(bone as VRMHumanBoneName)?.quaternion.copy(q);
    // Neutral relaxed arms; normalized humanoid rotations are independent of model node names.
    const armSign=vrm.meta.metaVersion==='1'?-1:1;
    this.rotate('leftUpperArm', 0, 0, 1.22*armSign); this.rotate('rightUpperArm', 0, 0, -1.22*armSign);
    this.rotate('leftLowerArm', 0, -.1, .06); this.rotate('rightLowerArm', 0, .1, -.06);
    this.rotate('chest', Math.sin(this.time * 1.7) * .012, 0, 0);
    if (this.walking) {
      const swing = Math.sin(this.walkPhase) * .5;
      this.rotate('leftUpperLeg', swing, 0, 0); this.rotate('rightUpperLeg', -swing, 0, 0);
      this.rotate('leftLowerLeg', Math.max(0, -swing) * .8, 0, 0); this.rotate('rightLowerLeg', Math.max(0, swing) * .8, 0, 0);
      this.rotate('leftUpperArm', 0, 0, 1.22 + swing * .3); this.rotate('rightUpperArm', 0, 0, -1.22 - swing * .3);
    }
    const c = this.command; if (!c) return;
    const end = c.args.duration_ms / 1000, t = this.actionTime;
    const blend = Math.min(1, t / .25, Math.max(0, (end - t) / .3));
    if (c.action === 'wave') {
      this.rotate('rightUpperArm', 0, 0, -1.22 + 1.8 * blend);
      this.rotate('rightLowerArm', -.2 * blend, 0, 1.05 * blend);
      this.rotate('rightHand', 0, Math.sin(t * 12) * .35 * blend, 0);
      this.rotate('head', 0, 0, -.07 * blend);
    } else if (c.action === 'nod') this.rotate('head', Math.sin(t * 8) * .18 * blend, 0, 0);
    else if (c.action === 'think') {
      this.rotate('rightUpperArm', -.35 * blend, 0, -1.22 + .5 * blend);
      this.rotate('rightLowerArm', -1.4 * blend, .25 * blend, .55 * blend);
      this.rotate('head', -.08 * blend, .12 * blend, .12 * blend);
    } else if (c.action === 'look_at') {
      const p = this.camera.position;
      this.rotate('head', THREE.MathUtils.clamp((1.3 - p.y) * .2, -.2, .2) * blend, THREE.MathUtils.clamp(Math.atan2(p.x, p.z), -.5, .5) * blend, 0);
    } else if ((c.action === 'pick_up' || c.action === 'put_down') && this.reach > 0) {
      // Reach forward and down toward the object, then return.
      const r = Math.sin(Math.min(1, this.reach) * Math.PI);
      this.rotate('rightUpperArm', -.15 - .55 * r, 0, -1.22 + .75 * r);
      this.rotate('rightLowerArm', -.9 * r, .1 * r, .35 * r);
      this.rotate('head', -.14 * r, 0, 0);
      this.rotate('spine', -.08 * r, 0, 0);
    }
  }

  isActionDone(): boolean { return this.actionDone; }
  getState() { const yaw = Math.atan2(Math.sin(this.actor.yaw), Math.cos(this.actor.yaw)); return { x: Number(this.actor.x.toFixed(3)), z: Number(this.actor.z.toFixed(3)), yaw: Number(yaw.toFixed(3)), carrying: this.carrying, action: this.command?.action ?? null }; }

  update(dt: number) {
    const elapsed=Math.max(0,dt);
    const playbackDt=Math.min(dt,.25);
    dt = Math.min(dt, .05); this.time += dt; this.actionTime += dt; this.controls.update();
    if (this.vrm) {
      if(this.motion?.skeleton){
        if(this.cameraMode!=='eye')this.observerPosition.copy(this.camera.position);
        if(!this.desktopMode)this.motion.lookAt=false;
        else this.motion.lookTarget.copy(this.camera.position);
        this.motion.headingOffset=0;
        this.motion.update(playbackDt);
        if(this.previewIdle&&this.motion.received===0)this.pose();
        const p=this.vrm.scene.position;this.actor.x=p.x;this.actor.z=p.z;
      } else {
        this.applyTransform();if(this.previewIdle)this.pose();else this.vrm.humanoid.resetNormalizedPose();
      }
      const c = this.command;
      if (c && !this.actionDone) {
        const geometric = c.action === 'move_to' || c.action === 'turn' || c.action === 'pick_up' || c.action === 'put_down';
        if (c.action === 'move_to' && !this.waypoints.length) this.actionDone = true;
        if (c.action === 'turn' && Math.abs(angleDelta(this.actor.yaw, this.actor.targetYaw)) < 0.06) this.actionDone = true;
        if (!geometric && this.actionTime * 1000 >= c.args.duration_ms) this.actionDone = true;
      }
      const semantic = this.expression.update(dt);
      const eyelids=this.generatedFace?.eyelids(semantic.blinkLeft,semantic.blinkRight)??{left:semantic.blinkLeft,right:semantic.blinkRight};
      const manager = this.vrm.expressionManager;
      if (manager) {
        for (const key of [...Object.values(expressionMap), 'blink', 'blinkLeft', 'blinkRight']) manager.setValue(key, 0);
        const generated=this.generatedFace?.update(elapsed,Math.max(eyelids.left,eyelids.right))??false;
        this.currentFaceWeight = THREE.MathUtils.damp(this.currentFaceWeight, this.faceWeight, 10, dt);
        const weight = this.currentFaceWeight;
        if (!generated&&this.face === 'happy') manager.setValue('happy', Math.min(1, weight * (.35 + semantic.smile)));
        else if (!generated&&expressionMap[this.face]) manager.setValue(expressionMap[this.face], weight);
        const winkL = this.face === 'wink_left' ? weight : 0, winkR = this.face === 'wink_right' ? weight : 0;
        const eyes=this.generatedFace?.eyelids(Math.max(winkL,semantic.blinkLeft),Math.max(winkR,semantic.blinkRight))??{left:Math.max(winkL,semantic.blinkLeft),right:Math.max(winkR,semantic.blinkRight)};
        if(manager.getExpression('blinkLeft')&&manager.getExpression('blinkRight')){manager.setValue('blinkLeft',eyes.left);manager.setValue('blinkRight',eyes.right);}
        else manager.setValue('blink',Math.max(eyes.left,eyes.right));
      }
      this.eyeMicro?.beforeUpdate();
      this.vrm.update(dt);
      this.eyeMicro?.afterUpdate(dt,Math.max(eyelids.left,eyelids.right));
      this.motion?.captureObservation();
      this.updateActiveLook();
      if(this.cameraMode==='eye')this.updateEyeCamera();
      if(this.skeletonVisible){
        for(const {bone,sprite} of this.debugLabels){const node=this.vrm.humanoid.getNormalizedBoneNode(bone);sprite.visible=!!node;if(node)sprite.position.copy(node.getWorldPosition(new THREE.Vector3())).add(new THREE.Vector3(0,.06,0));}
        const points=this.motion?.sourceJoints??[],meta=this.motion?.skeleton;
        const position=this.sourceLines.geometry.getAttribute('position'),colors=this.sourceLines.geometry.getAttribute('color');let count=0;
        if(meta)meta.parents.forEach((parent,i)=>{if(parent<0||!points[i]||!points[parent])return;
          const color=new THREE.Color(meta.names[i].startsWith('Right')?'#269ddd':meta.names[i].startsWith('Left')?'#f0655d':'#8291a7');
          for(const p of [points[parent],points[i]]){position.setXYZ(count,p[0]+1.2,p[1],p[2]);colors.setXYZ(count,color.r,color.g,color.b);count++;}});
        position.needsUpdate=true;colors.needsUpdate=true;this.sourceLines.geometry.setDrawRange(0,count);
      }
    }
    if(this.previewFollowFace&&this.vrm){
      const head=this.vrm.humanoid.getRawBoneNode('head')?.getWorldPosition(new THREE.Vector3());
      if(head){head.y+=.07;const delta=head.sub(this.controls.target).multiplyScalar(1-Math.exp(-playbackDt*8));this.controls.target.add(delta);this.camera.position.add(delta);this.camera.updateMatrixWorld(true);}
    }
    if(this.desktopMode&&!this.previewFollowFace&&this.vrm){
      const head=this.vrm.humanoid.getRawBoneNode('head')?.getWorldPosition(new THREE.Vector3());
      if(head){
        head.y+=.07;
        const target=new THREE.Vector3(0,1.05,1.35).lerp(head,this.desktopZoomFraming);
        const delta=target.sub(this.controls.target).multiplyScalar(1-Math.exp(-playbackDt*12));
        this.controls.target.add(delta);this.camera.position.add(delta);this.camera.updateMatrixWorld(true);
      }
    }
    this.renderer.render(this.scene, this.camera);
  }
  dispose() { ++this.loadGeneration; this.resize.disconnect(); this.controls.dispose(); this.contactShadow.dispose(); if (this.vrm) VRMUtils.deepDispose(this.vrm.scene); this.renderer.dispose(); }
  headScreenAnchor(){
    const head=this.vrm?.humanoid.getRawBoneNode('head');if(!head)return null;
    this.vrm!.scene.updateMatrixWorld(true);
    const p=head.getWorldPosition(new THREE.Vector3());p.y+=.18;p.project(this.camera);
    return {x:(p.x+1)*this.container.clientWidth/2,y:(1-p.y)*this.container.clientHeight/2,visible:p.z>=-1&&p.z<=1&&Math.abs(p.x)<1.2&&Math.abs(p.y)<1.3};
  }
  setExpression(name:string,lookAt?:boolean|'auto'|'user'|'none',target?:string|number[],scope='step'){
    this.face=name;this.faceWeight=.65;this.expression.set(name,.65);
    // An emotion-only message must preserve gaze, rather than silently track the camera.
    if(lookAt!==undefined){this.requestedGaze=typeof lookAt==='boolean'?(lookAt?'user':'none'):lookAt;this.gazeTarget=target??'camera';}
    if(lookAt!==undefined&&scope==='plan'){this.ambientGaze=this.requestedGaze;this.ambientTarget=this.gazeTarget;}
    else if(lookAt==='auto'){this.requestedGaze=this.ambientGaze;this.gazeTarget=this.ambientTarget;}
    this.refreshGaze();
  }
  facePacket(data:unknown){this.generatedFace?.packet(data);}
  setCameraGazePolicy(policy:'auto'|'off'|'follow'){this.cameraGazePolicy=policy;this.refreshGaze();}
  private refreshGaze(){
    if(!this.motion||this.activeLook)return;
    const camera=this.gazeTarget==='camera';
    this.motion.lookAt=this.requestedGaze!=='none'&&this.gazeTarget!=='none'&&
      (camera?this.cameraGazePolicy!=='off'&&(this.requestedGaze==='user'||this.cameraGazePolicy==='follow'):this.requestedGaze==='user');
  }
  private resolveGazeTarget(){
    if(Array.isArray(this.gazeTarget))return new THREE.Vector3().fromArray(this.gazeTarget);
    if(this.gazeTarget==='small_cube')return this.smallCube.getWorldPosition(new THREE.Vector3());
    if(this.gazeTarget==='big_cube'){const o=roomLayout.objects.big_cube;return new THREE.Vector3(o.x,o.size/2,o.z);}
    if(this.gazeTarget==='platform')return new THREE.Vector3(0,roomLayout.platform.top,0);
    return this.observerPosition.clone();
  }
  private lookPoint(target:string|number[]){
    if(Array.isArray(target))return new THREE.Vector3().fromArray(target);
    if(target==='small_cube')return this.smallCube.getWorldPosition(new THREE.Vector3());
    if(target==='big_cube'){const o=roomLayout.objects.big_cube;return new THREE.Vector3(o.x,o.size/2,o.z);}
    if(target==='platform')return new THREE.Vector3(0,roomLayout.platform.top,0);
    if(target==='camera')return this.observerPosition.clone();
    return null;
  }
  requestLook(data:{request_id:string;target:string|number[];question:string;turn_body:boolean;timeout_s:number}){
    if(!this.lookPoint(data.target)){
      this.onLookResult?.({type:'look_result',request_id:data.request_id,status:'TARGET_NOT_FOUND'});return;
    }
    this.activeLook={...data,deadline:performance.now()+data.timeout_s*1000,stableAt:0,sent:false,previousLookAt:this.motion?.lookAt??true};
  }
  releaseLook(request_id:string){
    if(this.activeLook?.request_id===request_id){
      if(this.motion){this.motion.lookAt=this.activeLook.previousLookAt;this.motion.headingOffset=Math.atan2(Math.sin(this.motion.headingOffset),Math.cos(this.motion.headingOffset));}
      this.activeLook=undefined;
      this.refreshGaze();
    }
  }
  private updateActiveLook(){
    const look=this.activeLook;if(!look||look.sent||!this.vrm)return;
    const now=performance.now(),target=this.lookPoint(look.target);
    if(!target)return;
    const head=this.vrm.humanoid.getNormalizedBoneNode('head');if(!head)return;
    const forward=new THREE.Vector3().fromArray(this.motion?.facing('head')??[0,0,1]);
    const eye=head.getWorldPosition(new THREE.Vector3()).add(new THREE.Vector3(0,.06,0)).addScaledVector(forward,.1);
    const camera=new THREE.PerspectiveCamera(75,512/288,.02,100);camera.position.copy(eye);camera.lookAt(eye.clone().add(forward));camera.updateMatrixWorld(true);
    const ndc=target.clone().project(camera),toward=target.clone().sub(eye);
    const inView=toward.dot(forward)>0&&Math.abs(ndc.x)<.9&&Math.abs(ndc.y)<.9&&ndc.z>=-1&&ndc.z<=1;
    // Geometry occlusion check against other scene props (avatar is excluded).
    const ray=new THREE.Raycaster(eye,toward.clone().normalize(),.02,Math.max(.02,toward.length()-.05));
    const blockers=this.scene.children.filter(object=>object instanceof THREE.Mesh&&object!==this.smallCube);
    const blocked=ray.intersectObjects(blockers,false).some(hit=>hit.distance<toward.length()-.35);
    const elapsed=Math.max(.001,(now-(look.lastSample??now))/1000);
    const stable=!!look.lastForward&&!!look.lastEye&&forward.angleTo(look.lastForward)/elapsed<.35&&eye.distanceTo(look.lastEye)/elapsed<.2;
    look.lastForward=forward;look.lastEye=eye;look.lastSample=now;
    if(inView&&!blocked&&stable){if(!look.stableAt)look.stableAt=now;}else look.stableAt=0;
    if(look.stableAt&&now-look.stableAt>=600){
      const frame=this.captureVision('eye');if(!frame)return;
      look.sent=true;this.onLookResult?.({...frame,type:'look_result',request_id:look.request_id,status:'CAPTURED',captured_at_unix:Date.now()/1000,target_in_view:true,stable_ms:now-look.stableAt});
    }else if(now>=look.deadline){
      look.sent=true;this.onLookResult?.({type:'look_result',request_id:look.request_id,status:!inView||blocked?'NOT_VISIBLE':'UNSTABLE',reason:blocked?'target_occluded':!inView?'target_outside_eye_view':'camera_not_stable'});
    }
  }
  cameraPreset(mode:'full'|'face'|'eye'){
    this.previewFollowFace=false;
    this.cameraMode=mode;this.controls.enabled=!this.desktopMode&&mode!=='eye';this.camera.near=mode==='eye'?.02:.05;this.camera.fov=mode==='eye'?75:32;this.camera.updateProjectionMatrix();
    this.camera.layers.set(0);
    if(this.vrm?.firstPerson)this.camera.layers.enable(mode==='eye'?this.vrm.firstPerson.firstPersonOnlyLayer:this.vrm.firstPerson.thirdPersonOnlyLayer);
    if(mode==='eye'){this.updateEyeCamera();return;}
    const hips=this.vrm?.humanoid.getNormalizedBoneNode('hips')?.getWorldPosition(new THREE.Vector3())??new THREE.Vector3(0,.9,1.35);
    const head=this.vrm?.humanoid.getNormalizedBoneNode('head')?.getWorldPosition(new THREE.Vector3())??hips.clone().add(new THREE.Vector3(0,.6,0));
    const target=mode==='face'?hips.clone().lerp(head,.65):this.desktopMode?new THREE.Vector3(0,1.05,1.35):hips.clone().add(new THREE.Vector3(0,.05,0));
    const forward=mode==='face'?new THREE.Vector3().fromArray(this.motion?.facing()??[0,0,1]):new THREE.Vector3(0,0,1);
    this.controls.target.copy(target);this.camera.position.copy(target).addScaledVector(forward,mode==='face'?2:this.desktopMode?6:4.15).add(new THREE.Vector3(0,.25,0));
    this.camera.lookAt(target);this.controls.update();this.observerPosition.copy(this.camera.position);
  }
  focusFacePreview(distance=1.3){
    this.previewFaceDistance=distance;
    this.cameraPreset('face');
    this.previewFollowFace=true;
    this.vrm?.scene.updateMatrixWorld(true);this.vrm?.update(0);
    const head=this.vrm?.humanoid.getRawBoneNode('head')?.getWorldPosition(new THREE.Vector3());
    if(!head)return;
    head.y+=.07;this.controls.target.copy(head);this.camera.position.copy(head).add(new THREE.Vector3(0,0,this.previewFaceDistance));
    this.camera.lookAt(head);this.controls.update();this.camera.updateMatrixWorld(true);
  }
  facePreviewAnchor(){return this.vrm?.humanoid.getRawBoneNode('head')?.getWorldPosition(new THREE.Vector3()).toArray();}
  private updateEyeCamera(){
    const head=this.vrm?.humanoid.getNormalizedBoneNode('head');if(!head)return;
    const forward=new THREE.Vector3().fromArray(this.motion?.facing('head')??[0,0,1]);
    const eye=head.getWorldPosition(new THREE.Vector3()).add(new THREE.Vector3(0,.06,0)).addScaledVector(forward,.1);
    this.camera.position.copy(eye);this.camera.lookAt(eye.clone().add(forward));
  }
  spatialFeedback(){
    const big=roomLayout.objects.big_cube;
    return {frame_id:'world',body_forward:this.motion?.facing(),head_forward:this.motion?.facing('head'),
      camera:{mode:this.cameraMode,position:this.camera.position.toArray(),forward:this.camera.getWorldDirection(new THREE.Vector3()).toArray(),fov_degrees:this.camera.fov},
      observer:{position:this.observerPosition.toArray(),source:'virtual_observer'},
      gaze:{...this.motion?.gazeDiagnostics(),requested_target:this.gazeTarget,camera_policy:this.cameraGazePolicy,requested:this.requestedGaze},
      objects:this.desktopMode?[]:[{id:'small_cube',position:this.smallCube.getWorldPosition(new THREE.Vector3()).toArray(),size:[.2,.2,.2],grabbable:false},
        {id:'big_cube',position:[big.x,big.size/2,big.z],size:[big.size,big.size,big.size],grabbable:false},
        {id:'platform',position:[0,roomLayout.platform.top/2,0],size:[1.24,roomLayout.platform.top,1.24],grabbable:false}]};
  }
  /** Low-resolution scene-only image, independent of the user's orbit/preset camera. */
  captureVision(source:'eye'|'viewport'='eye'){
    if(!this.vrm)return null;
    const camera=this.visionCamera;
    if(source==='eye'){
      const head=this.vrm.humanoid.getNormalizedBoneNode('head');if(!head)return null;
      const forward=new THREE.Vector3().fromArray(this.motion?.facing('head')??[0,0,1]);
      camera.position.copy(head.getWorldPosition(new THREE.Vector3())).add(new THREE.Vector3(0,.06,0)).addScaledVector(forward,.1);
      camera.fov=75;camera.lookAt(camera.position.clone().add(forward));camera.layers.set(0);
      if(this.vrm.firstPerson)camera.layers.enable(this.vrm.firstPerson.firstPersonOnlyLayer);
    }else{
      camera.position.copy(this.camera.position);camera.quaternion.copy(this.camera.quaternion);camera.fov=this.camera.fov;camera.layers.mask=this.camera.layers.mask;
      // Preserve the user's camera point and orientation in a fixed 16:9 observation frame.
    }
    camera.aspect=512/288;camera.updateProjectionMatrix();
    const target=this.renderer.getRenderTarget(),visible=this.debugGroup.visible;
    try{
      this.debugGroup.visible=false;this.renderer.setRenderTarget(this.visionTarget);
      this.renderer.render(this.scene,camera);this.renderer.readRenderTargetPixels(this.visionTarget,0,0,512,288,this.visionPixels);
    }finally{this.renderer.setRenderTarget(target);this.debugGroup.visible=visible;}
    this.visionCanvas.width=512;this.visionCanvas.height=288;
    const ctx=this.visionCanvas.getContext('2d')!,pixels=ctx.createImageData(512,288);
    for(let y=0;y<288;y++)pixels.data.set(this.visionPixels.subarray((287-y)*512*4,(288-y)*512*4),y*512*4);
    ctx.putImageData(pixels,0,0);
    this.visionThumbnail.width=12;this.visionThumbnail.height=12;
    const small=this.visionThumbnail.getContext('2d')!;small.drawImage(this.visionCanvas,0,0,12,12);
    const rgb=small.getImageData(0,0,12,12).data;
    const signature=Array.from({length:144},(_,i)=>Math.round((rgb[i*4]+rgb[i*4+1]+rgb[i*4+2])/3));
    return {type:'vision_frame',image:this.visionCanvas.toDataURL('image/jpeg',.72),signature,
      camera:{source,position:camera.position.toArray(),forward:camera.getWorldDirection(new THREE.Vector3()).toArray(),fov_degrees:camera.fov,width:512,height:288,frame:Math.floor(this.motion?.cursor??0)}};
  }
  resetMotion(meta:import('../../packages/contracts/motion').SkeletonMetadata){
    if(!this.vrm)return;
    this.vrm.humanoid.resetNormalizedPose();this.vrm.update(0);this.resetWorld();
    this.vrm.springBoneManager?.reset();
    this.motion=new VRMMotionPlayer(this.vrm);this.motion.setSkeleton(meta);
    this.motion.desktopMode=this.desktopMode;
    this.cameraPreset('full');
  }
  showSkeleton(value:boolean){this.skeletonVisible=value;this.debugGroup.visible=value;}
  setDesktopBrightness(value:number){for(const {light,base} of this.desktopLights)light.intensity=base*Math.max(.5,Math.min(3,value));}
  hitsAvatar(x:number,y:number){
    if(!this.vrm)return false;
    const rect=this.renderer.domElement.getBoundingClientRect();
    if(x<rect.left||x>rect.right||y<rect.top||y>rect.bottom)return false;
    const ray=new THREE.Raycaster();
    ray.layers.mask=this.camera.layers.mask;
    ray.setFromCamera(new THREE.Vector2((x-rect.left)/rect.width*2-1,1-(y-rect.top)/rect.height*2),this.camera);
    return ray.intersectObject(this.vrm.scene,true).some(hit=>hit.object instanceof THREE.Mesh&&hit.object.visible);
  }
  private hitTarget=new THREE.WebGLRenderTarget(48,72);
  desktopHitMask(){
    const previous=this.renderer.getRenderTarget();
    const shadowVisible=this.contactShadow.mesh.visible;
    this.contactShadow.mesh.visible=false;
    this.renderer.setRenderTarget(this.hitTarget);this.renderer.clear();this.renderer.render(this.scene,this.camera);
    this.contactShadow.mesh.visible=shadowVisible;
    const pixels=new Uint8Array(48*72*4);this.renderer.readRenderTargetPixels(this.hitTarget,0,0,48,72,pixels);
    this.renderer.setRenderTarget(previous);
    const mask=new Uint8Array(48*72);
    for(let y=0;y<72;y++)for(let x=0;x<48;x++)mask[y*48+x]=pixels[((71-y)*48+x)*4+3]>10?1:0;
    const padded=mask.slice();for(let y=1;y<71;y++)for(let x=1;x<47;x++)if(mask[y*48+x])for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++)padded[(y+dy)*48+x+dx]=1;
    return {width:48,height:72,mask:btoa(String.fromCharCode(...padded))};
  }
  diagnostics(){
    const bones=['hips','head','rightHand','leftHand','rightShoulder','leftShoulder','rightFoot','leftFoot'] as const;
    const positions=Object.fromEntries(bones.map(name=>[name,this.vrm?.humanoid.getNormalizedBoneNode(name)?.getWorldPosition(new THREE.Vector3()).toArray()]));
    const screenPositions=Object.fromEntries(bones.map(name=>{const point=this.vrm?.humanoid.getNormalizedBoneNode(name)?.getWorldPosition(new THREE.Vector3()).project(this.camera);return [name,point?[(point.x+1)/2*this.renderer.domElement.clientWidth,(1-point.y)/2*this.renderer.domElement.clientHeight]:undefined];}));
    const rotations=Object.fromEntries(bones.map(name=>[name,this.vrm?.humanoid.getNormalizedBoneNode(name)?.quaternion.toArray()]));
    return {positions,rotations,screenPositions,eyeMicro:this.eyeMicro?.snapshot(),headAnchor:this.headScreenAnchor(),bodyAvoidance:this.motion?.collisionDiagnostics(),feedback:this.motion?.feedback(),spatial:this.spatialFeedback(),expression:this.face,mapping:this.motion?.mappingDiagnostics(),
      sourceJointCount:this.motion?.sourceJoints.length,sourceSegments:this.sourceLines.geometry.drawRange.count};
  }
}
