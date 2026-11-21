"""Container readiness check using the configured HTTP listener."""
import json
import os
import sys
from urllib.request import urlopen

host=os.environ.get('SINK_BIND_ADDRESS','0.0.0.0')
if host in ('0.0.0.0','::'):host='127.0.0.1' if host=='0.0.0.0' else '::1'
if ':' in host:host='['+host+']'
try:
    with urlopen(f'http://{host}:{os.environ.get("SINK_HTTP_PORT","8080")}/api/health',timeout=3) as response:
        healthy=response.status==200 and json.load(response).get('status')=='ok'
except (OSError,ValueError):
    healthy=False
sys.exit(0 if healthy else 1)
