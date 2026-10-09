"""Real local face stream: speech ends during inference and continuation stays silent."""
import json,time,urllib.request
from pathlib import Path
from websockets.sync.client import connect
import os
BASE='http://127.0.0.1:'+os.environ.get('PET_TEST_PORT','8096')
session=json.load(urllib.request.urlopen(BASE+'/api/v1/session'))
def api(path,data=None):
    req=urllib.request.Request(BASE+'/api/v1/'+path,data=None if data is None else json.dumps(data).encode(),headers={'Authorization':'Bearer '+session['token'],'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req,timeout=30))
with connect(f"ws://127.0.0.1:{session['ws_port']}/?token={session['token']}&observer=1") as ws:
    api('face',{'enabled':True,'continuous':True,'guidance':True})
    started=time.monotonic()
    result=api('face-test',{'text':'I am happy to see you!','emotion':'happy','speaking_s':1,'seed':42,'state':{'hold_s':30,'decay_s':45}})
    stop_latency=None;renewed=None
    deadline=time.monotonic()+55
    while time.monotonic()<deadline:
        p=json.loads(ws.recv(timeout=max(.1,deadline-time.monotonic())))
        if p['type']=='face_speech' and p.get('duration_s')==0 and p['id']==result['id']:
            stop_latency=time.monotonic()-started
        if p['type']=='face_speech' and p['id']>result['id']:assert p['duration_s']==0
        if p['type']=='face_batch' and p['id']>result['id']:
            assert p['continuation'] and p['speaking_s']==0;renewed=p['id'];break
    assert stop_latency is not None and .95<=stop_latency<1.3,stop_latency
    assert renewed is not None
    record={'speech_duration_s':1,'authoritative_stop_s':round(stop_latency,3),'continuation_id':renewed,'continuation_silent':True}
    (Path(__file__).resolve().parents[1]/'.run'/'pet-face-validation.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(record,indent=2))
