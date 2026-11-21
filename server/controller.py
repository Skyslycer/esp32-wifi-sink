import asyncio
from collections import deque
import contextlib
import json
import logging
import math
import time
from uuid import UUID, uuid4

from audio.sources import TestTone, SourceError
from audio.sbc import SBCEncoder
from audio.gain import GainRamp
from audio.buffering import AdaptiveBuffer, source_group
from audio.tempo import TempoPCM
from airplay.timing import ClockMapper, raw_ns
from protocol.sbc import SBCFrame, PROFILE as SBC_PROFILE
from protocol.frames import Frame, MessageType as T, read_frame
from registry import Registry

log = logging.getLogger('sink')


def decode_json(data):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result: raise ValueError('duplicate JSON key')
            result[k] = v
        return result
    obj = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                     parse_constant=lambda s: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    def depth(value, level=0):
        if level > 8: raise ValueError('JSON nesting limit')
        if isinstance(value, float) and not math.isfinite(value): raise ValueError('nonfinite JSON')
        if isinstance(value, dict):
            for v in value.values(): depth(v, level+1)
        elif isinstance(value, list):
            for v in value: depth(v, level+1)
    depth(obj)
    if not isinstance(obj, dict): raise ValueError('JSON object required')
    return obj


class CommandError(Exception):
    def __init__(self, code, detail=None):
        self.code = code
        super().__init__(detail or code)


class Session:
    def __init__(self, controller, reader, writer, hello):
        self.controller, self.reader, self.writer, self.hello = controller, reader, writer, hello
        self.device_id = hello['device_id']
        self.connection_id = str(uuid4())
        capabilities=hello.get('capabilities',{})
        if (capabilities.get('sbc_audio') != SBC_PROFILE or hello.get('buffer_capacity_frames') != 65536 or
            capabilities.get('adaptive_buffer') is not True or capabilities.get('output_timing') != 1):
            raise ValueError("Current SBC profile, 65536-frame capacity, adaptive buffering and output timing are required")
        self.sbc_encoder = None
        self.gain_ramp = GainRamp()
        self.tempo_pcm = None
        self.buffering = AdaptiveBuffer(0)
        self.output_clock=ClockMapper()
        self.output_feedback_raw=0
        self.buffer_feedback_at = 0
        self.applied_buffer_frames = 65536
        self.tx = 0; self.rx = 1
        self.writer_lock = asyncio.Lock()
        self.playback_lock = asyncio.Lock()
        self.pending = {}
        self.status = {}
        self.last_seen = time.monotonic()
        self._persisted_seen = self.last_seen
        self.events = deque(maxlen=128)
        self.remote_controls = asyncio.Queue(maxsize=8)
        self.discovered = {}
        self.wake = asyncio.Event()
        self.closed = False
        saved = controller.player_desires.get(self.device_id, {})
        self.desired = saved.get('state', 'stopped')
        self.source = saved.get('source')
        if saved.get('deadline') is not None and saved['deadline'] <= time.monotonic(): self.desired = 'stopped'
        self.playback_id = None
        self.epoch = 0
        self.position = 0
        self.sent_frames = 0
        self.packets_sent = 0
        self.bytes_sent = 0
        self.reset_delivery_metrics()
        self.started_at = time.monotonic()
        self.last_error = None
        self.source_retries = 0
        self.device_volume_active = False
        self.volume_lock = asyncio.Lock()
        self.spotify = None
        self.spotify_task = None
        self.airplay = None
        self.airplay_task = None
        self.wfas = self.wfas_task = None
        self.stream_task = None
        self.timer_task = None

    def remember_speaker(self, address, name):
        import re
        if not isinstance(address,str) or not re.fullmatch(r'[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}',address): return
        if not isinstance(name,str) or not name.strip(): return
        names=dict(self.prefs.get('bluetooth_names',{}))
        address=address.lower();name=name.strip()[:128]
        if names.get(address)==name:return
        if address not in names and len(names)>=64:return
        names[address]=name
        self.controller.registry.update_debounced(self.device_id,bluetooth_names=names)

    def reset_delivery_metrics(self):
        self.max_source_read_ms=0.0
        self.max_sbc_encode_wait_ms=0.0
        self.max_audio_send_ms=0.0
        self.max_flow_interarrival_ms=0.0
        self.max_audio_send_interarrival_ms=0.0
        self.max_credit_wait_ms=0.0
        self.max_event_loop_lag_ms=0.0
        self._last_flow_at=None
        self._last_audio_send_at=None
        self._credit_wait_at=None

    def timing_active(self):
        return self.desired=='playing' and self.position>=16384 and bool(self.status.get('active'))

    async def timing_monitor(self):
        while True:
            due=time.monotonic()+.02
            await asyncio.sleep(.02)
            if self.timing_active():
                self.max_event_loop_lag_ms=max(self.max_event_loop_lag_ms,max(0,time.monotonic()-due)*1000)

    @property
    def prefs(self): return self.controller.registry.get(self.device_id)

    async def send(self, kind, obj):
        payload = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(',', ':'), allow_nan=False).encode()
        async with self.writer_lock:
            if self.closed or self.tx >= 0xfffffffe: raise ConnectionError('closed session')
            self.tx += 1
            self.writer.write(Frame(kind, self.tx, payload).encode())
            await asyncio.wait_for(self.writer.drain(), 2)

    async def command(self, command, args=None, timeout=40):
        ident = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send(T.COMMAND, {'connection_id': self.connection_id, 'request_id': ident,
                                       'command': command, 'args': args or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def run(self):
        output=self.hello.get("output",{})
        self.remember_speaker(output.get("connected_address") or output.get("active_address"),output.get("connected_name"))
        await self.send(T.HELLO, {'version': 1, 'connection_id': self.connection_id,
                        'server_clock_id': self.controller.clock_id, **self.prefs,
                        'audio_codec':SBC_PROFILE,
                        'heartbeat_interval_ms': 5000, 'heartbeat_timeout_ms': 15000,
                        })
        self.stream_task = asyncio.create_task(self.stream_loop())
        heartbeat = asyncio.create_task(self.heartbeat())
        timing = asyncio.create_task(self.timing_monitor())
        remote = asyncio.create_task(self.remote_control_loop())
        clock = asyncio.create_task(self.output_clock_loop())
        if self.prefs.get('spotify_enabled'): self.start_spotify()
        if self.prefs.get('airplay_enabled'): self.start_airplay()
        if self.prefs.get('wfas_enabled'): self.start_wfas()
        deadline = self.controller.player_desires.get(self.device_id, {}).get('deadline')
        if deadline and self.desired == 'playing':
            self.timer_task = asyncio.create_task(self.auto_stop(deadline)) if deadline is not None else None
        try:
            while True:
                frame = await read_frame(self.reader)
                if frame.sequence != self.rx + 1: raise ValueError('sequence mismatch')
                self.rx = frame.sequence
                obj = decode_json(frame.payload)
                if obj.get('connection_id') != self.connection_id: raise ValueError('stale connection')
                self.last_seen = time.monotonic()
                if self.last_seen-self._persisted_seen >= 30:
                    self.controller.registry.update_debounced(self.device_id,last_seen_at=time.time())
                    self._persisted_seen=self.last_seen
                if frame.kind in (T.FLOW, T.STATUS):
                    if frame.kind==T.FLOW:
                        now=time.monotonic()
                        if self.timing_active() and self._last_flow_at is not None:
                            self.max_flow_interarrival_ms=max(self.max_flow_interarrival_ms,(now-self._last_flow_at)*1000)
                        self._last_flow_at=now
                    self.status.update(obj)
                    bt=obj.get('bluetooth',{})
                    self.remember_speaker(bt.get('connected_address') or bt.get('active_address'),bt.get('connected_name'))
                    if frame.kind==T.FLOW and "output_time_us" in obj:self.output_feedback_raw=raw_ns()
                    if (frame.kind == T.FLOW and self.buffering.policy.group and
                        obj.get('playback_id') == self.playback_id and obj.get('epoch') == self.epoch):
                        self.buffer_feedback_at = time.monotonic()
                        self.buffering.observe(self.buffer_feedback_at,
                            int(obj['buffer_fill_frames']), int(obj['min_fill_frames']),
                            int(obj['underruns']), bool(obj['playout_started']), float(obj['delivery_gap_us'])/1000)
                    self.wake.set()
                elif frame.kind == T.ACK:
                    fut = self.pending.get(obj.get('request_id'))
                    if fut and not fut.done() and not obj.get('accepted'):
                        fut.set_exception(CommandError(obj.get('error', {}).get('code', 'REJECTED')))
                elif frame.kind == T.EVENT:
                    event = {**obj, 'device_id': self.device_id, 'received_at': time.time()}
                    self.events.append(event)
                    if obj.get('event') == 'REMOTE_CONTROL' and obj.get('detail') in ('play','pause','next','previous'):
                        try: self.remote_controls.put_nowait(obj['detail'])
                        except asyncio.QueueFull: log.warning('Remote control queue full for %s', self.device_id)
                    if obj.get('event') == 'BT_VOLUME_CHANGED' and self.device_volume_active and self.volume_command() == 'BLUETOOTH_SET_VOLUME':
                        try: self.controller.registry.update_debounced(self.device_id,volume=int(obj.get('detail',''))/127)
                        except ValueError: pass
                    if obj.get('event') == 'BT_DEVICE_FOUND':
                        address, _, name = obj.get('detail', '').partition(' ')
                        self.discovered[address] = {'address': address, 'name': name}
                        self.remember_speaker(address,name)
                    if obj.get('event') == 'COMMAND_COMPLETED':
                        fut = self.pending.get(obj.get('request_id'))
                        if fut and not fut.done():
                            if obj.get('success'): fut.set_result(obj.get('result'))
                            else: fut.set_exception(CommandError(obj.get('error', {}).get('code', 'FAILED')))
                elif frame.kind == T.ERROR:
                    if obj.get('code') == 'STALE_STREAM': continue
                    self.last_error = obj
                    self.events.append(obj)
                elif frame.kind != T.HEARTBEAT: raise ValueError('unexpected message type')
        finally:
            self.closed = True
            tasks=[task for task in (self.stream_task,heartbeat,timing,remote,clock,self.timer_task,self.spotify_task,self.airplay_task,self.wfas_task) if task is not None]
            for task in tasks: task.cancel()
            cleanup=asyncio.gather(*tasks, return_exceptions=True)
            # A simultaneous peer disconnect/server shutdown must not cancel
            # encoder cleanup a second time and orphan a child process.
            while not cleanup.done():
                try:await asyncio.shield(cleanup)
                except asyncio.CancelledError:pass
            for fut in self.pending.values():
                if not fut.done(): fut.set_exception(ConnectionError('device disconnected'))
            if self.source: await self.source.close()
            if self.source and self.source.kind in ('spotify', 'airplay', 'wfas'): self.controller.player_desires.pop(self.device_id, None)
            self.writer.close()
            with contextlib.suppress(Exception): await asyncio.wait_for(self.writer.wait_closed(),3)

    async def output_clock_loop(self):
        iteration=0
        while True:
            try:
                sent=raw_ns()
                report=await self.command('GET_OUTPUT_TIMING',timeout=2)
                received=raw_ns()
                self.output_clock.add(sent,received,int(report['device_time_us']))
            except (ConnectionError,CommandError,TimeoutError,KeyError,TypeError,ValueError):pass
            iteration+=1
            await asyncio.sleep(.1 if iteration<8 else 1)

    async def remote_control_loop(self):
        # Keep reading device replies while a button action awaits its command.
        while True:
            action = await self.remote_controls.get()
            try:
                await self.handle_remote_control(action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Remote control failed for %s (%s)', self.device_id, type(exc).__name__)

    async def handle_remote_control(self, action):
        if self.closed or not self.prefs.get("approved", False): return
        if self.source and self.source.kind == 'spotify' and action in ('next', 'previous'):
            await self.source.transport(action)
            return
        if action == 'play':
            if self.desired != 'playing' and self.source: await self.resume()
        elif action == 'pause':
            if self.desired == 'playing': await self.stop(pause=True)
        elif action in ('next', 'previous') and self.source and self.source.kind == 'radio':
            favorites = getattr(self.controller, 'favorites', None)
            stations = list(favorites.items) if favorites else []
            if not stations: return
            current = self.source.station.get('id')
            index = next((i for i, station in enumerate(stations) if station['id'] == current), None)
            target = ((index + (1 if action == 'next' else -1)) % len(stations)
                      if index is not None else (0 if action == 'next' else len(stations)-1))
            if stations[target]['id'] != current:
                await self.play_source(self.controller.radio_factory(stations[target]['id']), duration=None)

    async def heartbeat(self):
        try:
            while True:
                await self.send(T.HEARTBEAT, {'connection_id': self.connection_id})
                await asyncio.sleep(5)
        except (ConnectionError, OSError, TimeoutError): self.writer.close()

    def audio_output_ready(self):
        return self.status.get('output_ready',False)

    def assignment_needed(self):
        return not self.status.get('active') or self.status.get('playback_id')!=self.playback_id

    def volume_command(self):
        if self.hello.get('output', {}).get('kind') == 'i2s_pcm5102':
            return 'SET_VOLUME'
        if self.status.get('bluetooth', {}).get('absolute_volume_supported'):
            return 'BLUETOOTH_SET_VOLUME'
        return None

    async def sync_output_volume(self):
        # Synchronize persisted output gain before delivering any encoded audio.
        # The HTTP slider shares this lock so startup cannot overwrite a new value.
        async with self.volume_lock:
            command = self.volume_command()
            if command and not self.device_volume_active:
                await self.command(command, {'volume': round(self.prefs['volume'] * 100)})
                self.device_volume_active = True

    async def set_player_volume(self, volume, from_spotify=False):
        async with self.volume_lock:
            command = self.volume_command()
            if command:
                target = round(volume * 100)
                initial = round(self.prefs['volume'] * 100)
                steps = min(8, max(1, abs(target-initial))) if command == 'BLUETOOTH_SET_VOLUME' and self.device_volume_active else 1
                started = time.monotonic()
                for step in range(1, steps+1):
                    await self.command(command, {'volume': round(initial+(target-initial)*step/steps)})
                    if step < steps:
                        await asyncio.sleep(max(0, started+.15*step/steps-time.monotonic()))
            self.device_volume_active = command is not None
            result = self.controller.registry.update(self.device_id, volume=volume)
            if not from_spotify and self.source and self.source.kind == 'spotify' and self.spotify and self.spotify.ready:
                await self.spotify.set_volume(volume)
            return result

    def start_spotify(self):
        factory = getattr(self.controller, 'spotify_factory', None)
        if not self.prefs.get("approved", False) or not factory or (self.spotify_task and not self.spotify_task.done()): return
        self.spotify = factory(self)
        self.spotify_task = asyncio.create_task(self.spotify.run())

    async def disable_spotify(self):
        if self.source and self.source.kind == 'spotify':
            await self.stop()
            self.source = None
            self.controller.player_desires[self.device_id] = {'state': 'stopped', 'source': None}
        if self.spotify_task:
            self.spotify_task.cancel()
            await asyncio.gather(self.spotify_task, return_exceptions=True)
        self.spotify_task = self.spotify = None

    def start_wfas(self):
        if not self.prefs.get("approved", False) or not self.prefs.get("wfas_allowed",True) or (self.wfas_task and not self.wfas_task.done()): return
        from wfas.receiver import WFASReceiver
        self.wfas = WFASReceiver(self)
        self.wfas_task = asyncio.create_task(self.wfas.run())

    async def disable_wfas(self):
        if self.source and self.source.kind == 'wfas':
            await self.stop()
            self.source = None
            self.controller.player_desires[self.device_id] = {'state':'stopped','source':None}
        if self.wfas_task:
            self.wfas_task.cancel()
            await asyncio.gather(self.wfas_task, return_exceptions=True)
        self.wfas = self.wfas_task = None

    def start_airplay(self):
        factory = getattr(self.controller, 'airplay_factory', None)
        if not self.prefs.get("approved", False) or not factory or (self.airplay_task and not self.airplay_task.done()): return
        self.airplay = factory(self)
        self.airplay_task = asyncio.create_task(self.airplay.run())

    async def disable_airplay(self):
        if self.source and self.source.kind == 'airplay':
            await self.stop()
            self.source = None
            self.controller.player_desires[self.device_id] = {'state': 'stopped', 'source': None}
        if self.airplay_task:
            self.airplay_task.cancel()
            await asyncio.gather(self.airplay_task, return_exceptions=True)
        self.airplay_task = self.airplay = None

    async def read_source_packet(self, frames, position=None):
        started=time.monotonic()
        pcm=await self.source.read_pcm(self.position if position is None else position,frames,1)
        if len(pcm)!=frames*4:raise SourceError('Source returned an incomplete PCM packet')
        prefs=self.prefs
        station=getattr(self.source,'station',{})
        try:station=self.controller.favorites.get(station['id'])
        except (AttributeError,KeyError):pass
        delta=prefs.get('volume_delta_db',0)+prefs.get('source_deltas_db',{}).get(self.source.kind,0)+station.get('volume_delta_db',0)
        base=1 if self.device_volume_active else prefs['volume']
        target=min(64 if self.source.kind in ('radio','spotify','airplay','wfas') else 1,base*10**(delta/20))
        pcm=self.gain_ramp.process(pcm,target)
        read_ms=(time.monotonic()-started)*1000
        if self.position>=16384:
            self.max_source_read_ms=max(self.max_source_read_ms,read_ms)
            if read_ms>200:log.warning('PCM source stall device=%s position=%d duration_ms=%.1f',self.device_id,self.position,read_ms)
        return pcm

    async def stream_loop(self):
        try:
            while True:
                self.wake.clear()
                wait=.1
                if self.source and getattr(self.source, 'is_live', False) and not self.audio_output_ready():
                    await self.close_sbc_encoder()
                    await self.source.close()
                if not self.volume_command(): self.device_volume_active=False
                if self.desired == 'playing' and self.prefs.get('approved', False) and self.source and self.audio_output_ready():
                    await self.sync_output_volume()
                    if self.assignment_needed():
                        self.playback_id = str(uuid4()); self.epoch += 1
                        if self.epoch >= 0xffffffff: raise ConnectionError('delivery epoch exhausted')
                        await self.close_sbc_encoder()
                        self.reset_delivery_metrics()
                        self.position = 0; self.sent_frames = 0
                        group = source_group(self.source.kind)
                        self.buffering = AdaptiveBuffer(group, time.monotonic())
                        self.buffer_feedback_at = 0
                        self.applied_buffer_frames = self.buffering.frames
                        result = await self.command('AUDIO_START', {
                            'playback_id': self.playback_id, 'epoch': self.epoch,
                            'next_sample_position': '0',
                            **({'buffer_target_frames':self.buffering.frames} if group else {}),
                            **({'clocked':True} if getattr(self.source,'presentation_timed',False) else {})})
                        if result: self.status.update(result)
                    if self.status.get('playback_id') == self.playback_id and self.status.get('epoch') == self.epoch:
                        if self.buffering.frames != self.applied_buffer_frames:
                            target = self.buffering.frames
                            await self.command('SET_BUFFER_TARGET', {'playback_id':self.playback_id,
                                'epoch':self.epoch, 'buffer_target_frames':target})
                            self.applied_buffer_frames = target
                        grant = int(self.status.get('grant_total_frames', '0'))
                        if not 0 <= grant - self.sent_frames <= self.hello['buffer_capacity_frames']:
                            raise ValueError('invalid flow grant')
                        if grant-self.sent_frames>=1024 and self._credit_wait_at is not None:
                            if self.timing_active():self.max_credit_wait_ms=max(self.max_credit_wait_ms,(time.monotonic()-self._credit_wait_at)*1000)
                            self._credit_wait_at=None
                        while self.desired == 'playing' and min(grant, grant-65536+self.applied_buffer_frames) - self.sent_frames >= 1024:
                            if not self.sbc_encoder:
                                reader = self.read_source_packet
                                if self.buffering.policy.group and not getattr(self.source,'presentation_timed',False):
                                    self.tempo_pcm = TempoPCM(reader,self.playback_tempo)
                                    reader = self.tempo_pcm.read_pcm
                                self.sbc_encoder=SBCEncoder(reader, lookahead=4096 if self.buffering.policy.group else 8192)
                            encode_started=time.monotonic()
                            encoded=await self.sbc_encoder.read()
                            if self.timing_active():self.max_sbc_encode_wait_ms=max(self.max_sbc_encode_wait_ms,(time.monotonic()-encode_started)*1000)
                            send_started = time.monotonic()
                            if self.timing_active() and self._last_audio_send_at is not None:
                                self.max_audio_send_interarrival_ms=max(self.max_audio_send_interarrival_ms,(send_started-self._last_audio_send_at)*1000)
                            self._last_audio_send_at=send_started
                            packet = SBCFrame(UUID(self.playback_id),self.epoch,self.position,encoded).encode()
                            await self.send(T.SBC, packet)
                            self.max_audio_send_ms = max(self.max_audio_send_ms, (time.monotonic()-send_started)*1000)
                            self.position += 1024; self.sent_frames += 1024
                            self.packets_sent += 1; self.bytes_sent += len(encoded)
                            self.source_retries = 0
                        if grant-65536+self.applied_buffer_frames-self.sent_frames<1024 and self._credit_wait_at is None:self._credit_wait_at=time.monotonic()
                try: await asyncio.wait_for(self.wake.wait(), wait)
                except TimeoutError: pass
        except asyncio.CancelledError: raise
        except SourceError as exc:
            await self.close_sbc_encoder()
            self.last_error = {'code': 'SOURCE_FAILURE', 'message': str(exc)}
            if self.source: await self.source.close()
            with contextlib.suppress(ConnectionError, CommandError, TimeoutError):
                await self.command('AUDIO_STOP', timeout=5)
            self.playback_id = None
            if self.source and getattr(self.source, 'retryable', False) and self.desired == 'playing' and not self.closed:
                self.source_retries += 1
                self.stream_task = asyncio.create_task(self.retry_source(self.source, min(30, 2**min(self.source_retries-1,5))))
            else:
                self.desired = 'stopped'
                self.controller.player_desires[self.device_id] = {'state': 'stopped', 'source': self.source}
        except CommandError as exc:
            self.last_error = {'code': exc.code, 'message': str(exc)}
            self.desired = 'stopped'
        except (OSError, ValueError, ConnectionError, TimeoutError) as exc:
            self.last_error = {'code': 'STREAM_FAILURE', 'message': str(exc)}
            self.writer.close()
        finally:
            await self.close_sbc_encoder()

    def playback_tempo(self):
        consumed = int(self.status.get('grant_total_frames',65536))-65536
        receiver = getattr(self.source,'receiver',None)
        queued = receiver.snapshot().get('buffered_pcm_frames',0) if receiver else 0
        return self.buffering.tempo(max(0,self.sent_frames-consumed), queued,
                                    time.monotonic()-self.buffer_feedback_at)

    async def close_sbc_encoder(self):
        encoder,self.sbc_encoder=self.sbc_encoder,None
        try:
            if encoder:await encoder.close()
        finally:
            if self.tempo_pcm: self.tempo_pcm.close(); self.tempo_pcm=None
            self.gain_ramp=GainRamp()

    async def retry_source(self, source, delay):
        await asyncio.sleep(delay)
        if not self.closed and self.desired == 'playing' and self.source is source:
            self.stream_task = asyncio.create_task(self.stream_loop())

    async def auto_stop(self, deadline):
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        if not self.closed:
            with contextlib.suppress(ConnectionError, CommandError, TimeoutError): await self.stop()

    async def quiesce_source(self, close=True):
        if self.stream_task and self.stream_task is not asyncio.current_task():
            self.stream_task.cancel()
            await asyncio.gather(self.stream_task, return_exceptions=True)
            self.stream_task = None
        if close and self.source: await self.source.close()

    async def play_source(self, source, duration=10):
        async with self.playback_lock:
            await self._play_source(source, duration)

    async def resync_source(self, source):
        async with self.playback_lock:
            if self.source is source and self.desired == 'playing':
                await self._play_source(source, duration=None)

    async def _play_source(self, source, duration=10):
        if not self.prefs.get("approved", False): raise CommandError("SETUP_APPROVAL_REQUIRED")
        self.desired = 'stopped'
        if self.timer_task: self.timer_task.cancel()
        await self.quiesce_source(close=False)
        if self.source and self.source.kind in ('airplay','wfas') and self.source is not source:
            self.source.receiver.suspend()
        if self.source and self.source.kind == 'spotify' and self.source is not source and self.source.receiver.ready:
            with contextlib.suppress(SourceError): await self.source.transport('pause')
        try:
            await self.command('AUDIO_STOP')
        finally:
            if self.source: await self.source.close()
        if source.kind in ('spotify','airplay','wfas'): source.receiver.flush()
        self.source = source
        self.playback_id=None
        self.reset_delivery_metrics()
        self.last_error = None
        self.source_retries = 0
        if source.kind == 'radio':
            self.controller.registry.update(self.device_id, station_id=source.station['id'] if not source.station['id'].startswith('preview-') else None, playback_enabled=not source.station['id'].startswith('preview-'))
        else:
            self.controller.registry.update(self.device_id, playback_enabled=False)
        self.desired = 'playing'
        deadline = time.monotonic() + duration if duration is not None else None
        self.controller.player_desires[self.device_id] = {'state': self.desired, 'source': self.source, 'deadline': deadline}
        self.timer_task = asyncio.create_task(self.auto_stop(deadline)) if deadline is not None else None
        if self.stream_task is None or self.stream_task.done(): self.stream_task = asyncio.create_task(self.stream_loop())
        self.wake.set()

    async def play_tone(self, frequency=440, duration=10):
        await self.play_source(TestTone(frequency), duration)

    async def stop(self, pause=False):
        async with self.playback_lock:
            await self._stop(pause)

    async def _stop(self, pause=False):
        if self.timer_task and self.timer_task is not asyncio.current_task(): self.timer_task.cancel()
        self.desired = 'paused' if pause else 'stopped'
        if self.source and self.source.kind in ('airplay','wfas'): self.source.receiver.suspend()
        self.controller.registry.update(self.device_id, playback_enabled=False)
        await self.quiesce_source(close=False)
        self.controller.player_desires[self.device_id] = {'state': self.desired, 'source': self.source}
        try:
            if self.source and self.source.kind == 'spotify' and self.source.receiver.ready:
                with contextlib.suppress(SourceError): await self.source.transport('pause')
            await self.command('AUDIO_PAUSE' if pause else 'AUDIO_STOP')
        finally:
            if self.source: await self.source.close()
        self.playback_id = None

    async def resume(self, duration=10):
        if not self.source: raise CommandError('NO_SOURCE')
        if self.source.kind in ('spotify','airplay','wfas'):
            await self.source.transport('play')
            return
        await self.play_source(self.source, None if self.source.kind=='radio' else duration)

    def snapshot(self,event_limit=128):
        return {'device_id': self.device_id, **self.prefs, 'online': not self.closed,
                'last_seen_at':time.time()-max(0,time.monotonic()-self.last_seen),
                'connection_id': self.connection_id, 'hello': self.hello, 'diagnostics': self.status,
                'spotify': self.spotify.snapshot() if self.spotify else {'enabled': False, 'available': bool(getattr(self.controller, 'spotify_factory', None))},
                'wfas': self.wfas.snapshot() if self.wfas else {'enabled':False,'available':True},
                'airplay': self.airplay.snapshot() if self.airplay else {'enabled': False, 'available': bool(getattr(self.controller, 'airplay_factory', None))},
                'player': {'state': self.desired, 'source': self.source.kind if self.source else None,
                           'playback_id': self.playback_id, 'epoch': self.epoch,
                           'packets_sent': self.packets_sent, 
                           'audio_bytes_sent':self.bytes_sent,
                           'sbc_bytes_sent':self.bytes_sent,
                           'max_sbc_encode_wait_ms':round(self.max_sbc_encode_wait_ms,2),
                           'audio_transport':'tcp',
                           'audio_codec':SBC_PROFILE,
                           'max_source_read_ms': round(self.max_source_read_ms, 2),
                           'max_audio_send_ms': round(self.max_audio_send_ms, 2),
                           **{key:round(getattr(self,key),2) for key in ('max_flow_interarrival_ms','max_audio_send_interarrival_ms','max_credit_wait_ms','max_event_loop_lag_ms')},
                           'station': getattr(self.source,'station',None),
                           'stream_title': getattr(self.source,'stream_title',None),
                           'recovery_attempts': self.source_retries,
                           'buffering': {**self.buffering.snapshot(),
                               'requested_group':source_group(self.source.kind) if self.source else 0,
                               'supported':True,
                               'clock_locked_mode':bool(getattr(self.source,'presentation_timed',False))},
                           'output_clock':self.output_clock.snapshot()},
                'last_error': self.last_error, 'events': list(self.events)[-event_limit:],
                'bluetooth_devices': list(self.discovered.values())}


class Controller:
    def __init__(self, registry: Registry):
        self.registry = registry
        self.sessions = {}
        self.player_desires = {}
        self.clock_id = str(uuid4())
        self.tcp_server = None
        self.handlers = set()
        self.radio_factory = None

    async def accept(self, reader, writer):
        handler = asyncio.current_task()
        self.handlers.add(handler)
        session = None
        try:
            if len(self.sessions) >= 16: raise ValueError('live connection limit')
            frame = await read_frame(reader, timeout=5)
            hello = decode_json(frame.payload)
            if frame.kind != T.HELLO or frame.sequence != 1 or hello.get('protocol') != 1 or hello.get('versions') != [1]:
                raise ValueError('invalid HELLO')
            UUID(hello['boot_id'])
            prefs = self.registry.get(hello['device_id'])
            if hello.get('output', {}).get('kind') not in ('bluetooth_a2dp', 'i2s_pcm5102'):
                raise ValueError('unsupported output')
            existing = self.sessions.get(hello['device_id'])
            if existing: raise ValueError('duplicate device connection')
            if prefs.get('approved') and hello['device_id'] not in self.player_desires and prefs.get('playback_enabled') and prefs.get('station_id') and self.radio_factory:
                try: self.player_desires[hello['device_id']] = {'state':'playing','source':self.radio_factory(prefs['station_id'])}
                except (ValueError,KeyError): self.registry.update(hello['device_id'],playback_enabled=False)
            session = Session(self, reader, writer, hello)
            self.registry.update(session.device_id,output_type=hello['output']['kind'],last_seen_at=time.time())
            self.sessions[session.device_id] = session
            log.info('Device online: %s (%s)', session.device_id, hello['output']['kind'])
            await session.run()
        except (OSError, ValueError, KeyError, TypeError, TimeoutError, asyncio.IncompleteReadError) as exc:
            log.info('Device transport closed: %s', type(exc).__name__)
        finally:
            if session:
                self.registry.update(session.device_id,last_seen_at=time.time()-max(0,time.monotonic()-session.last_seen))
                session.closed = True
                if self.sessions.get(session.device_id) is session:
                    self.sessions.pop(session.device_id)
            writer.close()
            with contextlib.suppress(Exception): await asyncio.wait_for(writer.wait_closed(),3)
            self.handlers.discard(handler)

    async def start(self, host='0.0.0.0', port=8765):
        self.tcp_server = await asyncio.start_server(self.accept, host, port, limit=16384, backlog=16)

    async def close(self):
        if self.tcp_server:
            self.tcp_server.close()
        for session in list(self.sessions.values()): session.writer.close()
        handlers = list(self.handlers)
        for task in handlers: task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        if self.tcp_server: await self.tcp_server.wait_closed()
        await self.registry.flush()
