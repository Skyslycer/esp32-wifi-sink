"""Bounded, cancellable FFmpeg-to-PCM input shared by live sources."""
import array
import asyncio
import contextlib
import sys
from .sources import AudioSource, SourceError


class FFmpegPCM(AudioSource):
    is_live = True
    ffmpeg_log_level = 'error'

    def __init__(self):
        self.process = None
        self.stderr_task = None
        self.stderr_tail = b''
        self.frames_read = 0

    async def input_arguments(self):
        raise NotImplementedError('source must define FFmpeg input arguments')

    async def start(self):
        if self.process is not None: return
        arguments = await self.input_arguments()
        self.stderr_tail = b''
        try:
            self.process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-nostdin', '-nostats', '-hide_banner', '-loglevel', self.ffmpeg_log_level,
                *arguments, '-vn', '-ac', '2', '-ar', '44100',
                '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1',
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=8192)
        except OSError as exc:
            raise SourceError('Could not start FFmpeg') from exc
        self.stderr_task = asyncio.create_task(self._drain_errors(self.process))

    async def _drain_errors(self, process):
        while True:
            data = await process.stderr.read(1024)
            if not data: return
            self.stderr_tail = (self.stderr_tail + data)[-4096:]
            self.handle_stderr(data)

    def handle_stderr(self, data):
        pass

    async def read_pcm(self, position: int, frames: int, gain: float) -> bytes:
        if not 1 <= frames <= 1024 or not 0 <= gain <= 64:
            raise ValueError('invalid PCM bounds')
        await self.start()
        try:
            raw = await asyncio.wait_for(self.process.stdout.readexactly(frames * 4), 12 if not self.frames_read else 5)
        except (asyncio.IncompleteReadError, TimeoutError) as exc:
            process = self.process
            await self.close()
            code = process.returncode if process else None
            raise SourceError(f'FFmpeg did not produce PCM (exit {code})') from exc
        self.frames_read += frames
        if gain == 1:
            return raw
        samples = array.array('h', raw)
        if sys.byteorder != 'little': samples.byteswap()
        for i, value in enumerate(samples): samples[i] = max(-32768,min(32767,round(value * gain)))
        if sys.byteorder != 'little': samples.byteswap()
        return samples.tobytes()

    async def close(self):
        process, self.process = self.process, None
        self.frames_read = 0
        if process is None: return
        async def drain():
            while await process.stdout.read(8192): pass
        drain_task = asyncio.create_task(drain())
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError): process.terminate()
            try: await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError): process.kill()
                await process.wait()
        try: await asyncio.wait_for(drain_task, 2)
        except TimeoutError:
            drain_task.cancel()
        if self.stderr_task:
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None
        await asyncio.gather(drain_task, return_exceptions=True)
