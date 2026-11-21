"""Small process snapshot for the administration panel."""
import os
import platform
import time
from pathlib import Path

STARTED = time.monotonic()

def process_stats(controller):
    memory = None
    try:
        memory = int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
    except (OSError,ValueError,IndexError): pass
    return {'uptime_seconds':round(time.monotonic()-STARTED), 'process_id':os.getpid(),
            'memory_bytes':memory,'python_version':platform.python_version(),
            'connected_sinks':sum(not session.closed for session in controller.sessions.values()),
            'known_sinks':sum(bool(record.get('approved')) for record in controller.registry.devices.values()),
            'pending_approvals':sum(not record.get('approved') for record in controller.registry.devices.values()),
            'playing_sinks':sum(not session.closed and getattr(session,'desired',None)=='playing' and
                                getattr(session,'status',{}).get('active',False) and
                                getattr(session,'status',{}).get('playout_started',False)
                                for session in controller.sessions.values())}
