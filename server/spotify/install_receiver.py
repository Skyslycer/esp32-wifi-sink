#!/usr/bin/env python3
"""Install the pinned Linux x86_64 Connect runtime into an explicit directory."""
import argparse
import hashlib
import io
from pathlib import Path
import platform
import tarfile
import urllib.request

VERSION = '0.10.7'
SHA256 = '0712061edc269e935492c109c215c83d2998a240775789cb77d2a803f05fd816'
URL = f'https://github.com/devgianlu/go-librespot/releases/download/v{VERSION}/go-librespot_linux_x86_64.tar.gz'


def install(directory):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('This pinned installer supports Linux x86_64 only')
    with urllib.request.urlopen(URL, timeout=30) as response: archive = response.read(64 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != SHA256: raise SystemExit('Receiver archive checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        binary = next(member for member in tar.getmembers() if Path(member.name).name == 'go-librespot' and member.isfile())
        data = tar.extractfile(binary).read()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / 'go-librespot'
    temporary = destination.with_suffix('.new')
    temporary.write_bytes(data); temporary.chmod(0o755); temporary.replace(destination)
    return destination.resolve()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    print(install(parser.parse_args().directory))
