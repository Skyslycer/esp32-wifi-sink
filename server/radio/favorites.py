import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4
from .urls import validate_url


class Favorites:
    def __init__(self, path: Path):
        self.path = path
        self.items = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(self.items, list) or len(self.items) > 256: raise ValueError('invalid favorites file')

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.favorites-')
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(self.items, out); out.flush(); os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name): os.unlink(name)

    def get(self, ident):
        for item in self.items:
            if item['id'] == ident: return dict(item)
        raise KeyError(ident)

    def add(self, values):
        if len(self.items) >= 256: raise ValueError('favorite limit reached')
        value = self.validated(values)
        stationuuid = value.get('stationuuid')
        ident = 'rb-' + str(UUID(stationuuid)) if stationuuid else 'custom-' + str(uuid4())
        for item in self.items:
            if item['id'] == ident: return dict(item)
        value['id'] = ident
        self.items.append(value); self.save(); return dict(value)

    @staticmethod
    def validated(values):
        name = values.get('name', '').strip()
        if not 1 <= len(name) <= 128 or any(ord(c)<32 for c in name): raise ValueError('station name must be 1..128 printable characters')
        url = validate_url(values.get('url', ''))
        result = {'name': name, 'url': url}
        for key in ('stationuuid','favicon','codec','bitrate','countrycode','language','lastcheckok'):
            if key in values and values[key] is not None: result[key] = values[key]
        delta=float(values.get('volume_delta_db',0))
        if not -24<=delta<=12: raise ValueError('Invalid station volume delta')
        result['volume_delta_db']=delta
        if result.get('stationuuid'): result['stationuuid'] = str(UUID(result['stationuuid']))
        if result.get('favicon'):
            try: validate_url(result['favicon'])
            except ValueError: result['favicon'] = ''
        return result

    def update(self, ident, changes):
        for index, item in enumerate(self.items):
            if item['id'] == ident:
                value = self.validated({**item, **changes}); value['id'] = ident
                self.items[index] = value; self.save(); return dict(value)
        raise KeyError(ident)

    def delete(self, ident):
        self.get(ident)
        self.items = [x for x in self.items if x['id'] != ident]; self.save()

    def reorder(self, ids):
        if len(ids) != len(set(ids)) or set(ids) != {x['id'] for x in self.items}: raise ValueError('reorder must include every favorite exactly once')
        self.items = [self.get(ident) for ident in ids]; self.save()
        return self.items
