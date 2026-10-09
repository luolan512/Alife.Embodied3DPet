"""Inference-only adapter for official TMR encoders. No Lightning/training stack or cloud calls."""
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def load_module(name,path,package=False):
    spec=importlib.util.spec_from_file_location(name,path,submodule_search_locations=[str(path.parent)] if package else None)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

class TMRBackend:
    def __init__(self,root=ROOT):
        import torch
        from transformers import AutoTokenizer, AutoModel
        self.torch=torch
        torch.set_num_threads(2)  # This module runs in its own CPU subprocess.
        repo=Path(root)/'.cache/TMR'
        run=repo/'models/tmr_humanml3d_guoh3dfeats'
        config=json.loads((run/'config.json').read_text())
        actor=load_module('monitor_tmr_actor',repo/'src/model/actor.py')
        self.features=load_module('monitor_guofeats',repo/'src/guofeats/__init__.py',True).joints_to_guofeats
        self.motion=self._encoder(actor,config['model']['motion_encoder'],run/'last_weights/motion_encoder.pt')
        self.text=self._encoder(actor,config['model']['text_encoder'],run/'last_weights/text_encoder.pt')
        self.mean=torch.load(repo/'stats/humanml3d/guoh3dfeats/mean.pt',weights_only=True,map_location='cpu')
        self.std=torch.load(repo/'stats/humanml3d/guoh3dfeats/std.pt',weights_only=True,map_location='cpu')
        text_dir=Path(root)/'.cache/tmr-distilbert'
        self.tokenizer=AutoTokenizer.from_pretrained(text_dir,local_files_only=True)
        self.distilbert=AutoModel.from_pretrained(text_dir,local_files_only=True).eval()
        self.cache={}

    def _encoder(self,actor,config,path):
        params={k:v for k,v in config.items() if not k.startswith('_')}
        encoder=actor.ACTORStyleEncoder(**params)
        encoder.load_state_dict(self.torch.load(path,map_location='cpu',weights_only=True),strict=True)
        return encoder.eval()

    def encode_text(self,texts):
        torch=self.torch
        missing=list(dict.fromkeys(t for t in texts if t not in self.cache))
        if missing:
            tokens=self.tokenizer(missing,padding=True,truncation=True,max_length=128,return_tensors='pt')
            with torch.inference_mode():
                hidden=self.distilbert(**tokens).last_hidden_state
                means=self.text({'x':hidden,'mask':tokens['attention_mask'].bool()})[:,0]
                means=torch.nn.functional.normalize(means,dim=-1)
            for text,embedding in zip(missing,means):self.cache[text]=embedding
            if len(self.cache)>96:self.cache={t:self.cache[t] for t in texts}
        return torch.stack([self.cache[t] for t in texts])

    def score(self,clips,texts):
        import numpy as np
        torch=self.torch
        text=self.encode_text(texts)
        converted=[]
        for clip in clips:
            joints=np.asarray(clip,dtype=np.float32)
            if joints.ndim!=3 or joints.shape[1:]!=(22,3) or len(joints)<10 or not np.isfinite(joints).all():
                raise ValueError('Expected finite Y-up HumanML22 joints')
            feats=self.features(joints)
            if feats.shape[1]!=263 or not np.isfinite(feats).all():raise ValueError('Invalid converted motion features')
            converted.append((torch.from_numpy(feats).float()-self.mean)/(self.std+1e-12))
        lengths=[len(x) for x in converted]
        x=torch.zeros((len(clips),max(lengths),263))
        mask=torch.zeros((len(clips),max(lengths)),dtype=torch.bool)
        for i,value in enumerate(converted):x[i,:len(value)]=value;mask[i,:len(value)]=True
        with torch.inference_mode():
            means=self.motion({'x':x,'mask':mask})[:,0]  # deterministic VAE mean, official sample_mean=True
            means=torch.nn.functional.normalize(means,dim=-1)
            scores=means@text.T
            if not torch.isfinite(scores).all():raise ValueError('Non-finite TMR embeddings')
            return scores.tolist()

def main():
    try:
        backend=TMRBackend()
        print(json.dumps({'ready':True}),flush=True)
    except Exception as exc:
        print(json.dumps({'error':str(exc)}),flush=True);return
    for line in sys.stdin:
        try:
            job=json.loads(line);started=time.perf_counter()
            scores=backend.score(job['clips'],job['texts'])
            result={'scores':scores,'inference_ms':round((time.perf_counter()-started)*1000,1)}
        except Exception as exc:result={'error':str(exc)}
        print(json.dumps(result,allow_nan=False),flush=True)

if __name__=='__main__':main()
