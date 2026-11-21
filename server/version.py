"""Server release version; firmware versions are maintained by each board project."""
from pathlib import Path
import re

SERVER_VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?', SERVER_VERSION):
    raise ValueError('VERSION must contain a semantic server version')
