#!/usr/bin/env python3
"""Publish credential-free firmware images from two existing build directories."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys

FIRMWARE=Path(__file__).resolve().parents[1]
OUTPUT=FIRMWARE.parent/'frontend/firmware'

def publish(board_id, builds):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',board_id):
        raise ValueError('Invalid board identifier')
    project=FIRMWARE/board_id
    board=json.loads((project/'board.json').read_text())
    if set(builds)!=set(board['outputs']):
        raise ValueError('Supply one build for every board output')
    output_dir=OUTPUT/board_id
    output_dir.mkdir(parents=True,exist_ok=True)
    images={}
    version=(FIRMWARE/'VERSION').read_text().strip()
    if not version or len(version)>31:raise ValueError('Invalid shared firmware version')
    for variant,build in builds.items():
        config=(build/'sdkconfig').read_text()
        expected='CONFIG_SINK_OUTPUT_BLUETOOTH=y' if variant=='bluetooth' else 'CONFIG_SINK_OUTPUT_PCM5102=y'
        if expected not in config:raise ValueError('Wrong output in '+str(build))
        description=json.loads((build/'project_description.json').read_text())
        if description['project_version']!=version:raise ValueError('Rebuild firmware: embedded version differs from firmware/VERSION')
        if Path(description['project_path']).resolve()!=project.resolve():raise ValueError('Build project does not match the board')
        if description['target']!=board['esptool_chip']:raise ValueError('Build target does not match the board')
        output=output_dir/(variant+'.bin')
        flash=board['flash']
        subprocess.run([sys.executable,'-m','esptool','--chip',board['esptool_chip'],'merge-bin','-o',str(output),
                        '--flash-mode',flash['mode'],'--flash-size',flash['size'],'--flash-freq',flash['frequency'],
                        '--target-offset',hex(flash['offset']),
                        hex(flash['bootloader_offset']),str(build/'bootloader/bootloader.bin'),
                        hex(flash['partition_table_offset']),str(build/'partition_table/partition-table.bin'),
                        hex(flash['app_offset']),str(build/description['app_bin'])],check=True)
        data=output.read_bytes()
        if flash['offset']+len(data)>board['config_offset']:raise ValueError('Image overlaps setup partition')
        images[variant]={'url':f'assets/firmware/{board_id}/{output.name}','sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}
    manifest_path=OUTPUT/'manifest.json'
    manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {'schema_version':1,'boards':{}}
    if manifest.get('schema_version')!=1 or not isinstance(manifest.get('boards'),dict):
        raise ValueError('Unsupported firmware catalog')
    boards=manifest['boards'] if manifest.get('firmware_version')==version else {}
    manifest={'schema_version':1,'firmware_version':version,'boards':boards}
    manifest['boards'][board_id]={**board,'images':images}
    temporary=manifest_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(manifest,indent=2)+'\n')
    temporary.replace(manifest_path)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board',required=True,help='Firmware directory name, such as esp32-wroom')
    parser.add_argument('--bluetooth',type=Path)
    parser.add_argument('--i2s',type=Path)
    args=parser.parse_args()
    publish(args.board,{key:value.resolve() for key,value in {'bluetooth':args.bluetooth,'i2s':args.i2s}.items() if value is not None})
