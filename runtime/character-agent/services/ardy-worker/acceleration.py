"""Official ARDY inference backends, isolated dependencies and explicit fallback."""
import os
import sys
from pathlib import Path
import torch

ROOT=Path(__file__).resolve().parents[2]
DLL_HANDLES=[]

def dependencies():
    path=ROOT/'.cache'/'ardy-acceleration-deps'
    if path.exists() and str(path) not in sys.path:sys.path.insert(0,str(path))
    dll=ROOT.parent/'runtime'/'tensorrt'
    if os.name=='nt' and dll.exists() and not DLL_HANDLES:
        DLL_HANDLES.append(os.add_dll_directory(str(dll)))
        torch_dll=Path(torch.__file__).parent/'lib'
        DLL_HANDLES.append(os.add_dll_directory(str(torch_dll)))
        os.environ['PATH']=str(dll)+os.pathsep+str(torch_dll)+os.pathsep+os.environ.get('PATH','')

class AcceleratedInference:
    def __init__(self,model,params,record):
        self.model=model;self.record=record
        self.original_denoiser=model.denoiser;self.original_autoencoder=model.autoencoder
        self.backend='none';self.capacity=0
        requested=params.get('inference_backend','trt-fp16')
        if requested=='none':self.report(requested);return
        try:
            if requested=='torch-compile':
                model.compile_denoiser();model.warmup(num_tokens=(params['history_frames']+(40 if params['model']=='core40' else 8))//4,num_text_tokens=1)
                self.backend=requested
            else:
                dependencies()
                import json
                from ardy.model.trt import TRTCFGDenoiser,TRTDecoder,TRTAutoencoder
                folder=ROOT/'.cache'/'ardy-engines'/params['model']
                meta=json.loads((folder/'manifest.json').read_text())
                import tensorrt as trt
                if meta['trt_version']!=trt.__version__ or meta['gpu']!=torch.cuda.get_device_name():raise ValueError('Engine environment mismatch; rebuild')
                self.capacity=meta['max_frames']
                denoiser=TRTCFGDenoiser(str(folder/meta['denoiser']),model.denoiser,4,self.capacity//4,1)
                decoder=TRTDecoder(str(folder/meta['decoder']),num_tokens=self.capacity//4,num_frames_per_token=4)
                autoencoder=TRTAutoencoder(decoder,model.autoencoder)
                model.denoiser=denoiser;model.set_autoencoder(autoencoder)
                self.backend=requested
            self.report(requested)
        except Exception as exc:
            self.restore();self.report(requested,type(exc).__name__+': '+str(exc)[:180])

    def restore(self):
        self.model.denoiser=self.original_denoiser
        self.model.set_autoencoder(self.original_autoencoder)
        self.backend='none'

    def report(self,requested,error=None):
        self.record(inference_backend_requested=requested,inference_backend_active=self.backend,
                    inference_engine_max_frames=self.capacity,acceleration_error=error)

    def step(self,**kwargs):
        # Full-call fallback: denoiser and decoder must use the same safe window.
        oversized=self.backend=='trt-fp16' and kwargs['num_frames']>self.capacity
        if oversized:
            denoiser,autoencoder=self.model.denoiser,self.model.autoencoder
            try:
                self.model.denoiser=self.original_denoiser;self.model.set_autoencoder(self.original_autoencoder)
                self.record(inference_call_backend='none',inference_fallback_reason='window_exceeds_engine')
                return self.model.autoregressive_step(**kwargs)
            finally:self.model.denoiser=denoiser;self.model.set_autoencoder(autoencoder)
        rng=torch.cuda.get_rng_state() if torch.cuda.is_available() else None
        try:
            self.record(inference_call_backend=self.backend,inference_fallback_reason=None)
            output=self.model.autoregressive_step(**kwargs)
            if self.backend!='none' and not torch.isfinite(output).all():raise RuntimeError('Non-finite accelerated output')
            return output
        except Exception as exc:
            if self.backend=='none':raise
            failed=self.backend;self.restore()
            if rng is not None:torch.cuda.set_rng_state(rng)
            self.report(failed,type(exc).__name__+': '+str(exc)[:180])
            return self.model.autoregressive_step(**kwargs)
