#!/usr/bin/env python3
"""Preflight and apply the project's ordered, pinned ESP-IDF patch stack."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile

def preflight_applied_prefix(idf, patches):
    # Later patches can legitimately modify an earlier patch's context. Recognize an
    # applied prefix by peeling it in disposable copies, then replay the complete
    # stack. Never reverse or partially preflight against the live SDK checkout.
    paths=set()
    for patch in patches:
        for name in re.findall(r'^\+\+\+ b/(.+)$',patch.read_text(),re.M):
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts:raise SystemExit('Unsafe patch path')
            paths.add(relative)
    applied=None
    with tempfile.TemporaryDirectory(prefix='wifi-sink-idf-preflight-') as temporary:
        root=Path(temporary)
        for count in range(len(patches),-1,-1):
            trial=root/str(count);trial.mkdir()
            for relative in paths:
                target=trial/relative;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(idf/relative,target)
            base=['git','-C',str(trial),'apply']
            sequence=[(patch,True) for patch in reversed(patches[:count])]+[(patch,False) for patch in patches]
            if all(subprocess.run(base+(['--reverse'] if reverse else [])+[str(patch)],capture_output=True).returncode==0
                   for patch,reverse in sequence):
                applied=count;break
    return applied

def patch_stack(project, revision):
    if revision != '051e4c4eb20346e043cdd47bc388fde75ff8af63':
        raise SystemExit('ESP-IDF v6.1.1 at the pinned revision is required')
    names = [f'esp-idf-v6.1.1-{name}.patch' for name in (
        'lwip-nodelay-allocation', 'avrc-playback-status', 'bt-linker-paths',
        'external-sbc-bound', 'fixed-sbc-endpoint',
    )]
    return [project/'patches'/name for name in names]

def main():
    project=Path(__file__).resolve().parents[1]
    argument=sys.argv[1] if len(sys.argv)>1 else os.environ.get('IDF_PATH')
    if not argument: raise SystemExit('Pass the ESP-IDF source directory or set IDF_PATH')
    idf=Path(argument).resolve()
    revision=subprocess.check_output(['git','-C',str(idf),'rev-parse','HEAD'],text=True).strip()
    patches=patch_stack(project, revision)
    applied=preflight_applied_prefix(idf,patches)
    if applied is None:raise SystemExit("SDK does not match a supported patch prefix; live checkout was not changed")
    for patch in patches[:applied]:print(f'Pinned patch already applied: {patch.name}')
    for patch in patches[applied:]:
        subprocess.run(['git','-C',str(idf),'apply',str(patch)],check=True)
        print(f'Applied pinned patch: {patch.name}')

if __name__=="__main__":main()
