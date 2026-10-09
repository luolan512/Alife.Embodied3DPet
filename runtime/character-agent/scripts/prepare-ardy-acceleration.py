"""Build official ARDY TensorRT engines for a single desktop character."""
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'services'/'ardy-worker'),str(ROOT.parent/'ardy'/'scripts')]
from acceleration import dependencies
dependencies()
import torch,tensorrt as trt
from ardy.model.load_model import load_model
from export_onnx import export_denoiser_onnx,export_decoder_onnx,build_trt_engine,engine_path,verify_denoiser,verify_decoder
p=argparse.ArgumentParser();p.add_argument('--model',choices=['core40','core8'],default='core40');p.add_argument('--history',type=int,default=16)
args=p.parse_args()
frames=args.history+(40 if args.model=='core40' else 8)
if frames%4 or not 4<=frames<=200:raise ValueError('Window must be patch aligned and <=10 seconds')
folder=ROOT/'.cache'/'ardy-engines'/args.model;folder.mkdir(parents=True,exist_ok=True)
model,cfg=load_model(args.model,device='cuda:0',text_encoder=False,return_config=True)
denoiser=folder/'denoiser.onnx';decoder=folder/'decoder.onnx';tokens=frames//4
with torch.inference_mode():
 export_denoiser_onnx(model.denoiser,cfg,str(denoiser),num_tokens=tokens,num_text_tokens=1)
 export_decoder_onnx(model.autoencoder,str(decoder),num_tokens=tokens)
 dpath=engine_path(str(folder),'denoiser',tokens,True);apath=engine_path(str(folder),'decoder',tokens,True)
 if not Path(dpath).exists():build_trt_engine(str(denoiser),dpath,max_tokens=tokens,fp16=True,workspace_bytes=1<<30)
 if not Path(apath).exists():build_trt_engine(str(decoder),apath,max_tokens=tokens,fp16=True,workspace_bytes=1<<30)
 verify_denoiser(model.denoiser,dpath,4,num_tokens=tokens,num_text_tokens=1)
 verify_decoder(model.autoencoder,apath,4,num_tokens=tokens)
manifest={'model':args.model,'max_frames':frames,'trt_version':trt.__version__,'gpu':torch.cuda.get_device_name(),'denoiser':Path(dpath).name,'decoder':Path(apath).name,'created':time.time()}
(folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps(manifest))
