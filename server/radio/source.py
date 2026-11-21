import asyncio
import re
from audio.ffmpeg import FFmpegPCM
from audio.sources import SourceError
from .urls import resolve_stream, validate_url


class RadioAudio(FFmpegPCM):
    kind = 'radio'
    retryable = True
    ffmpeg_log_level = 'verbose'

    def __init__(self, station, allow_private=False, station_lookup=None):
        super().__init__()
        self.station = dict(station)
        self.url = validate_url(station['url'])
        self.allow_private = allow_private
        self.station_lookup=station_lookup
        self.stream_title = None
        self._metadata_log = b''

    def handle_stderr(self, data):
        # FFmpeg's HTTP reader removes ICY blocks from the decoded audio and
        # emits these updates on the same connection. Bound partial log lines.
        lines = (self._metadata_log + data).replace(b'\r', b'\n').split(b'\n')
        self._metadata_log = lines.pop()[-8192:]
        for line in lines:
            match = re.match(rb'^\[https? @ [^\]]+\] Metadata update for StreamTitle: (.*)', line)
            if match:
                title = match.group(1).decode('utf-8', errors='replace')
                self.stream_title = ''.join(char for char in title if char.isprintable()).strip()[:512] or None

    async def close(self):
        try:
            await super().close()
        finally:
            self.stream_title = None
            self._metadata_log = b''

    async def input_arguments(self):
        if self.station_lookup and self.station.get('stationuuid'):
            try:
                fresh=await asyncio.wait_for(self.station_lookup(self.station['stationuuid']),5)
                self.url=validate_url(fresh['url'])
                self.station['url']=self.url
            except (SourceError,ValueError,KeyError,TimeoutError):
                pass  # Cached favorites remain playable during a directory outage.
        try: resolved = await asyncio.wait_for(resolve_stream(self.url, self.allow_private), 12)
        except TimeoutError as exc: raise SourceError('Station URL resolution timed out') from exc
        return ['-icy', '1', '-protocol_whitelist', 'http,https,tcp,tls,crypto', '-rw_timeout', '5000000',
                '-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '5',
                '-i', resolved]
