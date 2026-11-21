"""Bounded WFAS server directory populated by multicast presence beacons."""
import asyncio
import contextlib
import socket
import time

class WFASDiscovery:
    def __init__(self):
        self.servers = {}
        self.error = None
        self.task = None

    def feed(self, data, address, now=None):
        now = time.monotonic() if now is None else now
        if len(data) > 2048: return
        try:
            fields = data.decode('utf-8').split(';')
            if len(fields)<4 or fields[0]!='WIFI_AUDIO_STREAMER_DISCOVERY': return
            port = int(fields[3])
            if not 1<=port<=65535: return
            key = f'{address[0]}:{port}'
            if fields[2]=='BYE':
                self.servers.pop(key,None)
                return
            if fields[2] not in ('UNICAST','MULTICAST'): return
            options = dict(f.split('=',1) for f in fields[4:] if '=' in f)
            reason = None
            if fields[2]!='UNICAST': reason = 'Select Unicast in the sender'
            elif options.get('auth','OFF')!='OFF' or options.get('enc','0')!='0': reason = 'Select security Off in the sender'
            elif any(options.get(k)!=v for k,v in [('ch','2'),('bd','16')]):
                reason = 'Set sender audio to 16-bit stereo'
            elif options.get('sr') not in ('44100','48000'): reason = 'Select 44100 or 48000 Hz in the sender'
            self.snapshot(now)
            if key not in self.servers and len(self.servers)>=64: return
            self.servers[key] = dict(id=key,host=address[0],port=port,name=fields[1][:128],
                                     compatible=reason is None,reason=reason,sample_rate=int(options.get("sr","0")),seen=now)
        except (ValueError,UnicodeError): return

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        self.servers = {k:v for k,v in self.servers.items() if now-v['seen']<12}
        return {'servers':[{k:v for k,v in item.items() if k!='seen'} for item in self.servers.values()],
                'error':self.error}

    async def run(self):
        sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('',9091))
            sock.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton('239.255.0.1')+socket.inet_aton('0.0.0.0'))
            sock.setblocking(False)
            loop = asyncio.get_running_loop()
            while True:
                data,address = await loop.sock_recvfrom(sock,2049)
                self.feed(data,address)
        except OSError as exc: self.error = f'WFAS discovery unavailable: {exc}'
        finally: sock.close()

    def start(self): self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await self.task
