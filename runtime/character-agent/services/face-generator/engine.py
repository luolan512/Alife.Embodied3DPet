"""Local CTEG text generation and an explicit geometric FLAME-to-Fcl approximation."""
from pathlib import Path
import sys, importlib, pickle, time, json, math, os
ROOT=Path(__file__).resolve().parents[2]
CACHE=ROOT/'.cache'/'face-audit'
sys.path.insert(0,str(CACHE/'EmoAva'/'src'))
import numpy as np
import torch
from transformers import BertTokenizer,BertModel,BertConfig
from transformer.Models import Transformer
from transformer.Translator import TranslatorContinue

class _ArrayProxy:
    def __setstate__(self,state):self.__dict__.update(state)

class _ModelReader(pickle.Unpickler):
    def find_class(self,module,name):
        if (module,name)==('chumpy.ch','Ch'):return _ArrayProxy
        if module in ('numpy','numpy.core.multiarray','scipy.sparse.csc','__builtin__'):
            return super().find_class(module,name)
        raise ValueError('Unexpected geometry object')

class LandmarkMapper:
    """Uses FLAME landmark displacement, not arbitrary PCA coefficient indices.

    Expression basis is decoded geometrically. Jaw-to-mouth is an approximation;
    no claim of full FLAME skinning or anatomical equivalence to the VRM is made.
    """
    def __init__(self):
        directory=CACHE/'EmoAva'/'src'/'data'
        with (directory/'generic_model.pkl').open('rb') as f:model=_ModelReader(f,encoding='latin1').load()
        embed=np.load(directory/'landmark_embedding.npy',allow_pickle=True,encoding='latin1').item()
        indices=np.asarray(embed['full_lmk_faces_idx']).reshape(-1)
        bary=np.asarray(embed['full_lmk_bary_coords']).reshape(-1,3)
        faces=model['f'][indices]
        self.template=np.einsum('lij,li->lj',model['v_template'][faces],bary)
        basis=model['shapedirs'].x[:,:,300:350]
        self.basis=np.einsum('lijk,li->ljk',basis[faces],bary)
        self.scale=float(np.linalg.norm(self.template[36]-self.template[45]))
        self.reference=self.features(self.template)

    @staticmethod
    def features(p):
        y=p[:,1]
        return np.array([
            (y[21]+y[22]-y[39]-y[42])/2,
            (y[17]+y[26]-y[36]-y[45])/2,
            ((y[37]+y[38]-y[40]-y[41])+(y[43]+y[44]-y[46]-y[47]))/4,
            (y[48]+y[54]-y[51]-y[57])/2,
            y[62]-y[66],
            np.linalg.norm(p[48]-p[54])
        ])

    def map(self,coefficients):
        coeff=np.asarray(coefficients,dtype=np.float32)
        if coeff.shape!=(53,) or not np.isfinite(coeff).all():raise ValueError('Invalid CTEG frame')
        p=self.template+np.einsum('ljk,k->lj',self.basis,coeff[:50])
        inner,outer,eye,smile,gap,width=(self.features(p)-self.reference)/self.scale
        clamp=lambda x:float(np.clip(x,0,.85))
        return {
            'browInnerUp':clamp(inner*9), 'browOuterUp':clamp(outer*9),
            'browDown':clamp(-inner*9), 'eyeWide':clamp(eye*12),
            'eyeSquint':clamp(-eye*9), 'mouthSmile':clamp(smile*12),
            'mouthFrown':clamp(-smile*12),
            'mouthOpen':clamp(gap*10+max(0,float(coeff[50]))*1.7),
            'mouthPucker':clamp(-width*8),
        }

class FaceEngine:
    def __init__(self):
        torch.set_num_threads(2)
        requested=os.environ.get('CHARACTER_FACE_DEVICE','auto').lower()
        if requested not in ('auto','cpu','cuda'):raise ValueError('CHARACTER_FACE_DEVICE must be auto, cpu or cuda')
        available=torch.cuda.is_available()
        # Leave room for the desktop renderer and the body model on a 6 GB GPU.
        enough=available and torch.cuda.mem_get_info()[0]>=2*1024**3
        self.device='cuda' if requested!='cpu' and enough else 'cpu'
        self.device_reason=None if self.device=='cuda' or requested=='cpu' else 'CUDA unavailable or less than 2 GiB free VRAM'
        if requested=='cuda' and self.device!='cuda':raise RuntimeError(self.device_reason)
        path=CACHE/'cteg-model.chkpt'
        allowed=[]
        for name in torch.serialization.get_unsafe_globals_in_checkpoint(path):
            if not name.startswith(('torch.nn.modules.','transformers.models.bert.')) and name not in ('argparse.Namespace','torch._C._nn.gelu','transformers.activations.GELUActivation'):
                raise ValueError('Unexpected checkpoint global')
            module,attr=name.rsplit('.',1);allowed.append(getattr(importlib.import_module(module),attr))
        with torch.serialization.safe_globals(allowed):checkpoint=torch.load(path,map_location='cpu',weights_only=True)
        opt=checkpoint['settings']
        encoder=BertModel(BertConfig(**{k:v for k,v in vars(opt.encoder_pretrained.config).items() if not k.startswith('_')}))
        opt.encoder_pretrained=encoder
        model=Transformer(opt.src_vocab_size,opt.trg_exp_dim,src_pad_idx=opt.src_pad_idx,trg_pad_idx=opt.trg_pad_idx,trg_emb_prj_weight_sharing=opt.proj_share_weight,d_k=opt.d_k,d_v=opt.d_v,d_model=opt.d_model,d_word_vec=opt.d_word_vec,d_inner=opt.d_inner_hid,n_layers=opt.n_layers,n_head=opt.n_head,dropout=opt.dropout,scale_emb=opt.scale_emb,cvae=opt.cvae,flat_method=opt.flat_method,feature_attention=opt.feature_attention,encoder_pretrained=encoder,one_step_loss=opt.one_step_loss,exp_only_dim=opt.exp_only_dim,src_len=opt.src_len,trg_len=opt.trg_len,fix_encoder=opt.fix_encoder,second_sample=opt.second_sample)
        model.load_state_dict(checkpoint['model']);del checkpoint
        original=model.encoder.forward
        def forward(*args,**kwargs):
            mask=kwargs.get('attention_mask')
            if mask is not None and mask.ndim==3:kwargs['attention_mask']=mask[:,0,:]
            return original(*args,**kwargs)
        model.encoder.forward=forward
        self.tokenizer=BertTokenizer.from_pretrained(CACHE/'bert-tokenizer',local_files_only=True)
        try:model.to(self.device)
        except torch.cuda.OutOfMemoryError:
            if requested!='auto':raise
            model.to('cpu');torch.cuda.empty_cache()
            self.device='cpu';self.device_reason='CUDA memory allocation failed during model loading'
        self.translator=TranslatorContinue(model,64,self.device,self.tokenizer,1.0,opt.cvae,str(CACHE/'EmoAva'/'dataset'))
        self.mapper=LandmarkMapper()
        self.length=opt.src_len

    def generate(self,text,seed,frames=62):
        tokens=self.tokenizer([text],max_length=self.length,padding='max_length',truncation=True,return_tensors='pt')
        src=tokens['input_ids'].to(self.device);mask=tokens['attention_mask'].to(self.device).bool().unsqueeze(-2)
        torch.manual_seed(seed)
        smooth=None;alpha=1-math.exp(-2*math.pi*2.5/24)
        with torch.inference_mode():
            enc,gen=self.translator._get_init_state(src,mask)
            for step in range(2,frames+2):
                dec=self.translator._model_decode(gen,enc,mask)
                value=dec[:,-2,:].unsqueeze(1)
                gen=torch.cat((gen[:,:-1,:],value,value.new_zeros(1,1,53)),dim=1)
                raw=self.mapper.map(value[0,0].cpu().numpy())
                smooth=raw if smooth is None else {k:smooth[k]+alpha*(v-smooth[k]) for k,v in raw.items()}
                yield smooth.copy()

def main():
    engine=FaceEngine()
    print(json.dumps({'type':'ready','model':'CTEG','device':engine.device,'device_reason':engine.device_reason}),flush=True)
    for line in sys.stdin:
        try:
            request=json.loads(line);started=time.perf_counter();batch=[];offset=0
            for frame in engine.generate(request['text'],request['seed']):
                batch.append(frame)
                if len(batch)==4:
                    print(json.dumps({'type':'batch','id':request['id'],'offset':offset,'frames':batch,'elapsed_ms':round((time.perf_counter()-started)*1000,2)}),flush=True)
                    offset+=len(batch);batch=[]
            if batch:print(json.dumps({'type':'batch','id':request['id'],'offset':offset,'frames':batch,'elapsed_ms':round((time.perf_counter()-started)*1000,2)}),flush=True)
            print(json.dumps({'type':'done','id':request['id'],'elapsed_ms':round((time.perf_counter()-started)*1000,2)}),flush=True)
        except Exception as exc:print(json.dumps({'type':'error','id':request.get('id') if 'request' in locals() else None,'error':str(exc)}),flush=True)

if __name__=='__main__':main()
