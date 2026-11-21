"""One bounded FFmpeg SBC encoder per playback assignment; input gain stays upstream."""
import asyncio
import contextlib
from protocol.sbc import FRAME_BYTES, validate_frame
from .sources import SourceError

class SBCEncoder:
    def __init__(self,read_pcm,lookahead=8192):
        self.read_pcm=read_pcm
        if lookahead not in (4096,8192): raise ValueError("invalid encoder lookahead")
        self.lookahead=lookahead
        self.process=None
        self.feeder=None
        self.errors=None
        self.failure=None
        self.output_position=0
        self.room=asyncio.Event()
        self.closing=None

    async def start(self):
        self.process=await asyncio.create_subprocess_exec(
            'ffmpeg','-nostdin','-hide_banner','-loglevel','error','-filter_threads','1',
            '-thread_queue_size','1','-avioflags','direct','-blocksize','4096',
            '-probesize','32','-analyzeduration','0','-f','s16le','-ar','44100','-ac','2','-i','pipe:0',
            '-c:a','sbc','-threads','1','-b:a','320k','-flush_packets','1','-f','sbc','pipe:1',
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,limit=8192)
        self.process.stdin.transport.set_write_buffer_limits(high=8192,low=4096)
        self.feeder=asyncio.create_task(self.feed())
        self.errors=asyncio.create_task(self.drain_errors())

    async def drain_errors(self):
        while await self.process.stderr.read(1024):pass

    async def feed(self):
        pos=0
        try:
            while True:
                self.room.clear()
                if pos-self.output_position>=self.lookahead:
                    await self.room.wait()
                    continue
                pcm=await self.read_pcm(1024,pos)
                self.process.stdin.write(pcm)
                await self.process.stdin.drain()
                pos+=1024
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.failure=exc
            self.process.stdin.close()

    async def read(self):
        if not self.process:await self.start()
        try:data=await asyncio.wait_for(self.process.stdout.readexactly(8*FRAME_BYTES),15)
        except (asyncio.IncompleteReadError,TimeoutError) as exc:
            raise SourceError('SBC encoder stopped producing audio') from (self.failure or exc)
        for i in range(0,len(data),FRAME_BYTES):validate_frame(data[i:i+FRAME_BYTES])
        self.output_position+=1024
        self.room.set()
        return data

    async def close(self):
        if self.closing is None:self.closing=asyncio.create_task(self._close())
        cancelled=False
        while not self.closing.done():
            try:await asyncio.shield(self.closing)
            except asyncio.CancelledError:cancelled=True
        self.closing.result()
        if cancelled:raise asyncio.CancelledError

    async def _close(self):
        process=self.process
        if not process:return
        if self.feeder:self.feeder.cancel()
        process.stdin.close()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):process.terminate()
        async def drain():
            while await process.stdout.read(8192):pass
        task=asyncio.create_task(drain())
        try:await asyncio.wait_for(process.wait(),2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):process.kill()
            await process.wait()
        await asyncio.gather(task,self.errors,self.feeder,return_exceptions=True)
        self.process=None
