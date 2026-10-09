"""Validate packaged TMR assets against an existing real playback recording."""
from pathlib import Path
import sys,json,gzip,math
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/motion-monitor'))
from tmr_backend import TMRBackend
files=sorted((ROOT/'data/runs').glob('*/motion-observer-samples.jsonl.gz'))
if not files:raise SystemExit('Run the native motion validation first.')
with gzip.open(files[-1],'rt',encoding='utf-8') as f:samples=[json.loads(line) for line in f]
samples=samples[:40]
assert len(samples)>=10
backend=TMRBackend()
scores=backend.score([[s[k] for s in samples] for k in ('source','retarget','rendered')],[samples[0]['prompt']])
assert all(math.isfinite(row[0]) for row in scores)
result={'real_playback_samples':len(samples),'streams':['source','retarget','rendered'],'scores':scores,'finite':True}
(ROOT/'.desktop/tmr-assets-validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
