"""Supervised go-librespot v0.10.7 receiver; private control API, paced PCM FIFO."""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import socket
import time

import httpx
from websockets.asyncio.client import connect
from audio.sources import SourceError
from audio.receiver import ReceiverPCM


class SpotifyAudio(ReceiverPCM):
    kind = 'spotify'


class SpotifyReceiver:
    CHUNK = 4096
    QUEUE_CHUNKS = 16

    def __init__(self, session, binary, state_dir):
        self.session = session
        self.binary = str(Path(binary).resolve())
        self.directory = Path(state_dir).resolve() / session.device_id
        self.source = SpotifyAudio(self)
        self.process = None
        self.fd = None
        self.tasks = []
        self.queue = asyncio.Queue(self.QUEUE_CHUNKS)
        self.pending = bytearray()
        self.generation = 0
        self.track = {}
        self.volume = session.prefs['volume']
        self.remote_playing = False
        self.ready = False
        self.error = None
        self.dropped_frames = 0
        self.client = None
        self.port = None
        self.name = session.prefs['display_name']

    def snapshot(self):
        return {'enabled': True, 'ready': self.ready, 'name': self.name,
                'error': self.error, 'track': self.track, 'dropped_frames': self.dropped_frames,
                'buffered_pcm_frames': self.queue.qsize() * 1024 + len(self.pending) // 4}

    def flush(self):
        self.generation += 1
        self.pending.clear()
        while not self.queue.empty(): self.queue.get_nowait()

    async def read(self, length):
        try:
            while len(self.pending) < length:
                chunk = await asyncio.wait_for(self.queue.get(), 10)
                if chunk is None: raise SourceError('Spotify receiver stopped')
                self.pending.extend(chunk)
        except TimeoutError as exc:
            raise SourceError('Spotify stopped delivering audio') from exc
        result = bytes(self.pending[:length]); del self.pending[:length]
        return result

    async def request(self, action, data=None):
        if not self.client: raise SourceError('Spotify receiver is not ready')
        try:
            response = await self.client.post('/player/' + action, json=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceError('Spotify control request failed') from exc

    async def control(self, action):
        if action == 'pause' and not self.remote_playing: return
        await self.request({'previous': 'prev', 'play': 'resume'}.get(action, action))
        if action == 'pause': self.remote_playing = False

    async def set_volume(self, volume):
        if abs(volume - self.volume) < .005: return
        await self.request('volume', {'volume': round(volume * 100)})
        self.volume = volume

    async def handle_event(self, event):
        kind, data = event.get('type'), event.get('data') or {}
        session = self.session
        if kind == 'metadata':
            # Never expose receiver credentials, account details or raw API state.
            self.track = {key: data[key] for key in ('uri', 'name', 'artist_names', 'album_name', 'album_cover_url', 'duration', 'position') if key in data}
        elif kind == 'playing':
            self.remote_playing = True
            if not session.prefs.get("approved", False) or not session.audio_output_ready():
                await self.control('pause')
                return
            if session.source is not self.source or session.desired != 'playing':
                await session.set_player_volume(self.volume, from_spotify=True)
                await session.play_source(self.source, duration=None)
        elif kind in ('paused', 'inactive', 'stopped'):
            self.remote_playing = False
            if session.source is self.source and session.desired == 'playing':
                await session.stop(pause=kind != 'stopped')
        elif kind == 'seek':
            await session.resync_source(self.source)
        elif kind == 'volume':
            maximum = data.get('max', 100)
            value = data.get('value')
            if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0 and 0 <= value <= maximum:
                self.volume = value / maximum
                if session.source is self.source:
                    await session.set_player_volume(self.volume, from_spotify=True)
        elif kind == 'playback_error':
            self.error = 'Spotify playback failed'

    async def pump(self):
        """Pace the pipe (whose writer has no clock) even while no sink consumes it."""
        loop = asyncio.get_running_loop()
        partial = bytearray()
        due = loop.time()
        generation = self.generation
        while True:
            if generation != self.generation:
                partial.clear(); generation = self.generation
            try:
                chunk = os.read(self.fd, self.CHUNK - len(partial))
            except BlockingIOError:
                await asyncio.sleep(.002)
                continue
            if not chunk:
                await asyncio.sleep(.01)
                continue
            partial.extend(chunk)
            if len(partial) < self.CHUNK: continue
            wait = due - loop.time()
            if wait > 0: await asyncio.sleep(wait)
            due = max(due, loop.time() - .024) + 1024 / 44100
            selected = self.session.source is self.source and self.session.desired == 'playing'
            while self.queue.full() and selected:
                # Bound memory by slowing the pipe writer, not by dropping active
                # audio or killing Connect during encoder startup/credit stalls.
                await asyncio.sleep(.002)
                selected = self.session.source is self.source and self.session.desired == 'playing'
                if generation != self.generation:
                    partial.clear(); generation = self.generation
                    break
            if not partial: continue
            if self.queue.full():
                self.queue.get_nowait()
                self.dropped_frames += 1024
            self.queue.put_nowait(bytes(partial)); partial.clear()

    async def events(self):
        # Reconnect only before the first API connection. A lost event channel
        # afterwards must stop the receiver rather than miss pause/seek events.
        deadline = time.monotonic() + 20
        while True:
            if self.process.returncode is not None: raise SourceError('Spotify receiver exited')
            try:
                async with connect(f'ws://127.0.0.1:{self.port}/events', max_size=65536, max_queue=32, proxy=None) as ws:
                    self.ready = True
                    response = await self.client.get('/status')
                    if response.status_code == 200:
                        status = response.json()
                        await self.handle_event({'type':'metadata', 'data':status.get('track') or {}})
                        if not status.get('stopped', True) and not status.get('paused', True):
                            await self.handle_event({'type':'playing'})
                    async for message in ws:
                        await self.handle_event(json.loads(message))
                    raise SourceError('Spotify event connection closed')
            except OSError:
                if self.ready or time.monotonic() >= deadline: raise
                await asyncio.sleep(.2)

    async def run(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            fifo = self.directory / 'audio.fifo'
            fifo.unlink(missing_ok=True)
            os.mkfifo(fifo, 0o600)
            self.fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
            config = {'device_id': hashlib.sha1(self.session.device_id.encode()).hexdigest(),
                      'device_name': self.name, 'device_type': 'speaker', 'audio_backend': 'pipe',
                      'audio_output_pipe': str(fifo), 'audio_output_pipe_format': 's16le',
                      'external_volume': True, 'volume_steps': 100,
                      'initial_volume': round(self.volume * 100), 'ignore_last_volume': True,
                      'bitrate': 320, 'flac_enabled': False, 'mpris_enabled': False,
                      'zeroconf_enabled': True, 'zeroconf_backend': 'builtin',
                      'credentials': {'type': 'zeroconf', 'zeroconf': {'persist_credentials': True}},
                      'server': {'enabled': True, 'address': '127.0.0.1', 'port': self.port},
                      'log_level': 'warn'}
            path = self.directory / 'config.yml'
            path.write_text(json.dumps(config)); path.chmod(0o600)  # JSON is valid YAML.
            self.client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{self.port}', timeout=5, trust_env=False)
            log_path = self.directory / 'receiver.log'
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                self.process = await asyncio.create_subprocess_exec(self.binary, '--config_dir', str(self.directory),
                            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                            stderr=log_fd)
            finally: os.close(log_fd)
            self.tasks = [asyncio.create_task(self.pump()), asyncio.create_task(self.events()), asyncio.create_task(self.process.wait())]
            done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            raise SourceError('Spotify receiver stopped')
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, SourceError) else 'Spotify receiver failed to start or connect'
        finally:
            self.ready = False
            for task in self.tasks: task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try: await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    self.process.kill(); await self.process.wait()
            if self.client: await self.client.aclose()
            if self.fd is not None: os.close(self.fd); self.fd = None
            self.flush(); self.queue.put_nowait(None)
            if self.session.source is self.source and not self.session.closed:
                with contextlib.suppress(Exception): await self.session.stop()
