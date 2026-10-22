"""周期调度：只决定触发时刻，不解释业务数据。"""
import time
class PeriodicTask:
    """保持首次立即触发和每次执行后延迟的既有时序。"""
    def __init__(self, interval, clock=time.monotonic):
        self.interval=interval; self.clock=clock; self.next_at=0
    def due(self, force=False):
        now=self.clock()
        if not force and now < self.next_at: return False
        self.next_at=now+self.interval
        return True
