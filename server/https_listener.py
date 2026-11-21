"""Accept TLS or redirect plain HTTP on the same public HTTPS socket."""
import asyncio
import logging
import socket
from urllib.parse import quote_from_bytes

import uvicorn
from starlette.requests import Request
from starlette.responses import RedirectResponse

log = logging.getLogger(__name__)


async def redirect_to_https(scope, receive, send):
    request = Request(scope)
    path = quote_from_bytes(scope.get('raw_path', b'/'), safe="/%:@!$&'()*+,;=-._~")
    query = quote_from_bytes(scope.get('query_string', b''), safe="/%?:@!$&'()*+,;=-._~")
    location = f'https://{request.url.netloc}{path}' + ('?' + query if query else '')
    response = RedirectResponse(location, status_code=308, headers={'Connection': 'close'})
    await response(scope, receive, send)


async def first_byte(client):
    """Inspect without consuming the TLS ClientHello or HTTP request."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            return client.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            ready = loop.create_future()
            def readable():
                if not ready.done():ready.set_result(None)
            loop.add_reader(client.fileno(), readable)
            try:
                await ready
            finally:
                loop.remove_reader(client.fileno())


class HTTPSListener:
    def __init__(self, server):
        self.server = server
        self.pending = set()
        self.redirect = uvicorn.Config(redirect_to_https, lifespan='off', http='h11',
                                       ws='none', proxy_headers=False)
        self.redirect.load()

    def protocol(self, config):
        return config.http_protocol_class(config=config, server_state=self.server.server_state,
                                           app_state=self.server.lifespan.state,
                                           _loop=asyncio.get_running_loop())

    async def handoff(self, client):
        transferred = False
        try:
            prefix = await asyncio.wait_for(first_byte(client), 5)
            if not prefix:return
            tls = prefix == b'\x16'
            config = self.server.config if tls else self.redirect
            # asyncio owns the accepted socket once a transport is created.
            # TLS sees every original byte because the sniff above used MSG_PEEK.
            await asyncio.get_running_loop().connect_accepted_socket(
                lambda:self.protocol(config), client, ssl=config.ssl if tls else None,
                ssl_handshake_timeout=10 if tls else None)
            transferred = True
        except (OSError, TimeoutError, ConnectionError):
            pass
        except Exception:
            log.exception('HTTPS connection setup failed')
        finally:
            if not transferred:client.close()

    async def run(self, listener):
        try:
            # The Uvicorn server loads its SSL context and owns graceful shutdown
            # for all handed-off connections, while this loop owns accepting.
            while not self.server.started:
                if self.server.should_exit:return
                await asyncio.sleep(.01)
            listener.listen(self.server.config.backlog)
            listener.setblocking(False)
            loop = asyncio.get_running_loop()
            while True:
                client, _ = await loop.sock_accept(listener)
                if len(self.pending) >= 128:
                    client.close()
                    continue
                task = asyncio.create_task(self.handoff(client))
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
        finally:
            listener.close()
            pending = list(self.pending)
            for task in pending:task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
