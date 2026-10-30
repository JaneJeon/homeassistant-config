#!/usr/bin/env python3
"""Exercise published native apps with real disk-full and SIGKILL failures.

Run on an isolated Docker host with a read-only, quiesced legacy baseline:
  python3 tools/test_native_snapshot_real_faults.py --source-root /private/legacy-baseline --reference-time 1793349751
Docker uses network=none, disposable Linux volumes and a 1 MiB tmpfs. Results
contain only aggregates, image digests and fault outcomes; source data is not printed.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

IMAGES = {'vm': 'ghcr.io/janejeon/victoria-metrics:0.1.0', 'vl': 'ghcr.io/janejeon/victoria-logs:0.1.0'}
HTTP = '''import json,sys,urllib.request\nu=json.loads(sys.stdin.read())\nwith urllib.request.urlopen(u,timeout=120) as r: sys.stdout.write(r.read().decode())\n'''

class Harness:
    def __init__(self, root, reference_time):
        self.root = root.resolve(strict=True); self.containers=[]; self.volumes=[]
        self.reference_time = reference_time
        self.results={}
    def run(self, *args, input=None, timeout=180, check=True):
        proc=subprocess.run(['docker',*args], input=input, text=True, capture_output=True, timeout=timeout)
        if check and proc.returncode: raise RuntimeError('Docker command failed: '+args[0])
        return proc
    def volume(self, label):
        name='native-fault-'+label+'-'+uuid.uuid4().hex[:12]
        self.run('volume','create',name); self.volumes.append(name); return name
    def config(self, image, volume, mode):
        config={'bootstrap_mode':mode,'retention':'1y' if image==IMAGES['vm'] else '2w','additionalArguments':'','enableHTTPAuth':False}
        self.run('run','--rm','--network','none','-i','-v',volume+':/data','--entrypoint','sh',image,'-c','chmod 700 /data; cat > /data/options.json; chmod 600 /data/options.json',input=json.dumps(config))
    def create(self, image, volume, source=None, tmpfs=False):
        args=['run','-d','--network','none','--memory','3g','-v',volume+':/data']
        if source: args+=['-v',str(source)+':/share/'+source.name+':ro']
        if tmpfs: args+=['--tmpfs','/data/work:rw,size=1048576']
        cid=self.run(*args,image).stdout.strip(); self.containers.append(cid); return cid
    def running(self,cid): return self.run('inspect','-f','{{.State.Running}}',cid).stdout.strip()=='true'
    def wait_healthy(self,cid,port,limit=900):
        deadline=time.monotonic()+limit
        while time.monotonic()<deadline:
            if not self.running(cid): raise RuntimeError('container exited before healthy')
            probe=self.run('exec',cid,'python3','-c',f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/health',timeout=2).read()",check=False)
            if probe.returncode==0: return
            time.sleep(.5)
        raise RuntimeError('container health timeout')
    def stop(self,cid,kill=False):
        if self.running(cid): self.run('kill' if kill else 'stop',*([] if kill else ['-t','60']),cid,timeout=90)
        exit_code=int(self.run('inspect','-f','{{.State.ExitCode}}',cid).stdout.strip())
        self.run('rm',cid); self.containers.remove(cid); return exit_code
    def inspect_path(self,image,volume,command):
        return self.run('run','--rm','--network','none','-v',volume+':/data:ro','--entrypoint','sh',image,'-c',command,check=False)
    def clone_backup(self,image,source,target):
        self.run('run','--rm','--network','none','-v',source+':/from:ro','-v',target+':/data','--entrypoint','sh',image,'-c','chmod 700 /data; cp -a /from/installation.json /from/backup /data/',timeout=1200)
        self.config(image,target,'restore_only')
    def manifest(self,image,volume):
        code="import hashlib,runpy; m=runpy.run_path('/usr/local/bin/native-backup'); m['validate_backup'](); print(hashlib.sha256(open('/data/backup/manifest.json','rb').read()).hexdigest())"
        return self.run('run','--rm','--network','none','-v',volume+':/data:ro','--entrypoint','python3',image,'-c',code,timeout=900).stdout.strip()
    def query(self,cid,engine):
        if engine=='vm':
            path='/api/v1/query?'+urllib.parse.urlencode({'query':'sum(count_over_time(up[1h]))','time':str(self.reference_time-86400),'nocache':'1'}); port=8428
        else:
            path='/select/logsql/query?'+urllib.parse.urlencode({'query':'* | stats count()','start':str(self.reference_time-86400),'end':str(self.reference_time)}); port=9428
        raw=self.run('exec','-i',cid,'python3','-c',HTTP,input=json.dumps(f'http://127.0.0.1:{port}'+path),timeout=300).stdout
        value=json.loads(raw)
        if engine=='vm':
            if value.get('status')!='success' or len(value['data']['result'])!=1: raise RuntimeError('VM aggregate missing')
            return float(value['data']['result'][0]['value'][1])
        if set(value)!={'count(*)'}: raise RuntimeError('unexpected VL aggregate')
        return int(value['count(*)'])
    def verify_restart(self,image,volume,port,engine,expected):
        cid=self.create(image,volume); self.wait_healthy(cid,port)
        actual=self.query(cid,engine)
        if actual!=expected: raise RuntimeError('restored aggregate mismatch')
        self.stop(cid); return True
    def run_engine(self,engine):
        image=IMAGES[engine]; port=8428 if engine=='vm' else 9428
        source=self.root/('victoria-metrics-data' if engine=='vm' else 'victoria-logs-data')
        if not source.is_dir(): raise RuntimeError('baseline source missing')
        seed=self.volume(engine+'-seed'); self.config(image,seed,'import_legacy')
        cid=self.create(image,seed,source); self.wait_healthy(cid,port)
        expected=self.query(cid,engine)
        if expected<=0: raise RuntimeError('empty historical aggregate')
        self.run('exec',cid,'/backup-pre',timeout=3600)
        if self.query(cid,engine)!=expected: raise RuntimeError('seed changed during export')
        self.stop(cid)
        original=self.manifest(image,seed)
        print(engine+': seeded and validated published-image snapshot',flush=True)

        # A real bounded tmpfs forces the running backup hook to hit ENOSPC.
        cid=self.create(image,seed,source,tmpfs=True); self.wait_healthy(cid,port)
        fault=self.run('exec',cid,'/backup-pre',timeout=3600,check=False)
        detail=(fault.stdout+'\n'+fault.stderr).lower()
        if fault.returncode==0 or 'no space left on device' not in detail: raise RuntimeError('export did not demonstrate ENOSPC')
        if self.query(cid,engine)!=expected: raise RuntimeError('database changed during failed export')
        self.stop(cid)
        if self.manifest(image,seed)!=original: raise RuntimeError('prior backup generation changed after ENOSPC')
        print(engine+': real ENOSPC during export preserved prior generation',flush=True)

        # A second actual ENOSPC while hydrating a new installation cannot
        # create an empty live DB. A restart without the limit must restore it.
        full=self.volume(engine+'-full'); self.clone_backup(image,seed,full)
        cid=self.create(image,full,tmpfs=True)
        deadline=time.monotonic()+900
        while self.running(cid) and time.monotonic()<deadline: time.sleep(.5)
        if self.running(cid): raise RuntimeError('ENOSPC restore did not finish')
        logs=self.run('logs',cid,check=False); text=(logs.stdout+'\n'+logs.stderr).lower()
        if 'no space left on device' not in text: raise RuntimeError('restore did not demonstrate ENOSPC')
        self.stop(cid)
        if self.inspect_path(image,full,'test -e /data/live').returncode==0: raise RuntimeError('ENOSPC restore created live data')
        if self.manifest(image,full)!=original: raise RuntimeError('restore ENOSPC damaged backup')
        self.verify_restart(image,full,port,engine,expected)
        print(engine+': real ENOSPC restore refused empty live; retry restored data',flush=True)

        # SIGKILL only after the actual copy/vmrestore has made staged files.
        interrupted=self.volume(engine+'-kill'); self.clone_backup(image,seed,interrupted)
        cid=self.create(image,interrupted)
        deadline=time.monotonic()+900; seen=False
        process_check="ps | grep -q '[v]mrestore-prod' && " if engine=='vm' else ''
        probe=process_check+"test -d /data/work/restore && [ -n \"$(find /data/work/restore -type f -print -quit)\" ]"
        while time.monotonic()<deadline:
            if not self.running(cid): raise RuntimeError('restore exited before interruption')
            if self.run('exec',cid,'sh','-c',probe,check=False).returncode==0:
                seen=True; break
            time.sleep(.1)
        if not seen: raise RuntimeError('did not observe staged hydration')
        kill_exit=self.stop(cid,kill=True)
        if kill_exit!=137: raise RuntimeError('SIGKILL did not produce exit status 137')
        staged=int(self.inspect_path(image,interrupted,"find /data/work/restore -type f | wc -l").stdout.strip())
        journal_present=self.inspect_path(image,interrupted,'test -f /data/operation.json').returncode==0
        if not staged: raise RuntimeError('no staged files remained after SIGKILL')
        if self.inspect_path(image,interrupted,'test -e /data/live').returncode==0: raise RuntimeError('live appeared during staged hydration')
        if self.manifest(image,interrupted)!=original: raise RuntimeError('interruption damaged backup')
        self.verify_restart(image,interrupted,port,engine,expected)
        print(engine+': SIGKILL during real hydration; retry restored original data',flush=True)
        image_id=self.run('image','inspect',image,'--format','{{.Id}}').stdout.strip()
        self.results[engine]={'image':image,'image_id':image_id,'aggregate':expected,'manifest_sha256':original,'export_enospc_preserved':True,'restore_enospc_recovered':True,'sigkill_hydration_recovered':True,'sigkill_exit_code':kill_exit,'staged_files_at_kill':staged,'journal_present_at_kill':journal_present,'export_enospc_exit_code':fault.returncode}
    def close(self):
        for cid in self.containers:
            self.run('rm','-f',cid,check=False,timeout=90)
        for volume in self.volumes:
            self.run('volume','rm',volume,check=False,timeout=180)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--source-root',type=Path,required=True); parser.add_argument('--reference-time',type=int,required=True,help='Unix timestamp within the quiesced baseline; preceding day must contain VM up and VL records'); parser.add_argument('--result',type=Path)
    args=parser.parse_args(); h=Harness(args.source_root,args.reference_time)
    try:
        for engine in ('vm','vl'): h.run_engine(engine)
        if args.result:
            with os.fdopen(os.open(args.result,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as result:
                result.write(json.dumps(h.results,sort_keys=True,indent=2)+'\n')
    finally: h.close()
