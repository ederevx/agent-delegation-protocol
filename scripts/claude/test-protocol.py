#!/usr/bin/env python3
"""v2 Claude host installation and lifecycle smoke tests."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts/hosts/install.py"
HOOK = ROOT / "claude/hooks/delegation-enforcer.py"
def main():
  with tempfile.TemporaryDirectory(prefix="claude-v2-") as raw:
    home=Path(raw); env=dict(os.environ, CLAUDE_CONFIG_DIR=str(home))
    r=subprocess.run([sys.executable,str(ENGINE),"install","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    m=json.loads((home/'.delegation-protocol/manifest.json').read_text()); assert m['version']==2 and m['release']=='automatic_release'
    assert (home/'.delegation-protocol/delegationctl.py').is_symlink()
    p=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'s','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert 'hookSpecificOutput' in json.loads(p.stdout)
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps({'version':1}))
    old=subprocess.run([sys.executable,str(ENGINE),"install","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert old.returncode != 0 and 'tagged v1 uninstaller' in old.stderr
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps(m))
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
  print('Claude v2 host tests: PASS')
if __name__=='__main__': main()
