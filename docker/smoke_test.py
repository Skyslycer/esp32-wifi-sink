#!/usr/bin/env python3
"""Check an image using disposable Docker resources; no host ports are exposed."""
import argparse
import subprocess
import time
import uuid


def docker(*args, check=True, capture=False):
    return subprocess.run(['docker', *args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', nargs='?', default='esp32-wifi-sink:local')
    parser.add_argument('--setup-https', action='store_true', help='Also verify the optional self-signed HTTPS listener')
    args = parser.parse_args()
    name = 'esp32-sink-check-' + uuid.uuid4().hex[:12]
    volume = name + '-data'
    try:
        tls_env = ['-e', 'SINK_SETUP_HTTPS_ENABLED=true', '-e', 'SINK_SETUP_HTTPS_HOSTS=localhost,127.0.0.1'] if args.setup_https else []
        docker('run', '-d', '--name', name, '--read-only', '--tmpfs', '/run',
               '--tmpfs', '/tmp', '--security-opt', 'no-new-privileges:true',
               '-e', 'PUID=12345', '-e', 'PGID=12345',
               '-v', volume + ':/data', *tls_env, args.image)
        deadline = time.monotonic() + 60
        while True:
            if docker('exec', name, 'python', '/app/docker/healthcheck.py',
                      check=False, capture=True).returncode == 0:
                break
            state = docker('inspect', '--format', '{{.State.Running}}', name, capture=True)
            if state.stdout.strip() != 'true' or time.monotonic() >= deadline:
                raise RuntimeError('Container did not become healthy')
            time.sleep(1)
        docker('exec', name, 'python', '-c', '''
import hashlib, json, os, ssl
from pathlib import Path
from urllib.request import urlopen

def get(path):
    with urlopen('http://127.0.0.1:8080'+path,timeout=5) as response:
        assert response.status==200
        return response.read()

health=json.loads(get('/api/health'))
version=Path('/app/VERSION').read_text().strip()
assert json.loads(get('/api/version'))['server_version']==version
assert health['server_version']==version
assert json.loads(get('/openapi.json'))['info']['version']==version
assert health['features']['spotify'] and health['features']['airplay'] and health['features']['wfas']
status=Path('/proc')/str(health['runtime']['process_id'])/'status'
assert next(line for line in status.read_text().splitlines() if line.startswith('Uid:')).split()[1:] == ['12345']*4
for page in ('/','/admin','/setup'):get(page)
manifest=json.loads(get('/api/setup/firmware'))
assert manifest['schema_version']==1 and manifest['boards'] and manifest['firmware_version']
assert all('firmware_version' not in board for board in manifest['boards'].values())
for board in manifest['boards'].values():
    for image in board['images'].values():
        assert hashlib.sha256(get('/'+image['url'])).hexdigest()==image['sha256']
if os.environ.get('SINK_SETUP_HTTPS_ENABLED') == 'true':
    cert=Path('/data/tls/setup.crt')
    context=ssl.create_default_context(cafile=str(cert))
    for path in ('/setup','/api/health','/api/setup/firmware'):
        with urlopen('https://127.0.0.1:8443'+path,context=context,timeout=5) as response:
            assert response.status==200
    assert get('/api/setup/certificate')==cert.read_bytes()
    assert Path('/data/tls/setup.key').stat().st_mode & 0o777 == 0o600
    print('Trusted HTTPS setup, APIs, certificate download and key permissions passed')
else:
    assert not Path('/data/tls').exists()
print('HTTP pages, firmware hashes, source availability and application UID passed')
''')
        # D-Bus must accept the application UID for AirPlay's Avahi registration.
        docker('exec', '-u', '12345:12345', name, 'dbus-send', '--system',
               '--print-reply', '--dest=org.freedesktop.DBus', '/',
               'org.freedesktop.DBus.ListNames')
        docker('stop', '--timeout', '25', name)
        result = docker('inspect', '--format', '{{.State.ExitCode}}', name, capture=True)
        assert result.stdout.strip() == '0', result.stdout
        print('Container startup, D-Bus authentication and graceful shutdown passed')
    except BaseException:
        docker('logs', '--tail', '100', name, check=False)
        raise
    finally:
        docker('rm', '-f', name, check=False, capture=True)
        docker('volume', 'rm', volume, check=False, capture=True)


if __name__ == '__main__':
    main()
