"""Radio Browser mirror discovery with failover; no account required."""
import asyncio
import json
import random
import socket
import time
from uuid import UUID
import httpx
from audio.sources import SourceError
from .urls import validate_url


class RadioBrowser:
    def __init__(self):
        self.mirrors = []
        self.refreshed = 0
        self.lock = asyncio.Lock()

    async def discover(self):
        if self.mirrors and time.monotonic() - self.refreshed < 3600: return
        async with self.lock:
            if self.mirrors and time.monotonic() - self.refreshed < 3600: return
            loop = asyncio.get_running_loop()
            names = set()
            try:
                addresses = await asyncio.wait_for(loop.getaddrinfo('all.api.radio-browser.info',443,type=socket.SOCK_STREAM),3)
                async def reverse(address):
                    try:
                        hostname,_ = await asyncio.wait_for(loop.getnameinfo(address[4],socket.NI_NAMEREQD),2)
                        if hostname.endswith('.api.radio-browser.info'): return hostname
                    except (OSError,TimeoutError): pass
                for name in await asyncio.gather(*(reverse(a) for a in addresses[:16])):
                    if name: names.add(name)
            except (OSError,TimeoutError): pass
            # Bootstrap failover if reverse DNS is unavailable, never one fixed server.
            self.mirrors = ['https://' + name for name in names] or ['https://de1.api.radio-browser.info','https://nl1.api.radio-browser.info']
            random.shuffle(self.mirrors); self.refreshed=time.monotonic()

    async def request(self,path,params=None):
        await self.discover()
        async with httpx.AsyncClient(timeout=httpx.Timeout(5,connect=2),headers={'User-Agent':'ESP32WiFiSink/0.3'},follow_redirects=False) as client:
            for mirror in self.mirrors[:3]:
                try:
                    async with client.stream('GET',mirror+path,params=params) as response:
                        response.raise_for_status();body=bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body)>1024*1024:raise ValueError('directory response too large')
                    data=json.loads(body)
                    if not isinstance(data,list):raise ValueError('invalid directory response')
                    return data[:50]
                except (httpx.HTTPError,ValueError):continue
        self.refreshed=0
        raise SourceError('Radio Browser mirrors are currently unavailable')

    @staticmethod
    def normalize(station):
        ident=str(UUID(station['stationuuid']))
        url=validate_url(station.get('url_resolved') or station['url'])
        return {'stationuuid':ident,'name':str(station['name'])[:128],'url':url,
                **{key:station.get(key) for key in ('favicon','codec','bitrate','countrycode','language','lastcheckok')}}

    async def search(self,name='',country='',language='',limit=30):
        params={'name':name[:128],('countrycode' if len(country)==2 else 'country'):country.upper() if len(country)==2 else country[:128],'language':language[:80],
                'hidebroken':'true','limit':min(max(limit,1),50),'order':'clickcount','reverse':'true'}
        results=[]
        for station in await self.request('/json/stations/search',params):
            try:results.append(self.normalize(station))
            except (ValueError,KeyError,TypeError):continue
        return results

    async def lookup(self,stationuuid):
        ident=str(UUID(stationuuid))
        data=await self.request('/json/stations/byuuid/'+ident)
        for record in data:
            try:
                station=self.normalize(record)
                if station['stationuuid']==ident:return station
            except (ValueError,KeyError,TypeError):continue
        raise SourceError('Station UUID is not available in Radio Browser')
