#!/usr/bin/env python3
"""Codex host installation and native lifecycle smoke tests."""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts/hosts/install.py"
HOOK = ROOT / "codex/hooks/delegation-enforcer.py"
POWERSHELL_WRAPPERS = (
  ROOT / "scripts/codex/install.ps1",
  ROOT / "scripts/codex/uninstall.ps1",
  ROOT / "scripts/claude/install.ps1",
  ROOT / "scripts/claude/uninstall.ps1",
)

def test_powershell_wrappers():
  for wrapper in POWERSHELL_WRAPPERS:
    source = wrapper.read_text()
    assert not re.search(r'^\s*\$home\s*=', source, re.IGNORECASE | re.MULTILINE), f'{wrapper} assigns PowerShell read-only $HOME'
  powershell = shutil.which('pwsh') or shutil.which('powershell')
  if not powershell:
    return
  with tempfile.TemporaryDirectory(prefix="codex-v2-powershell-") as raw:
    for host, action in (('codex', 'install'), ('codex', 'uninstall'), ('claude', 'install'), ('claude', 'uninstall')):
      home = Path(raw) / host
      env = dict(os.environ)
      if host == 'codex':
        env.update(CODEX_HOME=str(home), CODEX_PYTHON='Write-Output')
      else:
        env.update(CLAUDE_CONFIG_DIR=str(home), PYTHON='Write-Output')
      wrapper = ROOT / f'scripts/{host}/{action}.ps1'
      result = subprocess.run([powershell, '-NoProfile', '-File', str(wrapper)], env=env, capture_output=True, text=True)
      assert result.returncode == 0, result.stderr or result.stdout
      assert action in result.stdout and f'--host\n{host}' in result.stdout.replace('\r', ''), result.stdout

def main():
  test_powershell_wrappers()
  with tempfile.TemporaryDirectory(prefix="codex-v2-") as raw:
    home=Path(raw); env=dict(os.environ, CODEX_HOME=str(home))
    r=subprocess.run([sys.executable,str(ENGINE),"install","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    m=json.loads((home/'.delegation-protocol/manifest.json').read_text()); assert m['version']==3 and m['release']=='session_release'
    hooks=json.loads((home/'hooks.json').read_text())['hooks']
    assert 'SubagentStart' in hooks and 'SubagentStop' in hooks
    assert 'PostToolUse' not in hooks, 'Codex completion must use native subagent lifecycle events'
    # Only native lifecycle events ever wire to worker-start/worker-complete --
    # no arbitrary tool call (which is how ACP/AALP traffic would otherwise
    # reach the hook) can ever produce delegation evidence.
    def commands(event):
      return [h['command'] for group in hooks.get(event, []) for h in group.get('hooks', [])]
    assert any(c.endswith(' worker-start') for c in commands('SubagentStart'))
    assert any(c.endswith(' worker-complete') for c in commands('SubagentStop'))
    assert any(c.endswith(' pre-mutation') for c in commands('PreToolUse'))
    worker_wired_events={e for e in hooks if any(c.endswith((' worker-start',' worker-complete')) for c in commands(e))}
    assert worker_wired_events=={'SubagentStart','SubagentStop'},worker_wired_events
    assert (home/'.delegation-protocol/hook_adapter.py').is_symlink()
    worker = home/'agents/bulk_worker.toml'; worker.write_text('user change\n')
    r2=subprocess.run([sys.executable,str(ENGINE),"install","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r2.returncode != 0 and 'unowned destination' in r2.stderr
    for index, prompt in enumerate(('Check hooks and evaluate', 'Inspect hooks', 'Verify hooks', 'Diagnose hooks')):
      audit=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':f'audit-{index}','prompt':prompt}),env=env,capture_output=True,text=True)
      assert audit.returncode==0,audit.stderr
      # Enforcement is the deterministic PreToolUse/Stop gate below, not
      # injected prompt context -- the prompt event only records state.
      assert json.loads(audit.stdout)=={},audit.stdout
    audit_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'audit-0','tool_name':'exec_command','tool_input':{'cmd':'touch changed.txt'}}),env=env,capture_output=True,text=True)
    assert json.loads(audit_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    audit_read=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'audit-0','tool_name':'exec_command','tool_input':{'cmd':'git status --short'}}),env=env,capture_output=True,text=True)
    assert json.loads(audit_read.stdout)=={},audit_read.stdout
    p=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'s','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'s','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    for event, worker in (('worker-start','worker-a'), ('worker-start','worker-b'), ('worker-complete','worker-a'), ('worker-complete','worker-b')):
      q=subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'s','agent_id':worker}),env=env,capture_output=True,text=True)
      assert q.returncode==0,q.stderr
    stopped=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'s'}),env=env,capture_output=True,text=True)
    assert stopped.returncode==0 and json.loads(stopped.stdout)=={}, 'session release created impossible finished-worker warning'
    # Stop detects unsatisfied delegation instead of silently ending the turn.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'unmet','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    stop_unmet=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'unmet'}),env=env,capture_output=True,text=True)
    stop_body=json.loads(stop_unmet.stdout)
    assert stop_body.get('decision')=='block' and stop_body.get('reason'),stop_body
    # Multi-agent/fan-out requires real concurrent overlap. Under Codex's
    # session_release mode a completed worker stays "held" (no inferred
    # dismissal debt), so this specifically exercises that a strictly
    # sequential start/complete/start/complete pair -- which never overlaps in
    # real time -- must not be misread as concurrent evidence.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'seq','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    for event,worker in (('worker-start','worker-a'),('worker-complete','worker-a'),('worker-start','worker-b'),('worker-complete','worker-b')):
      subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'seq','agent_id':worker}),env=env,capture_output=True,text=True)
    seq_stop=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'seq'}),env=env,capture_output=True,text=True)
    seq_body=json.loads(seq_stop.stdout)
    assert seq_body.get('decision')=='block' and 'concurrently' in seq_body.get('reason',''),seq_body
    # Owner bypass lifts both gates only while the marker file is present.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'byp','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    (home/'.delegation-protocol/bypass').write_text('owner note\n')
    allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'byp','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(allowed.stdout)=={},allowed.stdout
    byp_stop=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'byp'}),env=env,capture_output=True,text=True)
    assert json.loads(byp_stop.stdout)=={},byp_stop.stdout
    (home/'.delegation-protocol/bypass').unlink()
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
    assert not (home/'.delegation-protocol/hook_adapter.py').exists()
    assert not (home/'.delegation-protocol').exists()
  print('Codex host tests: PASS')
if __name__=='__main__': main()
