"""PCM adapter shared by server-side network receivers."""
import array
import sys
from .sources import AudioSource, SourceError


class ReceiverPCM(AudioSource):
    is_live = True

    def __init__(self, receiver):
        self.receiver = receiver

    @property
    def stream_title(self):
        track = self.receiver.track
        return ' — '.join(filter(None, [', '.join(track.get('artist_names', [])), track.get('name')])) or None

    async def read_pcm(self, position, frames, gain):
        if not 1 <= frames <= 1024 or not 0 <= gain <= 64:
            raise ValueError('invalid PCM bounds')
        raw = await self.receiver.read(frames * 4)
        if gain == 1:
            return raw
        samples = array.array('h', raw)
        if sys.byteorder != 'little': samples.byteswap()
        if gain != 1:
            for i, sample in enumerate(samples): samples[i] = max(-32768, min(32767, round(sample * gain)))
        if sys.byteorder != 'little': samples.byteswap()
        return samples.tobytes()

    async def close(self):
        self.receiver.flush()

    async def transport(self, action):
        await self.receiver.control(action)
