"""Execution receipts, separate from model estimates and motion continuation."""
import copy
import time
import uuid

class PlanTimeline:
    def __init__(self):
        self.plans=[];self.records={};self.on_save=None

    def restore(self,plans):
        self.plans=plans[-30:]
        for plan in self.plans:
            for step in plan['steps']:
                if step['status'] in ('queued','preparing','running','looping'):
                    step['status']='interrupted';step['outcome']='service_restarted'
            self.update(plan)

    def update(self,plan):
        statuses=[s['status'] for s in plan['steps']]
        plan['status']='looping' if 'looping' in statuses else 'active' if any(s in ('queued','preparing','running') for s in statuses) else 'finished'
        if self.on_save:self.on_save(copy.deepcopy(plan))

    def bind(self,steps,goal,source='agent'):
        plan={'id':uuid.uuid4().hex,'goal':goal[:160],'source':source,'created_at':time.time(),'status':'active','steps':[]}
        for step in steps:
            receipt={'id':uuid.uuid4().hex,'action':step.action,'label':getattr(step,'label','') or step.action,
                     'prompt':step.motion_prompt[:400],'kind':getattr(step,'kind','one_shot'),
                     'duration_s':step.duration_s,'repeat':getattr(step,'repeat',1),'status':'queued'}
            plan['steps'].append(receipt);self.records[id(step)]=(step,plan,receipt)
        self.plans.append(plan);self.plans=self.plans[-30:]
        allowed={p['id'] for p in self.plans};self.records={k:v for k,v in self.records.items() if v[1]['id'] in allowed}
        self.update(plan)

    def mark(self,step,status,**values):
        item=self.records.get(id(step))
        if item is None:return
        _,plan,receipt=item;receipt.update(status=status,**values);self.update(plan)

    def reconcile(self,current,queue):
        alive={id(s) for s in queue}
        if current:alive.add(id(current))
        for key,(step,plan,receipt) in list(self.records.items()):
            if key not in alive and receipt['status'] in ('queued','preparing','running','looping'):
                self.mark(step,'removed',outcome='queue_changed')

    def snapshot(self,current,state,start):
        plans=copy.deepcopy(self.plans)
        item=self.records.get(id(current)) if current else None
        if item:
            for plan in plans:
                if plan['id']!=item[1]['id']:continue
                for receipt in plan['steps']:
                    if receipt['id']!=item[2]['id']:continue
                    elapsed=max(0,state.get('frame',0)-(start or 0))/max(1,state.get('fps',20)) if start is not None else 0
                    end=state.get('behavior_state',{}).get('event_end_frame')
                    total=max(.1,(end-start)/state.get('fps',20)) if start is not None and end is not None else current.duration_s*current.repeat
                    receipt.update(elapsed_s=round(elapsed,1),estimated_s=current.duration_s*current.repeat,
                                   playback_total_s=round(total,1),progress=min(1,elapsed/total),
                                   phase='preparing' if start is None else 'paused' if not state.get('playing') else 'preparing' if state.get('frame',0)<start
                                   else 'loop' if current.kind=='loop' else 'settling' if elapsed>current.duration_s*current.repeat else 'action',
                                   continuation='cached_idle' if state.get('behavior_state',{}).get('enabled') and end is not None and state.get('frame',0)>=end else 'ardy')
        return {'plans':plans,'current_step_id':item[2]['id'] if item else None}
