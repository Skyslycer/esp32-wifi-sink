"""Supervise the application and its private discovery daemons without systemd."""
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import time

stopping=False


def stop_requested(signum, frame):
    global stopping
    stopping=True


def number(name, default, maximum):
    value=int(os.environ.get(name,str(default)))
    if not 1<=value<=maximum:raise ValueError(f'{name} must be between 1 and {maximum}')
    return value


def enabled(name):
    value=os.environ.get(name,'true').lower()
    if value not in ('true','false','1','0'):raise ValueError(f'{name} must be true or false')
    return value in ('true','1')


def wait_ready(child, path):
    deadline=time.monotonic()+10
    while not path.exists():
        if stopping:raise InterruptedError('Container is stopping')
        if child.poll() is not None:raise RuntimeError('Discovery daemon exited during startup')
        if time.monotonic()>deadline:raise RuntimeError('Discovery daemon did not become ready')
        time.sleep(.1)


def stop_child(child, timeout):
    if child.poll() is None:
        try:os.killpg(child.pid,signal.SIGTERM)
        except ProcessLookupError:return
        try:child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            child.wait()


def main():
    children=[]
    app=None
    signal.signal(signal.SIGTERM,stop_requested)
    signal.signal(signal.SIGINT,stop_requested)
    try:
        uid=number('PUID',10001,2147483647)
        gid=number('PGID',10001,2147483647)
        http=number('SINK_HTTP_PORT',8080,65535)
        tcp=number('SINK_TCP_PORT',8765,65535)
        spotify=enabled('SINK_SPOTIFY_ENABLED')
        airplay=enabled('SINK_AIRPLAY_ENABLED')
        state=Path('/data');state.mkdir(parents=True,exist_ok=True)
        os.chown(state,uid,gid)
        os.umask(0o077)
        # D-Bus EXTERNAL authentication requires a passwd entry for the app UID.
        # /etc/passwd and /etc/group point into the writable /run tmpfs.
        for kind,ident,entry in (
            ('passwd',uid,f'sink-runtime:x:{uid}:{gid}:Audio server:/data:/usr/sbin/nologin\n'),
            ('group',gid,f'sink-runtime:x:{gid}:\n'),
        ):
            content=Path(f'/etc/{kind}.base').read_text()
            if not any(line.split(':')[2]==str(ident) for line in content.splitlines() if line):
                content+=entry
            runtime_identity=Path('/run')/kind
            runtime_identity.write_text(content)
            runtime_identity.chmod(0o644)
        if airplay:
            # This bus is internal to the container; no host D-Bus mount is used.
            Path('/run/dbus').mkdir(parents=True,exist_ok=True)
            Path('/run/dbus').chmod(0o755)
            subprocess.run(['dbus-uuidgen','--ensure=/run/machine-id'],check=True)
            Path('/run/machine-id').chmod(0o644)
            bus=subprocess.Popen(['dbus-daemon','--system','--nofork','--nopidfile','--nosyslog'],start_new_session=True)
            children.append(bus)
            wait_ready(bus,Path('/run/dbus/system_bus_socket'))
            avahi_user=pwd.getpwnam('avahi')
            runtime=Path('/run/avahi-daemon');runtime.mkdir(parents=True,exist_ok=True)
            os.chown(runtime,avahi_user.pw_uid,avahi_user.pw_gid)
            config=Path('/app/docker/avahi-daemon.conf').read_text()
            interfaces=os.environ.get('SINK_MDNS_INTERFACES','')
            if interfaces:
                if not re.fullmatch(r'[a-zA-Z0-9_.:-]+(?:,[a-zA-Z0-9_.:-]+)*',interfaces):
                    raise ValueError('SINK_MDNS_INTERFACES must be comma-separated interface names')
                config=config.replace('[server]','[server]\nallow-interfaces='+interfaces,1)
            config_path=Path('/run/avahi-daemon.conf');config_path.write_text(config);config_path.chmod(0o644)
            avahi=subprocess.Popen(['avahi-daemon','--no-chroot','--file='+str(config_path)],start_new_session=True)
            children.append(avahi)
            wait_ready(avahi,Path('/run/avahi-daemon/pid'))
        command=[sys.executable,'/app/server/app.py','--state-dir','/data',
                 '--host',os.environ.get('SINK_BIND_ADDRESS','0.0.0.0'),
                 '--http-port',str(http),'--tcp-port',str(tcp)]
        if spotify:command+=['--spotify-binary','/opt/receivers/spotify/go-librespot']
        if airplay:command+=['--airplay-binary','/opt/receivers/airplay/shairport-sync']
        command+=sys.argv[1:]
        app=subprocess.Popen(command,user=uid,group=gid,extra_groups=[],start_new_session=True)
        print(f'Audio server started as {uid}:{gid}; HTTP {http}, audio TCP {tcp}',flush=True)
        while not stopping:
            for child in [app,*children]:
                if child.poll() is not None:
                    print(f'Container process {child.pid} exited with status {child.returncode}',file=sys.stderr,flush=True)
                    return child.returncode or 1
            time.sleep(.2)
        return 0
    except InterruptedError:
        return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(f'Container startup failed: {exc}',file=sys.stderr,flush=True)
        return 1
    finally:
        # Let the app save state and stop receiver children before removing discovery.
        if app:stop_child(app,15)
        for child in reversed(children):stop_child(child,5)


if __name__=='__main__':
    raise SystemExit(main())
