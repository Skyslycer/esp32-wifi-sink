"""Shairport 5.6.3 timestamped backend → server PCM → existing SBC path."""
import asyncio
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import socket
import time
import tempfile
from .timing import AUDIO_HEADER, PresentationTimeline, raw_ns
import xml.etree.ElementTree as ET

from audio.receiver import ReceiverPCM
from audio.sources import SourceError


class MetadataParser:
    MAX_ITEM = 65536
    MAX_DATA = 16384

    def __init__(self): self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        result = []
        while b'</item>' in self.buffer:
            end = self.buffer.index(b'</item>') + 7
            item = bytes(self.buffer[:end]).strip(); del self.buffer[:end]
            if len(item) > self.MAX_ITEM or b'<!' in item:
                raise SourceError('Invalid AirPlay metadata item')
            try:
                root = ET.fromstring(item)
                if root.tag != 'item': raise ValueError('wrong root')
                kind = int(root.findtext('type'),16).to_bytes(4,'big').decode('ascii')
                code = int(root.findtext('code'),16).to_bytes(4,'big').decode('ascii')
                length = int(root.findtext('length'))
                if not 0 <= length <= self.MAX_DATA: raise ValueError('metadata length')
                node = root.find('data')
                if node is not None and node.get('encoding') != 'base64': raise ValueError('metadata encoding')
                data = base64.b64decode(''.join((node.text or '').split()),validate=True) if node is not None else b''
                if len(data) != length: raise ValueError('metadata length mismatch')
                result.append((kind,code,data))
            except (ET.ParseError,ValueError,TypeError,OverflowError,UnicodeError) as exc:
                raise SourceError('Malformed AirPlay metadata') from exc
        if len(self.buffer) > self.MAX_ITEM: raise SourceError('AirPlay metadata exceeds bounds')
        return result


class AirPlayAudio(ReceiverPCM):
    kind = 'airplay'
    presentation_timed = True


class AirPlayReceiver:
    CHUNK = 4096
    QUEUE_CHUNKS = 16

    def __init__(self, session, binary, state_dir):
        self.session = session
        self.binary = str(Path(binary).resolve())
        self.directory = Path(state_dir).resolve() / session.device_id
        self.name = session.prefs['display_name'][:50]
        self.source = AirPlayAudio(self)
        self.queue = asyncio.Queue(self.QUEUE_CHUNKS)
        self.pending = bytearray()
        self.generation = 0
        self.track = {}
        self.sender_volume = None
        self.ready = False
        self.error = None
        self.stream_open = False
        self.suspended = False
        self.takeover_pending = False
        self.process = None
        self.tasks = []
        self.audio_fd = self.metadata_fd = None
        self.port = None
        self.queued_pcm_frames=0
        self.discarded_frames = 0
        self.timeline=PresentationTimeline()
        self.pending_position=0
        self.bridge_server=None;self.bridge_writer=None;self.bridge_serial=0
        self.bridge_connected=False;self.bridge_failure=None
        self.ipc_directory=None;self.audio_path=None;self.feedback_socket=None
        self.bridge_clients=set()

    def snapshot(self):
        fill = int(self.session.status.get('buffer_fill_frames',0))
        return {'enabled':True,'ready':self.ready,'name':self.name,'error':self.error,
                'protocol':'classic_audio','track':self.track,'stream_open':self.stream_open,
                'sender_controls':False,'discarded_idle_frames':self.discarded_frames,
                'buffered_pcm_frames':self.queued_pcm_frames+len(self.pending)//4,
                'timing':{**self.timeline.snapshot(),'bridge_connected':self.bridge_connected,
                          'esp_buffered_ms':round(fill*1000/44100,1)}}

    def flush(self):
        self.generation += 1
        self.pending.clear();self.queued_pcm_frames=0
        self.timeline.origin=None
        while not self.queue.empty(): self.queue.get_nowait()

    def suspend(self):
        self.suspended = True
        self.takeover_pending = False
        self.flush()

    async def control(self, action):
        # Foundation: local gating only. Do not claim to remotely pause iOS.
        if action == 'pause': self.suspend()
        elif action in ('play','resume'):
            self.suspended = False; self.takeover_pending = self.stream_open
        else: raise SourceError('AirPlay sender owns track controls')

    async def read(self, length):
        try:
            while len(self.pending) < length:
                data = await asyncio.wait_for(self.queue.get(),10)
                if data is None: raise SourceError('AirPlay receiver stopped')
                if isinstance(data,tuple):
                    position,data=data
                    self.queued_pcm_frames=max(0,self.queued_pcm_frames-len(data)//4)
                    if self.pending and position!=self.pending_position+len(self.pending)//4:
                        raise SourceError('AirPlay PCM queue lost continuity')
                    if not self.pending:self.pending_position=position
                    if self.timeline.origin is None:self.timeline.origin=position
                self.pending.extend(data)
        except TimeoutError as exc: raise SourceError('AirPlay stopped delivering audio') from exc
        result = bytes(self.pending[:length]); del self.pending[:length]
        self.pending_position+=length//4
        return result

    async def handle_metadata(self, kind, code, data):
        if kind == 'core':
            value = ''.join(c for c in data.decode('utf-8',errors='replace') if c.isprintable())[:512]
            if code == 'minm': self.track['name'] = value
            elif code == 'asar': self.track['artist_names'] = [value]
            elif code == 'asal': self.track['album_name'] = value
            return
        if kind != 'ssnc': return
        if code == 'pbeg':
            if self.bridge_connected:
                self.stream_open=True
                return  # ordered PREPARE owns activation and generation
            self.flush()
            self.stream_open = True; self.suspended = False; self.takeover_pending = True
        elif code in ('prsm','pres') and not self.suspended:
            self.stream_open = True
            if self.session.source is not self.source or self.session.desired != 'playing': self.takeover_pending = True
        elif code in ('pend','paus'):
            self.stream_open = code != 'pend'
            self.takeover_pending = False
            if self.session.source is self.source and self.session.desired == 'playing':
                await self.session.stop(pause=code == 'paus')
            self.flush()
        elif code == 'pfls':
            if self.bridge_connected:return  # ordered backend FLUSH is authoritative
            self.flush()
            # Check identity under the playback lock so a late flush cannot
            # reclaim the output after the user has selected another source.
            await self.session.resync_source(self.source)
        elif code == 'pvol':
            try: value = float(data.decode('ascii').split(',')[0])
            except (ValueError,UnicodeError): return
            if not math.isfinite(value) or (value != -144 and not -30 <= value <= 0): return
            self.sender_volume = 0 if value == -144 else (value+30)/30
            if self.session.source is self.source:
                await self.session.set_player_volume(self.sender_volume)

    async def activate(self):
        if self.suspended or not self.session.prefs.get("approved", False):
            self.takeover_pending = False
            return
        if not self.session.audio_output_ready(): return
        self.takeover_pending = False
        if self.sender_volume is not None: await self.session.set_player_volume(self.sender_volume)
        await self.session.play_source(self.source,duration=None)
        if not self.stream_open and self.session.source is self.source:
            await self.session.stop()

    async def accept_bridge(self,reader,writer):
        task=asyncio.current_task();self.bridge_clients.add(task)
        self.bridge_serial+=1;serial=self.bridge_serial
        if self.bridge_writer:self.bridge_writer.close()
        self.bridge_writer=writer
        sock=writer.get_extra_info('socket')
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8192)
        try:
            prepared=False
            while serial==self.bridge_serial:
                header=await reader.readexactly(AUDIO_HEADER.size)
                magic,version,kind,flags,generation,position,frames,rtp,pts,length=AUDIO_HEADER.unpack(header)
                if magic!=b'SAPT' or version!=1 or kind not in (1,2,3,4) or flags>1 or not generation:
                    raise SourceError('Invalid AirPlay timing bridge header')
                if kind!=1:
                    if frames or length or flags or pts:raise SourceError('Invalid AirPlay bridge control record')
                    if kind in (2,3):
                        if position or kind==3 and not prepared:raise SourceError('Invalid AirPlay bridge generation')
                        self.flush();self.timeline.reset(generation);self.bridge_connected=True;prepared=True
                        if kind==2:
                            self.stream_open=True;self.suspended=False;self.takeover_pending=True
                        elif self.session.source is self.source:
                            await self.session.resync_source(self.source)
                    elif prepared and generation==self.timeline.generation:
                        self.takeover_pending=False;self.stream_open=False
                        if self.session.source is self.source and self.session.desired=='playing':await self.session.stop()
                    else:raise SourceError('Stale AirPlay bridge control record')
                    continue
                if not prepared or generation!=self.timeline.generation or not 1<=frames<=1024 or length!=frames*4:
                    raise SourceError('Invalid AirPlay timed PCM bounds')
                data=await asyncio.wait_for(reader.readexactly(length),5)
                self.timeline.block(position,frames,rtp,pts,bool(flags))
                if self.takeover_pending:await self.activate()
                selected=self.session.source is self.source and self.session.desired=='playing' and not self.suspended
                local_generation=self.generation
                while self.queue.full() and selected:
                    await asyncio.sleep(.002)
                    selected=self.session.source is self.source and self.session.desired=='playing' and not self.suspended
                    if local_generation!=self.generation:break
                if selected and local_generation==self.generation:
                    self.queue.put_nowait((position,data));self.queued_pcm_frames+=frames
                else:self.discarded_frames+=frames
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:raise
        except Exception as exc:
            if self.bridge_failure and not self.bridge_failure.done():
                self.bridge_failure.set_exception(exc if isinstance(exc,SourceError) else SourceError(str(exc)))
        finally:
            self.bridge_clients.discard(task)
            if serial==self.bridge_serial:
                self.bridge_connected=False;self.timeline.feedback_valid=False
                if self.session.source is self.source and self.session.desired=='playing' and not self.session.closed:
                    with contextlib.suppress(Exception):await self.session.stop()
            writer.close()
            with contextlib.suppress(Exception):await writer.wait_closed()

    async def send_feedback(self):
        while True:
            await asyncio.sleep(.02)
            if not self.bridge_connected:continue
            packet=self.timeline.feedback(self.session,raw_ns())
            if packet:
                try:self.feedback_socket.sendto(packet,str(self.audio_path)+'.feedback')
                except (FileNotFoundError,ConnectionRefusedError,BlockingIOError):pass

    async def pump_metadata(self):
        parser = MetadataParser()
        while True:
            try: chunk = os.read(self.metadata_fd,8192)
            except BlockingIOError:
                await asyncio.sleep(.005); continue
            if not chunk:
                await asyncio.sleep(.01); continue
            for item in parser.feed(chunk): await self.handle_metadata(*item)

    @staticmethod
    def quoted(value):
        # libconfig supports environment expansion before parsing. Keep names literal.
        return json.dumps(str(value).replace('${','$${'),ensure_ascii=False)

    def configuration(self, audio, metadata):
        q = self.quoted
        return f'''general = {{
 name = {q(self.name)};
 port = {self.port};
 udp_port_base = 0;
 udp_port_range = 10;
 output_backend = "sink";
 mdns_backend = "avahi";
 ignore_volume_control = "yes";
}};
sink = {{
 name = {q(audio)};
 output_rate = 44100;
 output_format = "S16_LE";
 output_channels = 2;
 audio_backend_buffer_desired_length_in_seconds = 1.0;
}};
metadata = {{
 enabled = "yes";
 include_cover_art = "no";
 pipe_name = {q(metadata)};
}};
sessioncontrol = {{ session_timeout = 60; }};
'''

    async def wait_ready(self):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if self.process.returncode is not None: raise SourceError('AirPlay receiver exited; see private receiver.log')
            try:
                reader,writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1',self.port),1)
                try:
                    writer.write(b'OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n'); await writer.drain()
                    line = await asyncio.wait_for(reader.readline(),1)
                    if b'200' not in line: raise SourceError('AirPlay RTSP probe failed')
                finally:
                    writer.close(); await writer.wait_closed()
                self.ready = True
                return
            except OSError: await asyncio.sleep(.1)
        raise SourceError('AirPlay receiver startup timed out')

    async def run(self):
        try:
            self.directory.mkdir(parents=True,exist_ok=True,mode=0o700); self.directory.chmod(0o700)
            # Unix socket paths must fit sockaddr_un even when the workspace has
            # a long real path. The per-process temporary directory is private.
            self.ipc_directory=tempfile.TemporaryDirectory(prefix='sink-airplay-')
            audio=self.audio_path=Path(self.ipc_directory.name)/'pcm.sock'
            self.bridge_failure=asyncio.get_running_loop().create_future()
            self.bridge_server=await asyncio.start_unix_server(self.accept_bridge,path=audio,limit=8192)
            audio.chmod(0o600)
            self.feedback_socket=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
            self.feedback_socket.setblocking(False)
            metadata=self.directory/'metadata.fifo'
            metadata.unlink(missing_ok=True);os.mkfifo(metadata,0o600)
            self.metadata_fd = os.open(metadata,os.O_RDWR|os.O_NONBLOCK)
            with socket.socket() as sock:
                sock.bind(('0.0.0.0',0)); self.port = sock.getsockname()[1]
            path = self.directory/'receiver.conf'
            path.write_text(self.configuration(audio,metadata)); path.chmod(0o600)
            log_fd = os.open(self.directory/'receiver.log',os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
            try:
                self.process = await asyncio.create_subprocess_exec(self.binary,'--configfile',str(path),
                    stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=log_fd)
            finally: os.close(log_fd)
            self.tasks = [asyncio.create_task(self.send_feedback()),asyncio.create_task(self.pump_metadata()),asyncio.create_task(self.process.wait()),self.bridge_failure]
            await self.wait_ready()
            done,_ = await asyncio.wait(self.tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            raise SourceError('AirPlay receiver stopped')
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.error = str(exc) if isinstance(exc,SourceError) else 'AirPlay receiver failed'
        finally:
            cleanup=asyncio.create_task(self.cleanup())
            while not cleanup.done():
                try:await asyncio.shield(cleanup)
                except asyncio.CancelledError:pass
            cleanup.result()

    async def cleanup(self):
        self.ready = False
        if self.bridge_server:self.bridge_server.close()
        for client in list(self.bridge_clients):client.cancel()
        await asyncio.gather(*list(self.bridge_clients),return_exceptions=True)
        if self.bridge_writer:self.bridge_writer.close()
        if self.bridge_server:await self.bridge_server.wait_closed()
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(),3)
            except TimeoutError:
                self.process.kill(); await self.process.wait()
        for fd in (self.audio_fd,self.metadata_fd):
            if fd is not None: os.close(fd)
        self.audio_fd = self.metadata_fd = None
        if self.feedback_socket:self.feedback_socket.close()
        if self.ipc_directory:self.ipc_directory.cleanup()
        self.flush(); self.queue.put_nowait(None)
        if self.session.source is self.source and not self.session.closed:
            with contextlib.suppress(Exception): await self.session.stop()
