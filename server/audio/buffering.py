"""Finite-memory delay controller. Units are milliseconds, never lifetime minima.

The estimator consumes one-second worst drawdowns rather than every FLOW, so
report frequency does not change its statistics.
"""
from collections import deque
from dataclasses import dataclass
import math

RATE = 44100
CAPACITY = 65536

@dataclass(frozen=True)
class Policy:
    group: int
    floor: float
    initial: float
    margin: float
    memory: float
    hold: float
    decrease: float
    max_speed_change: float

POLICIES = {
    0: Policy(0, CAPACITY*1000/RATE, CAPACITY*1000/RATE, 0, 0, 0, 0, 0),
    1: Policy(1, 600, 900, 200, 180, 60, 5, .005),
    2: Policy(2, 160, 400, 80, 60, 20, 15, .02),
}

def source_group(kind):
    return {'spotify': 1, 'airplay': 2, 'wfas': 2}.get(kind, 0)

class AdaptiveBuffer:
    def __init__(self, group, now=0):
        self.policy = POLICIES[group]
        self.target_ms = self.policy.initial
        self.history = deque(maxlen=240)
        self.bucket_at = now
        self.bucket_peak = 0.0
        self.hold_until = now + self.policy.hold
        self.last_update = now
        self.last_underruns = None
        self.lowest_ms = None
        self.armed = False
        self.speed = 1.0
        self.underrun_events = 0

    @property
    def frames(self):
        # Whole network packets ensure the sender can satisfy the prefill.
        return min(CAPACITY, math.ceil(self.target_ms*RATE/1000/1024)*1024)

    def observe(self, now, fill_frames, minimum_frames, underruns, started, delivery_gap_ms=0):
        p = self.policy
        if not p.group: return
        if (not 0 <= fill_frames <= CAPACITY or not 0 <= minimum_frames <= CAPACITY or
                underruns < 0 or not math.isfinite(delivery_gap_ms) or delivery_gap_ms < 0):
            raise ValueError('invalid adaptive buffer feedback')
        fill = fill_frames*1000/RATE
        minimum = minimum_frames*1000/RATE
        self.lowest_ms = minimum
        dt = max(0, min(2, now-self.last_update))
        self.last_update = now
        if self.last_underruns is None: self.last_underruns = underruns
        failed = underruns > self.last_underruns
        self.last_underruns = underruns
        # Only infer drawdown after reaching the current target. Startup and a
        # deliberate drain must not be mistaken for a network stall.
        if fill >= self.frames*1000/RATE - 50: self.armed = True
        if started and self.armed:
            self.bucket_peak = max(self.bucket_peak, max(0, self.frames*1000/RATE-minimum))
        self.bucket_peak = max(self.bucket_peak, max(0, delivery_gap_ms-1024*1000/RATE))
        if failed:
            self.history.append((now,max(self.bucket_peak,self.target_ms)))
            self.underrun_events += 1
            self.bucket_peak = max(self.bucket_peak, self.target_ms)
            self.target_ms = min(CAPACITY*1000/RATE, self.target_ms + max(p.margin, self.target_ms*.5))
            self.hold_until = now+p.hold
            self.armed = False
        while self.history and now-self.history[0][0] > p.memory: self.history.popleft()
        if now-self.bucket_at >= 1:
            if (self.armed and started) or self.bucket_peak > 0:
                self.history.append((now, self.bucket_peak))
            self.bucket_at = now
            self.bucket_peak = 0
        # A finite-window maximum is deliberately more protective than NetEq's
        # speech-oriented 95th percentile. One rare TCP stall matters for music.
        need = max(p.floor, max((v for _,v in self.history), default=0)+p.margin)
        need = min(CAPACITY*1000/RATE, need)
        if need > self.target_ms:
            self.target_ms = need
            self.hold_until = now+p.hold
            self.armed = False
        elif now >= self.hold_until and started and not failed:
            self.target_ms = max(need, self.target_ms-p.decrease*dt)
        return self.frames

    def tempo(self, queued_frames, receiver_frames, feedback_age):
        p = self.policy
        if not p.group: return 1.0
        # The unencoded receiver backlog must participate: otherwise reducing
        # the ESP queue merely moves the same delay into the server's FIFO.
        error = (queued_frames-self.frames + max(0, receiver_frames-1024))*1000/RATE
        desired = 1.0 if feedback_age > .25 else 1 + max(-p.max_speed_change, min(p.max_speed_change, error/10000))
        # Slew per PCM block (23ms): no abrupt ratio steps.
        self.speed += max(-.0002, min(.0002, desired-self.speed))
        return self.speed

    def snapshot(self):
        return {'group':self.policy.group, 'target_frames':self.frames,
                'target_ms':round(self.frames*1000/RATE, 1),
                'recent_min_fill_ms':None if self.lowest_ms is None else round(self.lowest_ms,1),
                'history_seconds':self.policy.memory, 'history_windows':len(self.history),
                'underrun_events':self.underrun_events, 'tempo':round(self.speed,6)}
