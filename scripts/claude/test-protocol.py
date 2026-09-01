#!/usr/bin/env python3
"""v2 Claude host installation and lifecycle smoke tests."""
import json, os, subprocess, sys, tempfile, importlib.util
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts/hosts/install.py"
HOOK = ROOT / "claude/hooks/delegation-enforcer.py"
SETTINGS = ROOT / "scripts/hosts/settings.py"
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
    # Normalized lifecycle events release foreground workers automatically.
    for event, worker in (("worker-start", "worker-a"), ("worker-start", "worker-b"), ("worker-complete", "worker-a"), ("worker-complete", "worker-b")):
      q=subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'s','agent_id':worker}),env=env,capture_output=True,text=True)
      assert q.returncode==0,q.stderr
    q=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'s'}),env=env,capture_output=True,text=True)
    assert q.returncode==0 and json.loads(q.stdout)=={}
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps({'version':1}))
    old=subprocess.run([sys.executable,str(ENGINE),"install","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert old.returncode != 0 and 'tagged v1 uninstaller' in old.stderr
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps(m))
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
    # Settings manager preserves unrelated values and rejects invalid JSON
    # without replacing the user's file.
    spec=importlib.util.spec_from_file_location('settings', SETTINGS); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    cfg=home/'settings.json'; cfg.write_text(json.dumps({'keep': 7, 'env': {'CUSTOM':'x'}}))
    mod.install('claude', home, home/'hook.py', sys.executable)
    data=json.loads(cfg.read_text()); assert data['keep']==7 and data['env']['CUSTOM']=='x'
    before=cfg.read_bytes(); cfg.write_text('{invalid')
    try: mod.install('claude', home, home/'hook.py', sys.executable)
    except ValueError: pass
    else: raise AssertionError('invalid settings accepted')
    assert cfg.read_bytes()==b'{invalid'
  print('Claude v2 host tests: PASS')
if __name__=='__main__': main()
