"""Clock mapping and framed local Shairport timing bridge v1.

All host times are CLOCK_MONOTONIC_RAW nanoseconds, as used by Shairport on Linux.
Output timing is an estimate unless the physical output latency is calibrated.
"""
from collections import deque
import math
import struct
import time

AUDIO_HEADER=struct.Struct('!4sBBHQQIIQI')
FEEDBACK=struct.Struct('!4sHHQQqQII')
RATE=44100

def raw_ns():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)

class ClockMapper:
    def __init__(self):
        self.samples=deque(maxlen=128)
        self.offset=0.;self.skew=0.;self.origin=0
        self.updated=0;self.uncertainty_ns=math.inf

    def add(self, sent, received, device_us):
        if received<sent or received-sent>250_000_000 or device_us<0:return False
        device=device_us*1000
        self.samples.append((device,(sent+received)/2-device,received-sent,received))
        recent=[s for s in self.samples if received-s[3]<=120_000_000_000]
        best=min(s[2] for s in recent if received-s[3]<=10_000_000_000)
        chosen=[s for s in recent if s[2]<=best+2_000_000]
        if not chosen:return False
        self.origin=chosen[-1][0]
        self.skew=0.
        mean_x=sum(s[0]-self.origin for s in chosen)/len(chosen)
        mean_y=sum(s[1] for s in chosen)/len(chosen)
        span=max(s[0] for s in chosen)-min(s[0] for s in chosen)
        if len(chosen)>=8 and span>=20_000_000_000:
            den=sum((s[0]-self.origin-mean_x)**2 for s in chosen)
            slope=sum((s[0]-self.origin-mean_x)*(s[1]-mean_y) for s in chosen)/den if den else 0
            if abs(slope)<=.002:self.skew=slope
        self.offset=mean_y-self.skew*mean_x
        residuals=sorted(abs(s[1]-(self.offset+self.skew*(s[0]-self.origin))) for s in chosen)
        self.uncertainty_ns=best/2+residuals[len(residuals)//2]+100_000
        self.updated=received
        return True

    def valid(self, now):
        return len(self.samples)>=3 and 0<=now-self.updated<3_000_000_000 and self.uncertainty_ns<30_000_000

    def map(self, device_us):
        value=device_us*1000
        return round(value+self.offset+self.skew*(value-self.origin))

    def snapshot(self,now=None):
        now=raw_ns() if now is None else now
        return {'ready':self.valid(now),'samples':len(self.samples),
                'uncertainty_ms':None if not math.isfinite(self.uncertainty_ns) else round(self.uncertainty_ns/1e6,3),
                'skew_ppm':round(self.skew*1e6,2),'age_ms':None if not self.updated else round((now-self.updated)/1e6,1)}

class PresentationTimeline:
    def __init__(self):self.reset(0)
    def reset(self,generation):
        self.generation=generation;self.next_frame=0;self.origin=None
        self.anchors=deque(maxlen=2048)
        self.phase_ms=None;self.feedback_valid=False;self.latency_source='unknown'
        self.sync_offset_ms=0
        self.output_latency_ms=None;self.feedback_count=0;self.uncertainty_ms=None

    def block(self,position,frames,rtp,pts,timed):
        if position!=self.next_frame or not 1<=frames<=1024:raise ValueError('Discontinuous AirPlay PCM bridge')
        self.next_frame+=frames
        if timed:
            if not pts:raise ValueError('Timed AirPlay block has no presentation time')
            self.anchors.append((position,position+frames,pts,rtp))

    def expected_time(self,frame):
        for start,end,pts,_ in reversed(self.anchors):
            if start<=frame<end:return pts+(frame-start)*1e9/RATE
        return None

    def feedback(self,session,now):
        self.phase_ms=None;self.feedback_valid=False
        state=session.status
        matching=state.get('playback_id')==session.playback_id and state.get('epoch')==session.epoch
        if (self.origin is None or session.source is not session.airplay.source or
                session.desired!='playing' or not matching or not state.get('active') or
                not session.output_clock.valid(now) or now-session.output_feedback_raw>250_000_000):return None
        try:
            committed=int(state['output_position']);batch=int(state['output_batch_frames'])
            device_time=int(state['output_time_us'])
            if committed<0 or not 0<=batch<=896 or device_time<0:raise ValueError('Invalid output timing')
        except (ValueError,TypeError,KeyError):return None
        output_kind=session.hello.get('output',{}).get('kind')
        override=session.prefs.get('airplay_output_latency_ms')
        bt=state.get('bluetooth',{})
        if output_kind=='bluetooth_a2dp' and bt.get('speaker_delay_valid'):
            latency=float(bt['speaker_delay_ms']);self.latency_source='speaker_report'
        elif override is not None:
            latency=float(override);self.latency_source='calibrated'
        else:
            latency=120. if output_kind=='bluetooth_a2dp' else 12.
            self.latency_source='estimated'
        # Source external queue packets beyond the just-accepted batch.
        if output_kind=='bluetooth_a2dp':latency+=max(0,int(bt.get('sbc',{}).get('q',0))-1)*batch*1000/RATE
        self.output_latency_ms=latency
        self.sync_offset_ms=float(session.prefs.get("airplay_sync_offset_ms",0))
        # A positive user adjustment means sound later. Reporting less backend
        # delay makes Shairport deliver later, without changing physical latency.
        latency-=self.sync_offset_ms
        if committed and device_time:
            age=max(0,now-session.output_clock.map(device_time))
            frame=min(committed,committed-batch-latency*RATE/1000+age*RATE/1e9)
        else:frame=-latency*RATE/1000
        frame+=self.origin
        running=bool(state.get('playout_started')) and committed>0
        expected=self.expected_time(frame)
        if expected is not None:self.phase_ms=(now-expected)/1e6
        self.feedback_valid=True;self.feedback_count+=1
        self.uncertainty_ms=session.output_clock.uncertainty_ns/1e6
        return FEEDBACK.pack(b'SAPF',1,1|int(running)*2,self.generation,now,round(frame),now+100_000_000,RATE,
                             min(0xffffffff,round(self.uncertainty_ms*1000)))

    def snapshot(self):
        calibrated=self.latency_source!='estimated' and self.latency_source!='unknown'
        return {'presentation_clock':True,'av_sync_supported':True,
                'state':(('locked' if calibrated else 'estimated_lock') if self.phase_ms is not None and abs(self.phase_ms)<30 else 'correcting') if self.feedback_valid else 'waiting_for_feedback',
                'estimated_error_ms':None if self.phase_ms is None else round(self.phase_ms,2),
                'output_latency_ms':self.output_latency_ms,'sync_offset_ms':self.sync_offset_ms,'latency_source':self.latency_source,
                'physical_latency_verified':self.latency_source=='calibrated','clock_uncertainty_ms':self.uncertainty_ms,
                'feedback_count':self.feedback_count,'independent_tempo':False}
