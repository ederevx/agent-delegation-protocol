#!/usr/bin/env python3
"""v2 Codex host installation and clean-break smoke tests."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts/hosts/install.py"
HOOK = ROOT / "codex/hooks/delegation-enforcer.py"
def main():
  with tempfile.TemporaryDirectory(prefix="codex-v2-") as raw:
    home=Path(raw); env=dict(os.environ, CODEX_HOME=str(home))
    r=subprocess.run([sys.executable,str(ENGINE),"install","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    m=json.loads((home/'.delegation-protocol/manifest.json').read_text()); assert m['version']==2 and m['release']=='session_release'
    assert (home/'.delegation-protocol/delegationctl.py').is_symlink()
    assert not (home/'.delegation-protocol/mux-scheduler.py').exists()
    worker = home/'agents/bulk_worker.toml'; worker.write_text('user change\n')
    r2=subprocess.run([sys.executable,str(ENGINE),"install","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r2.returncode != 0 and 'unowned destination' in r2.stderr
    p=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'s','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'s','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    for event, worker in (('worker-start','worker-a'), ('worker-start','worker-b'), ('worker-complete','worker-a'), ('worker-complete','worker-b')):
      q=subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'s','agent_id':worker}),env=env,capture_output=True,text=True)
      assert q.returncode==0,q.stderr
    stopped=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'s'}),env=env,capture_output=True,text=True)
    assert stopped.returncode==0 and json.loads(stopped.stdout)=={}, 'session release created impossible finished-worker warning'
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
  print('Codex v2 host tests: PASS')
if __name__=='__main__': main()
