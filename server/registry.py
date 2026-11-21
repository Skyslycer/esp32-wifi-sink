"""Atomic, bounded on-disk device preferences, independent of live sessions."""
import json
import asyncio
import threading
import os
from pathlib import Path
import re
import tempfile
import time

DEVICE_ID = re.compile(r'esp32-[0-9a-f]{12}\Z')


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock=threading.Lock()
        self._revision=0
        self._committed_revision=-1
        self._save_task=None
        self.devices = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(self.devices, dict): raise ValueError('invalid registry')
        required={'display_name','approved','volume','spotify_enabled','airplay_enabled','wfas_allowed','radio_enabled'}
        if any(not isinstance(record,dict) or not required.issubset(record) for record in self.devices.values()):
            raise ValueError('Device registry must use the current settings schema')

    def _write_snapshot(self, revision, payload):
        # A queued older snapshot must not overwrite a newer synchronous save.
        with self._write_lock:
            if revision <= self._committed_revision: return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.registry-')
            try:
                with os.fdopen(fd, 'w') as out:
                    out.write(payload)
                    out.flush(); os.fsync(out.fileno())
                os.replace(name, self.path)
                self._committed_revision=revision
            finally:
                if os.path.exists(name): os.unlink(name)

    def save(self):
        self._revision+=1
        self._write_snapshot(self._revision,json.dumps(self.devices))

    def update_debounced(self, device_id, **values):
        revision=self._revision
        record=self.update(device_id,_persist=False,**values)
        if revision==self._revision:return record
        if self._save_task is None or self._save_task.done():
            self._save_task=asyncio.create_task(self._persist_debounced())
        return record

    async def _persist_debounced(self):
        while True:
            revision=self._revision
            await asyncio.sleep(.25)
            if revision!=self._revision:continue
            payload=json.dumps(self.devices)
            await asyncio.to_thread(self._write_snapshot,revision,payload)
            if revision==self._revision: return

    async def flush(self):
        if self._save_task is not None:
            await self._save_task
            self._save_task=None

    def get(self, device_id: str):
        if not DEVICE_ID.fullmatch(device_id): raise ValueError('invalid device_id')
        if device_id not in self.devices:
            if len(self.devices) >= 64: raise ValueError('registry capacity reached')
            self.devices[device_id] = {'display_name': device_id[-6:], 'approved': False, 'volume': .1, 'created_at': time.time(),
                                       'spotify_enabled':True,'airplay_enabled':True,'wfas_allowed':True,'radio_enabled':True}
            self.save()
        return self.devices[device_id]

    def update(self, device_id, _persist=True, **values):
        allowed={'display_name','approved','volume','spotify_enabled','airplay_enabled','wfas_allowed','radio_enabled',
                 'volume_delta_db','source_deltas_db','airplay_sync_offset_ms','airplay_output_latency_ms',
                 'station_id','playback_enabled','wfas_enabled','wfas_host','wfas_port','wfas_sample_rate','wfas_name',
                 'bluetooth_names','output_type','last_seen_at','created_at'}
        if values.keys()-allowed:raise ValueError('Unknown preference keys')
        record = self.get(device_id)
        if 'display_name' in values:
            name = values['display_name'].strip()
            if not 1 <= len(name) <= 64 or any(ord(c) < 32 for c in name):
                raise ValueError('display_name must contain 1..64 printable characters')
            values['display_name'] = name
        if 'airplay_sync_offset_ms' in values:
            v=values['airplay_sync_offset_ms']
            if not isinstance(v,(int,float)) or not -500<=v<=500:raise ValueError('Invalid AirPlay sync adjustment')
        if 'airplay_output_latency_ms' in values and values['airplay_output_latency_ms'] is not None:
            v=values['airplay_output_latency_ms']
            if not isinstance(v,(int,float)) or not 0<=v<=2000:raise ValueError('Invalid AirPlay output latency')
        if 'volume' in values and not 0 <= values['volume'] <= 1:
            raise ValueError('volume must be 0..1')
        if 'source_deltas_db' in values:
            if any(k not in ('radio','airplay','spotify','wfas') or not isinstance(v,(int,float)) or not (-60 if k=='wfas' else -24)<=v<=12 for k,v in values['source_deltas_db'].items()): raise ValueError('Invalid source volume delta')
        if 'volume_delta_db' in values and not -24<=values['volume_delta_db']<=12: raise ValueError('Invalid volume delta')
        if all(record.get(key)==value for key,value in values.items()): return record
        record.update(values)
        if _persist:self.save()
        else:self._revision+=1
        return record
