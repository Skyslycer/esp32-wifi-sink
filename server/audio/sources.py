"""Standard PCM sources. Every sink owns its source and sample timeline."""
import array
import math
import sys
from abc import ABC, abstractmethod

RATE = 44100


class SourceError(Exception):
    pass


class AudioSource(ABC):
    @abstractmethod
    async def read_pcm(self, position: int, frames: int, gain: float) -> bytes:
        """Return signed 16-bit LE stereo PCM at 44.1 kHz."""

    async def close(self):
        """Release input resources; generated/memory sources own none."""
        return None


class TestTone(AudioSource):
    kind = "test-tone"
    def __init__(self, frequency: float = 440):
        if not 20 <= frequency <= 18000:
            raise ValueError('test frequency must be 20..18000 Hz')
        self.frequency = frequency

    async def read_pcm(self, position: int, frames: int, gain: float) -> bytes:
        return self.read(position, frames, gain)

    def read(self, position: int, frames: int, gain: float) -> bytes:
        if not 0 <= gain <= 1 or not 1 <= frames <= 1024:
            raise ValueError('invalid audio bounds')
        result = array.array('h')
        # Source peak is 15% full scale; default 10% gain gives a quiet 1.5% peak.
        for i in range(position, position + frames):
            sample = round(4915 * gain * math.sin(2 * math.pi * self.frequency * i / RATE))
            result.extend((sample, sample))
        if sys.byteorder != 'little': result.byteswap()
        return result.tobytes()


class PCMClip(AudioSource):
    kind = 'bench-music'

    def __init__(self, path):
        from pathlib import Path
        raw = Path(path).read_bytes()
        if not raw or len(raw) % 4 or len(raw) > 20 * RATE * 4:
            raise ValueError('bench PCM must be 1..20 seconds of 44.1 kHz stereo s16le')
        self.samples = array.array('h', raw)
        if sys.byteorder != 'little': self.samples.byteswap()
        self.frames = len(self.samples) // 2

    async def read_pcm(self, position: int, frames: int, gain: float) -> bytes:
        return self.read(position, frames, gain)

    def read(self, position: int, frames: int, gain: float) -> bytes:
        if not 0 <= gain <= 1 or not 1 <= frames <= 1024:
            raise ValueError('invalid audio bounds')
        result = array.array('h')
        for i in range(position, position + frames):
            index = (i % self.frames) * 2
            result.extend((round(self.samples[index] * gain), round(self.samples[index+1] * gain)))
        if sys.byteorder != 'little': result.byteswap()
        return result.tobytes()
