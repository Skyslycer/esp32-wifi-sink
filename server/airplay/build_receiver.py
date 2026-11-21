#!/usr/bin/env python3
"""Build pinned classic Shairport Sync locally, without installing system services.

Requires compiler/autotools, pkg-config, FFmpeg/Avahi/OpenSSL/soxr/popt development
packages. libconfig is built privately and linked statically.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

SOURCES = {
    'shairport': ('https://codeload.github.com/mikebrady/shairport-sync/tar.gz/refs/tags/5.6.3', '670f9cefdb9c2d70f7df71890e63ab0a31d08a815dd0734245d935bfd01e766e'),
    'libconfig': ('https://codeload.github.com/hyperrealm/libconfig/tar.gz/refs/tags/v1.8.3', '683f723d647befcdb36e1a4a61a83ae8c89dc7959ca872a7407b6c8152104162'),
}


def fetch(root, name):
    url, checksum = SOURCES[name]
    archive = root / (name + '.tar.gz')
    with urllib.request.urlopen(url,timeout=45) as response: data = response.read(32*1024*1024)
    if hashlib.sha256(data).hexdigest() != checksum: raise SystemExit('Source checksum mismatch: '+name)
    archive.write_bytes(data)
    destination = root / name; destination.mkdir()
    with tarfile.open(archive) as tar:
        # Reject links and special files: source release archives need regular files only.
        for member in tar.getmembers():
            parts = Path(member.name).parts[1:]
            if not parts: continue
            if any(part in ('..','') for part in parts) or Path(*parts).is_absolute(): raise SystemExit('Unsafe source archive')
            target = destination.joinpath(*parts)
            if member.isdir(): target.mkdir(parents=True,exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(tar.extractfile(member).read());target.chmod(member.mode&0o777)
            else: raise SystemExit('Unsupported source archive entry')
    return destination


def build(destination):
    destination = destination.resolve();destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sink-airplay-build-',dir='/tmp') as directory, (destination/'build.log').open('w') as log:
        root = Path(directory);prefix = root/'deps'
        def run(command,cwd,env=None):
            log.write('Running '+repr(command)+'\n');log.flush()
            subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
        config = fetch(root,'libconfig')
        run(['autoreconf','-fi'],config)
        run(['./configure','--prefix='+str(prefix),'--disable-cxx','--disable-examples','--disable-shared','--enable-static'],config)
        run(['make','-j4'],config);run(['make','install'],config)
        source = fetch(root,'shairport')
        bridge=Path(__file__).with_name('audio_sink.c')
        shutil.copyfile(bridge,source/'audio_pipe.c')
        run(['autoreconf','-fi'],source)
        env = os.environ.copy();env['PKG_CONFIG_PATH']=str(prefix/'lib/pkgconfig')+':'+env.get('PKG_CONFIG_PATH','')
        run(['./configure','--prefix='+str(root/'unused-install'),'--with-pipe','--with-stdout','--with-avahi',
             '--with-ssl=openssl','--with-metadata','--with-metadata-pipe','--with-ffmpeg','--with-soxr'],source,env)
        run(['make','-j4'],source,env)
        temporary = destination/'shairport-sync.new'
        shutil.copyfile(source/'shairport-sync',temporary);temporary.chmod(0o755)
        temporary.replace(destination/'shairport-sync')
        licenses=destination/'licenses';licenses.mkdir(exist_ok=True)
        for label,folder in [('shairport',source),('libconfig',config)]:
            for filename in ('COPYING','LICENSE','LICENSE.md','COPYING.LIB'):
                if (folder/filename).is_file():shutil.copyfile(folder/filename,licenses/(label+'-'+filename))
        (destination/'build-manifest.json').write_text(json.dumps({'sources':SOURCES,'bridge_version':1,'bridge_sha256':hashlib.sha256(bridge.read_bytes()).hexdigest()},indent=2)+'\n')
    return destination/'shairport-sync'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    try:print(build(parser.parse_args().directory))
    except subprocess.CalledProcessError as exc:raise SystemExit('Receiver build failed; inspect the destination build.log') from exc
