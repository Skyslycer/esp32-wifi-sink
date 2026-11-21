"""Optional local HTTPS for the whole application, sharing one server state."""
import asyncio
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import tempfile

import uvicorn
from https_listener import HTTPSListener


def configuration():
    value = os.environ.get('SINK_SETUP_HTTPS_ENABLED', 'false').lower()
    if value not in ('true', 'false', '1', '0'):
        raise ValueError('SINK_SETUP_HTTPS_ENABLED must be true or false')
    if value in ('false', '0'):
        return None
    port = int(os.environ.get('SINK_SETUP_HTTPS_PORT', '8443'))
    if not 1 <= port <= 65535:
        raise ValueError('SINK_SETUP_HTTPS_PORT must be between 1 and 65535')
    return port


def certificate_hosts(bind_host):
    hosts = {'localhost', '127.0.0.1', '::1'}
    configured = os.environ.get('SINK_SETUP_HTTPS_HOSTS', '')
    if configured:
        hosts.update(configured.split(','))
    else:
        hosts.add(socket.gethostname())
        if bind_host not in ('0.0.0.0', '::'):
            hosts.add(bind_host)
        try:
            hosts.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None))
        except socket.gaierror:
            pass
        # A UDP connect selects the host's outbound address without sending data.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(('192.0.2.1', 9))
                hosts.add(sock.getsockname()[0])
        except OSError:
            pass
    result = set()
    for host in hosts:
        host = host.strip().rstrip('.')
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = host.encode('idna').decode('ascii').lower()
            if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                          for label in host.split('.')):
                raise ValueError('SINK_SETUP_HTTPS_HOSTS must contain hostnames or IP addresses without ports')
        result.add(host)
    return sorted(result)


def ensure_certificate(state_dir, bind_host):
    directory = Path(state_dir) / 'tls'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    cert, key, metadata = (directory / name for name in ('setup.crt', 'setup.key', 'hosts.json'))
    hosts = certificate_hosts(bind_host)
    reuse = False
    if cert.is_file() and key.is_file() and metadata.is_file():
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            reuse = (json.loads(metadata.read_text()) == hosts and
                     subprocess.run(['openssl', 'x509', '-checkend', str(30*86400), '-noout', '-in', str(cert)],
                                    capture_output=True, check=False).returncode == 0)
        except (OSError, ValueError, ssl.SSLError):
            pass
    if not reuse:
        alternatives = []
        for host in hosts:
            try:
                ipaddress.ip_address(host)
                alternatives.append('IP:' + host)
            except ValueError:
                alternatives.append('DNS:' + host)
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            new_cert, new_key = Path(temp) / 'cert.pem', Path(temp) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes',
                            '-days', '365', '-subj', '/CN=ESP32 sink local setup',
                            '-addext', 'subjectAltName=' + ','.join(alternatives),
                            '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
                            '-addext', 'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign',
                            '-addext', 'extendedKeyUsage=serverAuth',
                            '-keyout', str(new_key), '-out', str(new_cert)],
                           check=True, capture_output=True)
            new_key.chmod(0o600)
            new_cert.chmod(0o644)
            new_key.replace(key)
            new_cert.replace(cert)
        metadata.write_text(json.dumps(hosts))
        print('Generated self-signed setup certificate for: ' + ', '.join(hosts), flush=True)
    key.chmod(0o600)
    fingerprint = subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-fingerprint', '-sha256'],
                                 check=True, capture_output=True, text=True).stdout.strip()
    print(f'Setup certificate: {cert}; {fingerprint}', flush=True)
    return cert, key


class SharedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # One signal handler coordinates both listeners and their shared lifespan.
        yield


async def serve_with_https(app, host, http_port, https_port, cert, key, restart_requested):
    servers = [SharedServer(uvicorn.Config(app, host=host, port=http_port, lifespan='off',
                                         log_level='info', timeout_graceful_shutdown=5)),
               SharedServer(uvicorn.Config(app, host=host, port=https_port, lifespan='off',
                                         log_level='info', timeout_graceful_shutdown=5,
                                         ssl_certfile=cert, ssl_keyfile=key))]

    def stop(signum=None, frame=None):
        for server in servers:
            server.should_exit = True

    listeners = []
    tasks = []
    previous = {}
    try:
        # Bind both ports before starting the shared controller. A port conflict
        # must fail startup rather than leave a half-working HTTP-only service.
        listeners = []
        for server in servers:
            listeners.append(server.config.bind_socket())
        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        async with app.router.lifespan_context(app):
            tasks = [asyncio.create_task(servers[0].serve(sockets=[listeners[0]])),
                     asyncio.create_task(servers[1].serve(sockets=[])),
                     asyncio.create_task(HTTPSListener(servers[1]).run(listeners[1]))]
            try:
                while all(not task.done() for task in tasks):
                    if restart_requested():
                        stop()
                    await asyncio.sleep(.1)
            finally:
                stop()
                tasks[2].cancel()  # Stop accepting before draining Uvicorn connections.
                results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result

    finally:
        stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for listener in listeners:
            listener.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
