"""Shared image-request arbitration: active priority, one image task, bounded force events."""
import threading
import time
import uuid


class PerceptionScheduler:
    def __init__(self,trace=None,clock=time.monotonic):
        self.lock=threading.RLock();self.trace=trace;self.clock=clock
        self.active_owner=None;self.image_owner=None;self.last_started=-float('inf')
        self.force_event=None;self.force_count=0;self.min_image_interval_s=3

    def force_observe(self,reason,source='system'):
        with self.lock:
            self.force_count+=1
            self.force_event=dict(type='FORCE_OBSERVE',event_id=uuid.uuid4().hex,
                                  reason=str(reason)[:120],source=source,time_unix=time.time())
            event=dict(self.force_event)
        if self.trace:self.trace.record('perception_event',event)
        return event

    def force_pending(self):
        with self.lock:return dict(self.force_event) if self.force_event else None

    def consume_force(self,event_id):
        with self.lock:
            if self.force_event and self.force_event['event_id']==event_id:self.force_event=None

    def acknowledge_active(self,captured_at_unix):
        with self.lock:
            if self.force_event and self.force_event['time_unix']<=captured_at_unix:self.force_event=None

    def claim_background(self,request_id):
        with self.lock:
            if self.active_owner or self.image_owner or self.clock()-self.last_started<self.min_image_interval_s:return False
            self.image_owner=dict(kind='background',request_id=request_id);self.last_started=self.clock()
            return True

    def claim_active(self,request_id,valid,timeout_s=50):
        with self.lock:
            if self.active_owner and self.active_owner!=request_id:return False
            self.active_owner=request_id
        if self.trace:self.trace.record('perception_priority',dict(kind='active',request_id=request_id))
        deadline=time.monotonic()+timeout_s
        while time.monotonic()<deadline:
            if not valid():self.release(request_id);return False
            with self.lock:
                if not self.image_owner and self.clock()-self.last_started>=self.min_image_interval_s:
                    self.image_owner=dict(kind='active',request_id=request_id);self.last_started=self.clock()
                    return True
            time.sleep(.05)
        self.release(request_id)
        return False

    def release(self,request_id):
        with self.lock:
            if self.image_owner and self.image_owner['request_id']==request_id:self.image_owner=None
            if self.active_owner==request_id:self.active_owner=None

    def mark_image_started(self,request_id):
        with self.lock:
            if self.image_owner and self.image_owner['request_id']==request_id:self.last_started=self.clock()

    def snapshot(self):
        with self.lock:
            return dict(name='PerceptionScheduler',active_priority=True,active_owner=self.active_owner,
                        image_task=dict(self.image_owner) if self.image_owner else None,
                        force_event=dict(self.force_event) if self.force_event else None,
                        force_count=self.force_count,
                        cooldown_s=round(max(0,self.min_image_interval_s-(self.clock()-self.last_started)),2))
