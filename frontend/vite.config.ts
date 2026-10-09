import {defineConfig} from 'vite';
import {resolve} from 'node:path';
export default defineConfig({root:resolve('apps/demo-three'),
  resolve:{alias:{'@pet-emotion':resolve('../emotionAdapter/index.ts')}},
  build:{outDir:resolve('dist/web'),emptyOutDir:true,rollupOptions:{input:{alifeChat:resolve('apps/demo-three/alife-chat.html'),main:resolve('apps/demo-three/index.html'),pet:resolve('apps/demo-three/pet.html'),chat:resolve('apps/demo-three/chat.html'),facePreview:resolve('apps/demo-three/face-preview.html'),motionObserver:resolve('apps/demo-three/motion-observer.html')}}}});
