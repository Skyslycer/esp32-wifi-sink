"""Single-process multi-sink control/API service; FFmpeg radio and live diagnostics."""
import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import json
import os
import sys

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from controller import Controller, CommandError
from audio.sources import PCMClip
from registry import Registry
from maintenance import process_stats
from version import SERVER_VERSION
from radio.favorites import Favorites
from radio.directory import RadioBrowser
from radio.source import RadioAudio
from audio.sources import SourceError


class Preferences(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    volume_delta_db: float | None = Field(default=None,ge=-24,le=12)
    source_deltas_db: dict[str,float] | None = None
    spotify_enabled: bool | None = None
    airplay_enabled: bool | None = None
    wfas_allowed: bool | None = None
    radio_enabled: bool | None = None

class SetupApproval(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)

class Volume(BaseModel):
    volume: float = Field(ge=0, le=1)

class ReceiverSettings(BaseModel):
    enabled: bool

class WFASSettings(ReceiverSettings):
    sample_rate: int = Field(default=44100)
    host: str = Field(default="",max_length=253,pattern=r"^[a-zA-Z0-9.-]*$")
    port: int = Field(default=9090,ge=1,le=65535)

class AirPlayTimingSettings(BaseModel):
    sync_offset_ms: float | None = Field(default=None,ge=-500,le=500)
    output_latency_ms: float | None = Field(default=None,ge=0,le=2000)

class Diagnostic(BaseModel):
    duration_seconds: float = Field(default=10, ge=1, le=600)

class Tone(Diagnostic):
    frequency: float = Field(default=440, ge=20, le=18000)

class StationInput(BaseModel):
    name: str = Field(min_length=1,max_length=128)
    url: str = Field(min_length=1,max_length=2048)
    stationuuid: str | None = Field(default=None,max_length=36)
    favicon: str | None = Field(default=None,max_length=2048)
    codec: str | None = Field(default=None,max_length=32)
    bitrate: int | None = Field(default=None,ge=0,le=1000000)
    countrycode: str | None = Field(default=None,max_length=2)
    language: str | None = Field(default=None,max_length=128)
    lastcheckok: int | None = Field(default=None,ge=0,le=1)

class StationEdit(BaseModel):
    volume_delta_db: float | None = Field(default=None,ge=-24,le=12)
    name: str | None = Field(default=None,min_length=1,max_length=128)
    url: str | None = Field(default=None,min_length=1,max_length=2048)
    favicon: str | None = Field(default=None,max_length=2048)

class FavoriteOrder(BaseModel):
    ids: list[str] = Field(max_length=256)

class RadioPlay(BaseModel):
    station_id: str | None = Field(default=None,max_length=64)
    station: StationInput | None = None

class StreamTest(BaseModel):
    url: str = Field(min_length=1,max_length=2048)

class Command(BaseModel):
    command: str
    args: dict = Field(default_factory=dict)


def create_app(state_dir: Path, tcp_host='0.0.0.0', tcp_port=8765, bench_pcm=None, allow_private_streams=False, spotify_binary=None, airplay_binary=None, restart_callback=None, setup_certificate=None):
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry(state_dir / 'devices.json')
    favorites = Favorites(state_dir / 'favorites.json')
    directory = RadioBrowser()
    controller = Controller(registry)
    controller.favorites = favorites
    controller.radio_factory = lambda ident: RadioAudio(favorites.get(ident),allow_private_streams,directory.lookup)
    if spotify_binary:
        from spotify.receiver import SpotifyReceiver
        binary = Path(spotify_binary).resolve()
        if not binary.is_file(): raise ValueError('Spotify receiver binary does not exist')
        controller.spotify_factory = lambda session: SpotifyReceiver(session, binary, state_dir / 'spotify')
    if airplay_binary:
        from airplay.receiver import AirPlayReceiver
        airplay_executable = Path(airplay_binary).resolve()
        if not airplay_executable.is_file(): raise ValueError('AirPlay receiver binary does not exist')
        controller.airplay_factory = lambda session: AirPlayReceiver(session, airplay_executable, state_dir / 'airplay')
    from wfas.discovery import WFASDiscovery
    wfas_discovery = WFASDiscovery()
    maintenance_lock = asyncio.Lock()
    restart_pending = False
    probe_slots = asyncio.Semaphore(2)
    music = PCMClip(bench_pcm) if bench_pcm else None

    @asynccontextmanager
    async def lifespan(app):
        await controller.start(tcp_host, tcp_port)
        wfas_discovery.start()
        try: yield
        finally:
            await wfas_discovery.close()
            await controller.close()

    app = FastAPI(title='ESP32 multi-sink server', version=SERVER_VERSION, lifespan=lifespan)
    app.state.controller = controller
    app.state.favorites = favorites
    app.state.directory = directory

    @app.middleware('http')
    async def bounded_request(request,call_next):
        if request.method in ('POST','PUT','PATCH'):
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>65536: return JSONResponse(status_code=413,content={'error':{'code':'REQUEST_TOO_LARGE'}})
            request._body=bytes(body)
        parts=request.url.path.strip('/').split('/')
        if len(parts)>=3 and parts[:2]==['api','devices']:
            record=registry.devices.get(parts[2])
            if not record or not record.get('approved'):
                return JSONResponse(status_code=409,content={'error':{'code':'SETUP_APPROVAL_REQUIRED','message':'Approve this device in Setup first.'}})
        return await call_next(request)

    def device(ident):
        session = controller.sessions.get(ident)
        if not session or session.closed: raise HTTPException(409, detail={'code': 'DEVICE_OFFLINE'})
        return session

    @app.exception_handler(SourceError)
    async def source_error(request,exc):
        return JSONResponse(status_code=502,content={'error':{'code':'SOURCE_UNAVAILABLE','message':str(exc)}})

    @app.exception_handler(ValueError)
    async def value_error(request,exc):
        return JSONResponse(status_code=422,content={'error':{'code':'INVALID_ARGUMENT','message':str(exc)}})

    @app.exception_handler(KeyError)
    async def missing_item(request,exc):
        return JSONResponse(status_code=404,content={'error':{'code':'NOT_FOUND'}})

    @app.exception_handler(CommandError)
    async def command_error(request, exc):
        return JSONResponse(status_code=409, content={'error': {'code': exc.code, 'message': str(exc)}})

    @app.exception_handler(ConnectionError)
    async def disconnected_error(request, exc):
        return JSONResponse(status_code=409, content={'error': {'code': 'DEVICE_OFFLINE'}})

    @app.exception_handler(TimeoutError)
    async def timeout_error(request, exc):
        return JSONResponse(status_code=504, content={'error': {'code': 'DEVICE_TIMEOUT'}})

    @app.get('/api/setup/certificate')
    async def setup_public_certificate():
        if setup_certificate is None:
            raise HTTPException(404, detail='Built-in setup HTTPS is disabled')
        return FileResponse(setup_certificate, media_type='application/x-pem-file', filename='esp32-sink-setup.crt')

    @app.get('/api/setup/firmware')
    async def setup_firmware():
        manifest=Path(__file__).resolve().parents[1]/'frontend/firmware/manifest.json'
        if not manifest.is_file(): raise HTTPException(503,detail='Web firmware is not available. Build and publish the web firmware first.')
        return JSONResponse(json.loads(manifest.read_text()),headers={'Cache-Control':'no-store'})

    @app.get('/api/setup/devices/{ident}/status')
    async def setup_status(ident: str):
        record=registry.devices.get(ident)
        return {'known':record is not None,'approved':bool(record and record.get('approved')),
                'online':ident in controller.sessions}

    @app.get('/api/setup/devices')
    async def setup_devices():
        return {'devices':[controller.sessions[k].snapshot(event_limit=0) if k in controller.sessions else
                           {'device_id':k,**v,'online':False} for k,v in registry.devices.items()
                           if not v.get('approved')]}

    @app.post('/api/setup/devices/{ident}/approve')
    async def approve_device(ident: str, body: SetupApproval):
        if ident not in registry.devices: raise HTTPException(404)
        session=controller.sessions.get(ident)
        if not session or session.closed: raise HTTPException(409,detail='Wait for the device to connect.')
        if registry.devices[ident].get('approved'): raise HTTPException(409,detail='Device is already approved.')
        registry.update(ident, display_name=body.display_name, approved=True)
        if session.prefs.get('spotify_enabled'): session.start_spotify()
        if session.prefs.get('airplay_enabled'): session.start_airplay()
        return {'device_id':ident,'display_name':registry.get(ident)['display_name'],'approved':True}

    @app.post('/api/devices/{ident}/unpair')
    async def unpair_device(ident: str):
        session=controller.sessions.get(ident)
        # Block new playback before draining the old session and its receivers.
        registry.update(ident,approved=False,playback_enabled=False,wfas_enabled=False)
        controller.player_desires.pop(ident,None)
        if session and not session.closed:
            try:
                await session.stop()
            except (ConnectionError,TimeoutError,CommandError):
                session.writer.close()
            await asyncio.gather(*(getattr(session,'disable_'+component)() for component in ('spotify','airplay','wfas')),return_exceptions=True)
            session.source=None
        controller.player_desires.pop(ident,None)
        return {'unpaired':True}

    @app.get('/api/wfas/servers')
    async def wfas_servers():
        return wfas_discovery.snapshot()

    @app.get('/api/version')
    async def server_version():
        return JSONResponse({'server_version':SERVER_VERSION}, headers={'Cache-Control':'no-store'})

    @app.get('/api/health')
    async def health():
        return {'status':'ok','server_version':SERVER_VERSION,'protocol':1,'online_devices':len(controller.sessions),
                'runtime':{**process_stats(controller),'restart_available':restart_callback is not None,'restart_pending':restart_pending},
                'features':{'wfas':True,'radio':True,'bench_music':bool(music),'spotify':bool(spotify_binary),'airplay':bool(airplay_binary)}}

    @app.post('/api/server/restart')
    async def restart_server(background: BackgroundTasks):
        nonlocal restart_pending
        if restart_callback is None: raise HTTPException(409,detail='Server restart is unavailable in this launch mode')
        async with maintenance_lock:
            if restart_pending: return {'accepted':True}
            restart_pending=True
        async def request_restart():
            await asyncio.sleep(.2)
            restart_callback()
        background.add_task(request_restart)
        return {'accepted':True}

    @app.post('/api/devices/{ident}/receivers/{component}/restart')
    async def restart_receiver(ident: str, component: str):
        if component not in ('spotify','airplay','wfas'): raise HTTPException(404)
        async with maintenance_lock:
            if restart_pending: raise HTTPException(409,detail='Server is restarting')
            session=device(ident)
            if component in ('spotify','airplay') and not getattr(controller,component+'_factory',None):
                raise HTTPException(409,detail='Component is not configured on this server')
            if not session.prefs.get(component+'_enabled'): raise HTTPException(409,detail='This component is disabled for the selected sink')
            await getattr(session,'disable_'+component)()
            getattr(session,'start_'+component)()
        return {'accepted':True,'component':component}

    @app.get('/api/events')
    async def live_events(request:Request):
        async def stream():
            while not await request.is_disconnected():
                payload={'server':await health(),'favorites':favorites.items,
                         'devices':[controller.sessions[k].snapshot(event_limit=20) if k in controller.sessions else
                                    {'device_id':k,**v,'online':False} for k,v in registry.devices.items() if v.get('approved')]}
                yield 'event: state\ndata: '+json.dumps(payload,separators=(',',':'),allow_nan=False)+'\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/devices')
    async def devices():
        return {'devices': [controller.sessions[k].snapshot() if k in controller.sessions else
                {'device_id': k, **v, 'online': False} for k, v in registry.devices.items() if v.get('approved')]}

    @app.get('/api/devices/{ident}')
    async def get_device(ident: str):
        if ident not in registry.devices: raise HTTPException(404)
        return controller.sessions[ident].snapshot() if ident in controller.sessions else {'device_id': ident, **registry.devices[ident], 'online': False}

    @app.get('/api/devices/{ident}/diagnostics/export')
    async def export_device(ident:str):
        if ident not in registry.devices: raise HTTPException(404)
        data=controller.sessions[ident].snapshot() if ident in controller.sessions else {'device_id':ident,**registry.devices[ident],'online':False}
        return JSONResponse(data,headers={'Content-Disposition':f'attachment; filename="{ident}-diagnostics.json"'})

    @app.patch('/api/devices/{ident}')
    async def preferences(ident: str, body: Preferences):
        values=body.model_dump(exclude_none=True)
        for component in ('spotify','airplay'):
            if values.get(component+'_enabled') and not getattr(controller,component+'_factory',None):
                raise HTTPException(409,detail=component.title()+' is not configured on this server')
        if values.get('wfas_allowed') is False: values['wfas_enabled']=False
        try: result = registry.update(ident, **values)
        except ValueError as exc: raise HTTPException(422, detail=str(exc))
        session=controller.sessions.get(ident)
        if session and not session.closed:
            for component in ('spotify','airplay'):
                if component+'_enabled' in values:
                    if values[component+'_enabled']:getattr(session,'start_'+component)()
                    else:await getattr(session,'disable_'+component)()
            if values.get('wfas_allowed') is False:await session.disable_wfas()
        return result

    @app.post('/api/devices/{ident}/diagnostics/test-tone')
    async def tone(ident: str, body: Tone):
        session = device(ident); await session.play_tone(body.frequency, body.duration_seconds)
        return {'accepted': True, 'player': session.snapshot()['player']}

    @app.post('/api/devices/{ident}/diagnostics/test-music')
    async def test_music(ident: str, body: Diagnostic | None = None):
        if music is None: raise HTTPException(409, detail={'code': 'BENCH_MUSIC_NOT_CONFIGURED'})
        duration = body.duration_seconds if body else 10
        session = device(ident); await session.play_source(music, duration=duration)
        return {'accepted': True, 'duration_seconds': duration}

    @app.get('/api/radio/search')
    async def radio_search(name: str='',country: str='',language: str='',limit: int=30):
        return {'stations':await directory.search(name,country,language,limit)}

    @app.get('/api/radio/favorites')
    async def radio_favorites(): return {'favorites':favorites.items}

    @app.post('/api/radio/favorites')
    async def add_favorite(body:StationInput): return favorites.add(body.model_dump(exclude_none=True))

    @app.post('/api/radio/favorites/reorder')
    async def order_favorites(body:FavoriteOrder): return {'favorites':favorites.reorder(body.ids)}

    @app.put('/api/radio/favorites/{ident}')
    async def edit_favorite(ident:str,body:StationEdit): return favorites.update(ident,body.model_dump(exclude_none=True))

    @app.delete('/api/radio/favorites/{ident}')
    async def delete_favorite(ident:str):
        favorites.delete(ident);return {'deleted':True}

    @app.post('/api/radio/test')
    async def test_station(body:StreamTest):
        async with probe_slots:
            source=RadioAudio({'id':'test','name':'Test stream','url':body.url},allow_private_streams)
            try:
                data=await asyncio.wait_for(source.read_pcm(0,1024,0),20)
                return {'working':len(data)==4096,'decoded_frames':len(data)//4,'rate':44100,'channels':2,'bits':16}
            finally: await source.close()

    @app.post('/api/devices/{ident}/player/play')
    async def play_radio(ident:str,body:RadioPlay):
        session=device(ident)
        if body.station_id:
            source=controller.radio_factory(body.station_id)
        elif body.station:
            station=body.station.model_dump(exclude_none=True)
            station['id']='preview-'+str(__import__('uuid').uuid4())
            source=RadioAudio(station,allow_private_streams,directory.lookup)
        else: raise ValueError('Select a station')
        await session.play_source(source,duration=None)
        return {'accepted':True,'player':session.snapshot()['player']}

    @app.post('/api/devices/{ident}/player/stop')
    async def stop(ident: str):
        session = device(ident); await session.stop(); return {'state': session.desired}

    @app.post('/api/devices/{ident}/player/pause')
    async def pause(ident: str):
        session = device(ident); await session.stop(pause=True); return {'state': session.desired}

    @app.post('/api/devices/{ident}/player/resume')
    async def resume(ident: str):
        session = device(ident)
        if not session.source: raise HTTPException(409, detail='No source selected')
        await session.resume()
        return {'state': session.desired}

    @app.post('/api/devices/{ident}/player/volume')
    async def volume(ident: str, body: Volume):
        try:
            session=controller.sessions.get(ident)
            if session and not session.closed:
                return await session.set_player_volume(body.volume)
            return registry.update(ident, volume=body.volume)
        except ValueError as exc: raise HTTPException(422, detail=str(exc))

    @app.post('/api/devices/{ident}/spotify')
    async def spotify_settings(ident: str, body: ReceiverSettings):
        if body.enabled and not spotify_binary: raise HTTPException(409, detail='Spotify receiver is not configured')
        prefs = registry.get(ident)
        session = controller.sessions.get(ident)
        if session and not session.closed:
            if body.enabled: session.start_spotify()
            else: await session.disable_spotify()
        registry.update(ident, spotify_enabled=body.enabled)
        return {'enabled': body.enabled}

    @app.post('/api/devices/{ident}/player/{action}')
    async def skip(ident: str, action: str):
        if action not in ('next', 'previous'): raise HTTPException(404)
        await device(ident).handle_remote_control(action)
        return {'accepted': True}

    @app.post('/api/devices/{ident}/airplay/timing')
    async def airplay_timing(ident: str, body: AirPlayTimingSettings):
        values={}
        if not body.model_fields_set:values['airplay_output_latency_ms']=None
        if 'output_latency_ms' in body.model_fields_set:values['airplay_output_latency_ms']=body.output_latency_ms
        if 'sync_offset_ms' in body.model_fields_set:values['airplay_sync_offset_ms']=body.sync_offset_ms or 0
        record=registry.update(ident,**values)
        return {'output_latency_ms':record.get('airplay_output_latency_ms'),
                'sync_offset_ms':record.get('airplay_sync_offset_ms',0)}

    @app.post('/api/devices/{ident}/wfas')
    async def wfas_settings(ident: str, body: WFASSettings):
        if body.enabled and not registry.get(ident).get('wfas_allowed',True): raise HTTPException(409,detail='Wi-Fi streaming is disabled for this sink')
        if body.enabled and not body.host: raise HTTPException(422,detail='WFAS sender host is required')
        if body.sample_rate not in (44100,48000): raise HTTPException(422,detail='Unsupported WFAS sample rate')
        session = controller.sessions.get(ident)
        if session and not session.closed: await session.disable_wfas()
        advertised = next((s for s in wfas_discovery.snapshot()['servers'] if s['host']==body.host and s['port']==body.port),None)
        rate = advertised['sample_rate'] if advertised and advertised['compatible'] else body.sample_rate
        name = advertised['name'] if advertised else registry.get(ident).get('wfas_name',body.host)
        registry.update(ident,wfas_enabled=body.enabled,wfas_host=body.host,wfas_port=body.port,wfas_sample_rate=rate,wfas_name=name)
        if session and not session.closed and body.enabled: session.start_wfas()
        return {'enabled':body.enabled}

    @app.post('/api/devices/{ident}/airplay')
    async def airplay_settings(ident: str, body: ReceiverSettings):
        if body.enabled and not airplay_binary: raise HTTPException(409, detail='AirPlay receiver is not configured')
        prefs = registry.get(ident)
        session = controller.sessions.get(ident)
        if session and not session.closed:
            if body.enabled: session.start_airplay()
            else: await session.disable_airplay()
        registry.update(ident, airplay_enabled=body.enabled)
        return {'enabled': body.enabled}

    @app.post('/api/devices/{ident}/command')
    async def command(ident: str, body: Command):
        return {'success': True, 'result': await device(ident).command(body.command, body.args)}

    frontend=Path(__file__).resolve().parents[1]/'frontend'
    if (frontend/'index.html').exists():
        @app.get('/setup',include_in_schema=False)
        async def frontend_setup(): return FileResponse(frontend/'setup.html')

        @app.get('/admin',include_in_schema=False)
        async def frontend_admin(): return FileResponse(frontend/'admin.html')
        @app.get('/',include_in_schema=False)
        async def frontend_index(): return FileResponse(frontend/'index.html')
        app.mount('/assets',StaticFiles(directory=frontend),name='assets')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/esp32-wifi-sink')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--http-port', type=int, default=8080)
    parser.add_argument('--tcp-port', type=int, default=8765)
    parser.add_argument('--allow-private-streams',action='store_true')
    parser.add_argument('--bench-pcm', type=Path)
    parser.add_argument('--spotify-binary', type=Path, help='Optional go-librespot v0.10.7 executable')
    parser.add_argument('--airplay-binary', type=Path, help='Optional Shairport Sync 5.6.3 classic audio executable')
    args = parser.parse_args()
    print(f'ESP32 Wi-Fi sink server v{SERVER_VERSION}', flush=True)
    from setup_tls import configuration, ensure_certificate, serve_with_https
    https_port = configuration()
    if https_port is not None and https_port in (args.http_port, args.tcp_port):
        parser.error('Setup HTTPS port must differ from HTTP and audio TCP ports')
    certificate, private_key = ensure_certificate(args.state_dir, args.host) if https_port else (None, None)
    restart_requested = False
    def request_process_restart():
        global restart_requested
        restart_requested = True
        if https_port is None:
            server.should_exit = True
    app = create_app(args.state_dir, args.host, args.tcp_port, args.bench_pcm, args.allow_private_streams, args.spotify_binary, args.airplay_binary, restart_callback=request_process_restart, setup_certificate=certificate)
    # Both listeners share one controller, registry and TCP audio server.
    if https_port:
        asyncio.run(serve_with_https(app, args.host, args.http_port, https_port,
                                    certificate, private_key, lambda: restart_requested))
    else:
        server = uvicorn.Server(uvicorn.Config(app,host=args.host,port=args.http_port,log_level='info',timeout_graceful_shutdown=5))
        server.run()
    if restart_requested:
        os.execv(sys.executable,[sys.executable,*sys.argv])
