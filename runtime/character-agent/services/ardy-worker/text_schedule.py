"""Single encoder, priority admission and cooperative background preemption."""
import threading
import time


class TextPreparationPreempted(RuntimeError):
    pass


class TextScheduler:
    def __init__(self):
        self.condition=threading.Condition()
        self.pending=[]
        self.active=None
        self.sequence=0

    def encode(self,encoder,prompt,valid,priority=0,on_timing=None):
        started=time.monotonic()
        if not valid():raise RuntimeError('Motion preparation cancelled')
        if encoder.cache.contains(prompt):
            result=encoder([prompt])
            if on_timing:on_timing(queue_ms=0.,compute_ms=round((time.monotonic()-started)*1000,2),cache_hit=True)
            return result
        cancel=getattr(getattr(encoder,'encoder',None),'cancel_current',None)
        with self.condition:
            self.sequence+=1
            ticket={'priority':priority,'order':self.sequence,'prompt':prompt,
                    'preempted':threading.Event()}
            self.pending.append(ticket)
            try:
                while True:
                    if not valid():raise RuntimeError('Motion preparation cancelled')
                    first=min(self.pending,key=lambda t:(t['priority'],t['order']))
                    if self.active is None and first is ticket:
                        self.pending.remove(ticket);self.active=ticket
                        break
                    # Preserve useful work when foreground wants the same prompt.
                    # Different speculative work may yield to a current action.
                    active=self.active
                    if (cancel and active and active['priority']>priority
                            and active['prompt']!=prompt):active['preempted'].set()
                    self.condition.wait(.05)
            except BaseException:
                self.pending.remove(ticket);self.condition.notify_all()
                raise
        acquired=time.monotonic()
        finished=threading.Event();watcher=None
        try:
            if not valid():raise RuntimeError('Motion preparation cancelled')
            # Recheck after admission: another job may have warmed this exact text.
            hit=encoder.cache.contains(prompt)
            if cancel:
                def watch():
                    while not finished.wait(.05):
                        if ticket['preempted'].is_set() or not valid():cancel()
                watcher=threading.Thread(target=watch,daemon=True);watcher.start()
            try:result=encoder([prompt])
            except RuntimeError:
                if valid() and ticket['preempted'].is_set():raise TextPreparationPreempted('Background text yielded to current action')
                raise
            if not valid():raise RuntimeError('Motion preparation cancelled')
            if ticket['preempted'].is_set():raise TextPreparationPreempted('Background text yielded to current action')
            if on_timing:on_timing(queue_ms=round((acquired-started)*1000,2),
                                   compute_ms=round((time.monotonic()-acquired)*1000,2),cache_hit=hit)
            return result
        finally:
            # Stop cancellation before admitting a different job; a late cancel
            # must never hit the next prompt's encoder request.
            finished.set()
            if watcher:watcher.join()
            with self.condition:
                self.active=None;self.condition.notify_all()
