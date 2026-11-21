"""Sample-clocked gain changes for signed 16-bit little-endian stereo PCM."""
import array
import math
import sys


class GainRamp:
    def __init__(self, frames=6615):
        if not isinstance(frames, int) or frames < 1:
            raise ValueError('gain ramp length must be a positive frame count')
        self.frames = frames  # 150 ms at 44.1 kHz.
        self.current = self.target = None
        self.remaining = 0
        self.step = 0.0

    def process(self, pcm, target):
        if len(pcm) % 4 or not math.isfinite(target) or not 0 <= target <= 64:
            raise ValueError('invalid stereo PCM or gain')
        if self.current is None:
            self.current = self.target = target
        elif target != self.target:
            self.target = target
            self.remaining = self.frames
            self.step = (target - self.current) / self.frames
        if not self.remaining:
            if self.current == 1:
                return pcm
            if self.current == 0:
                return bytes(len(pcm))
        samples = array.array('h', pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        if not self.remaining:
            for index, value in enumerate(samples):
                samples[index] = max(-32768, min(32767, round(value * self.current)))
            if sys.byteorder != 'little':
                samples.byteswap()
            return samples.tobytes()
        for index in range(0, len(samples), 2):
            if self.remaining:
                self.current += self.step
                self.remaining -= 1
                if not self.remaining:
                    self.current = self.target
            for channel in (index, index + 1):
                samples[channel] = max(-32768, min(32767, round(samples[channel] * self.current)))
        if sys.byteorder != 'little':
            samples.byteswap()
        return samples.tobytes()
