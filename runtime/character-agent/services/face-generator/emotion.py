"""Independent affect state. CTEG is motion detail, not the semantic controller."""
import math,time,unicodedata
DEFAULTS={'neutral':(0,.05,0),'happy':(.85,.55,.25),'sad':(-.75,-.5,-.5),'angry':(-.8,.8,.6),'relaxed':(.45,-.65,.05)}
FIELDS={'valence':(-1,1),'arousal':(-1,1),'dominance':(-1,1),'intensity':(0,1),'hold_s':(0,120),'decay_s':(2,300)}
def english(text):
    if not isinstance(text,str):return ''
    normalized=unicodedata.normalize('NFKD',text).replace('’',"'").replace('—','-')
    if any('\u3400'<=c<='\u9fff' for c in normalized):return ''
    return normalized.encode('ascii','ignore').decode().strip()[:300]

class EmotionState:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.updated=clock();self.values=self.resolve('neutral',{'intensity':0})
        self.label='neutral';self.version=0
    @staticmethod
    def resolve(label,state=None):
        if label not in DEFAULTS:raise ValueError('Invalid emotion')
        v,a,d=DEFAULTS[label];values={'valence':v,'arousal':a,'dominance':d,'intensity':.8,'hold_s':8,'decay_s':45,'mixture':{}}
        if state is not None:
            if not isinstance(state,dict) or set(state)-set(FIELDS)-{'mixture'}:raise ValueError('Invalid face_state fields')
            mix=state.get('mixture',{})
            if not isinstance(mix,dict) or set(mix)-set(DEFAULTS):raise ValueError('Invalid emotion mixture')
            if any(type(w) not in (int,float) or not math.isfinite(w) or not 0<=w<=1 for w in mix.values()):raise ValueError('Invalid emotion mixture weight')
            total=sum(mix.values())
            if mix and total<=0:raise ValueError('Emotion mixture needs positive weight')
            values['mixture']={k:w/total for k,w in mix.items()} if total else {}
            if total:
                for i,k in enumerate(('valence','arousal','dominance')):values[k]=sum(DEFAULTS[label][i]*w for label,w in values['mixture'].items())
            for k,value in state.items():
                if k=='mixture':continue
                lo,hi=FIELDS[k]
                if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Invalid face_state '+k)
                values[k]=float(value)
        return values
    def set(self,label,state=None):
        self.values=self.resolve(label,state);self.updated=self.clock();self.label=label;self.version+=1
        return self.packet()
    def packet(self):
        return {'type':'face_state','emotion':self.label,'version':self.version,'age_s':max(0,self.clock()-self.updated),**self.values}
    def snapshot(self):
        data=self.packet();elapsed=max(0,data['age_s']-data['hold_s'])
        data['effective_intensity']=data['intensity']*math.exp(-elapsed/data['decay_s'])
        return data
    def continuation_text(self,text):
        if not text and self.values['mixture']:
            return 'I feel '+' and '.join(k for k,w in self.values['mixture'].items() if w>.05)+' at the same time.'
        return english(text) or {'happy':'I feel happy and pleased.','sad':'I feel sad and disappointed.','angry':'I feel upset and annoyed.','relaxed':'I feel calm and comfortable.','neutral':'I am listening attentively.'}[self.label]
