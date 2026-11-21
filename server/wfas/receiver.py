"""WFAS v2 unicast PCM receiver (44.1/48 kHz, stereo, security Off)."""
import asyncio
import contextlib
import socket
import struct
from audio.receiver import ReceiverPCM
from audio.sources import SourceError

HEADER = struct.Struct('!2sBBHI')

class WFASAudio(ReceiverPCM):
    kind = 'wfas'

class WFASReceiver:
    def __init__(self, session):
        self.session = session
        self.input_rate = getattr(session,"prefs",{}).get("wfas_sample_rate",44100)
        self.resampler = None
        self.source = WFASAudio(self)
        self.track = {'name': getattr(session,'prefs',{}).get('wfas_name','WFAS')}
        self.pending = bytearray()
        self.queue = asyncio.Queue(32)
        self.queued_pcm_frames = 0
        self.ready = False
        self.error = None
        self.suspended = False
        self.expected = None
        self.packet_frames = 240
        self.dropped_packets = 0
        self.concealed_frames = 0
        self.received_audio_packets = 0

    def snapshot(self):
        return dict(enabled=True, ready=self.ready, error=self.error,
                    input_sample_rate=self.input_rate, output_sample_rate=44100,
                    name=self.track["name"],received_audio_packets=self.received_audio_packets,
                    waiting_for_output=self.ready and not self.session.audio_output_ready(),
                    host=self.session.prefs.get('wfas_host'), port=self.session.prefs.get('wfas_port',9090),
                    buffered_pcm_frames=self.queued_pcm_frames+len(self.pending)//4,
                    dropped_packets=self.dropped_packets, concealed_frames=self.concealed_frames)

    def flush(self):
        if self.resampler: self.resampler.reset()
        self.pending.clear()
        self.queued_pcm_frames = 0
        while not self.queue.empty(): self.queue.get_nowait()

    def suspend(self):
        self.suspended = True
        self.flush()

    async def control(self, action):
        if action == 'pause': self.suspend()
        elif action in ('play','resume'):
            self.suspended = False
            await self.session.play_source(self.source, duration=None)
        else: raise SourceError('WFAS has no track controls')

    async def read(self, length):
        try:
            while len(self.pending) < length:
                data = await asyncio.wait_for(self.queue.get(), 5)
                self.queued_pcm_frames -= len(data)//4
                self.pending.extend(data)
        except TimeoutError as exc: raise SourceError('WFAS stopped delivering audio') from exc
        result = bytes(self.pending[:length])
        del self.pending[:length]
        return result

    def audio(self, data):
        if not 10 <= len(data) <= 1400: return None
        magic, version, flags, seq, position = HEADER.unpack_from(data)
        if magic != b'WF': return None
        if version != 2: raise SourceError('Incompatible WFAS version; update the sender')
        if flags & 2: raise SourceError('WFAS encryption is not supported; select security Off')
        if flags & ~1 or (len(data)-10)%4: return None
        pcm = data[10:]
        frames = len(pcm)//4
        if flags & 1:
            frames = frames or self.packet_frames
            pcm = bytes(frames*4)
        if not frames: return None
        gap = 0 if self.expected is None else (position-self.expected)&0xffffffff
        if gap >= 0x80000000:
            self.dropped_packets += 1
            return None
        # Conceal short losses only; a long discontinuity starts at live audio.
        if gap <= self.input_rate//10:
            pcm = bytes(gap*4)+pcm
            self.concealed_frames += gap
        else: self.flush()
        self.expected = (position+frames)&0xffffffff
        self.packet_frames = frames
        return pcm

    async def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            if self.input_rate == 48000:
                from audio.resample import StereoResampler
                self.resampler = StereoResampler(self.input_rate)
            elif self.input_rate != 44100: raise SourceError("Unsupported WFAS sample rate")
            await loop.sock_connect(sock, (self.session.prefs['wfas_host'], self.session.prefs.get('wfas_port',9090)))
            for _ in range(5):
                await loop.sock_sendall(sock, b'HELLO_FROM_CLIENT;v=2')
                try: reply = await asyncio.wait_for(loop.sock_recv(sock, 1401),1)
                except TimeoutError: continue
                if reply == b'WFAS_PENDING': continue
                if reply.startswith(b'HELLO_ACK;') and b'v=2' in reply.split(b';'): break
                raise SourceError('WFAS connection rejected: '+reply.decode('ascii',errors='replace')[:120])
            else: raise SourceError('WFAS handshake timed out')
            self.ready = True
            while True:
                data = await asyncio.wait_for(loop.sock_recv(sock,1401),3)
                if data == b'BYE': raise SourceError('WFAS sender disconnected')
                if data == b'PING': continue
                pcm = self.audio(data)
                if pcm is None: continue
                self.received_audio_packets += 1
                if not self.suspended and self.session.source is not self.source and self.session.audio_output_ready():
                    await self.session.play_source(self.source,duration=None)
                if self.suspended or self.session.source is not self.source: continue
                if self.resampler: pcm = self.resampler.process(pcm)
                if not pcm: continue
                while self.queue.full():
                    old = self.queue.get_nowait()
                    self.queued_pcm_frames -= len(old)//4
                    self.dropped_packets += 1
                self.queue.put_nowait(pcm)
                self.queued_pcm_frames += len(pcm)//4
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            self.ready = False
            with contextlib.suppress(OSError): sock.send(b'CLIENT_BYE')
            sock.close()
            self.flush()
            if self.resampler: self.resampler.close()
            if self.session.source is self.source and not self.session.closed:
                with contextlib.suppress(Exception): await self.session.stop()
