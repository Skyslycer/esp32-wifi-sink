"""HTTP-only station URLs, bounded redirects and ordinary playlist resolution."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urljoin
import httpx
from audio.sources import SourceError


def validate_url(url: str):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError('invalid stream URL')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('stream URL must be HTTP/HTTPS without credentials or fragment')
    try: port = parsed.port
    except ValueError: raise ValueError('invalid stream port')
    if port is not None and not 1 <= port <= 65535: raise ValueError('invalid stream port')
    return url


async def check_address(url, allow_private=False):
    validate_url(url)
    hostname = urlsplit(url).hostname
    try:
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM), 3)
    except (OSError, TimeoutError) as exc: raise SourceError('Station hostname cannot be resolved') from exc
    if not addresses: raise SourceError('Station hostname has no address')
    if not allow_private and any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise SourceError('Private, loopback and reserved station addresses are disabled')


def playlist_target(text: str, base: str):
    candidates = []
    for line in text.splitlines()[:512]:
        line = line.strip()
        if line.lower().startswith('file') and '=' in line: line = line.split('=', 1)[1].strip()
        if not line or line.startswith(('#', '[')) or '=' in line and not line.startswith(('http://', 'https://')): continue
        try: candidates.append(validate_url(urljoin(base, line)))
        except ValueError: continue
    if not candidates: raise SourceError('Playlist contains no HTTP/HTTPS stream')
    return validate_url(candidates[0])


async def resolve_stream(url, allow_private=False, client=None):
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3), follow_redirects=False,
                                         headers={'User-Agent': 'ESP32WiFiSink/0.3'})
    try:
        for _ in range(5):
            await check_address(url, allow_private)
            async with client.stream('GET', url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location: raise SourceError('Station redirect has no destination')
                    url = validate_url(urljoin(url, location)); continue
                response.raise_for_status()
                content_type = response.headers.get('content-type', '').split(';')[0].lower()
                path = urlsplit(url).path.lower()
                ordinary_playlist = path.endswith(('.m3u', '.pls')) or content_type in ('audio/x-scpls','audio/x-mpegurl','application/x-mpegurl')
                if not ordinary_playlist or path.endswith('.m3u8'): return url
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536: raise SourceError('Playlist exceeds 64 KiB')
                text = data.decode('utf-8-sig', errors='replace')
                if '#EXT-X-' in text: return url  # HLS is decoded by FFmpeg.
                url = playlist_target(text, url)
        raise SourceError('Too many station redirects or nested playlists')
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError('Station URL did not open successfully') from exc
    finally:
        if own: await client.aclose()
