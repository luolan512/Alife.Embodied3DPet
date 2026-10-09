"""Check runtime imports without loading weights, downloading or changing packages."""
import importlib
import json
import platform
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'ardy'))
errors = []
if not (3, 10) <= sys.version_info[:2] <= (3, 12):
    errors.append('Use Python 3.10, 3.11 or 3.12; other versions are not validated.')
if sys.maxsize <= 2**32:
    errors.append('64-bit Python is required.')
for name in ('torch', 'numpy', 'scipy', 'yaml', 'einops', 'einx', 'hydra',
             'omegaconf', 'safetensors', 'vector_quantize_pytorch', 'transformers',
             'httpx', 'websockets.sync.server', 'pydantic', 'psutil', 'huggingface_hub'):
    try:
        importlib.import_module(name)
    except Exception as exc:
        errors.append(f'{name}: {exc}')
torch = sys.modules.get('torch')
print(json.dumps({'ready': not errors, 'python': platform.python_version(),
                  'executable': sys.executable, 'torch': getattr(torch, '__version__', None),
                  'cuda_available': bool(torch and torch.cuda.is_available()),
                  'errors': errors}, ensure_ascii=False))
raise SystemExit(1 if errors else 0)
