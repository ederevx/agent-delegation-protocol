#!/usr/bin/env python3
"""Claude host installation and native lifecycle smoke tests."""
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
    m=json.loads((home/'.delegation-protocol/manifest.json').read_text()); assert m['version']==3 and m['release']=='automatic_release'
    assert (home/'.delegation-protocol/hook_adapter.py').is_symlink()
    assert (home/'agents/bulk-worker.md').is_symlink()
    assert (home/'agents/balanced-worker.md').is_symlink()
    # Only native lifecycle events (plus the documented Agent-failure signal)
    # ever wire to worker-start/worker-complete -- no arbitrary tool call
    # (which is how ACP/AALP traffic would otherwise reach the hook) can ever
    # produce delegation evidence.
    installed_settings=json.loads((home/'settings.json').read_text())
    hooks=installed_settings['hooks']
    def commands(event):
      return [h['command'] for group in hooks.get(event, []) for h in group.get('hooks', [])]
    assert any(c.endswith(' worker-start') for c in commands('SubagentStart'))
    assert any(c.endswith(' worker-complete') for c in commands('SubagentStop'))
    assert any(c.endswith(' pre-mutation') for c in commands('PreToolUse'))
    worker_wired_events={e for e in hooks if any(c.endswith((' worker-start',' worker-complete')) for c in commands(e))}
    assert worker_wired_events=={'SubagentStart','SubagentStop','PostToolUseFailure'},worker_wired_events
    p=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'s','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert 'hookSpecificOutput' in json.loads(p.stdout)
    # Normalized lifecycle events release foreground workers automatically.
    for event, worker in (("worker-start", "worker-a"), ("worker-start", "worker-b"), ("worker-complete", "worker-a"), ("worker-complete", "worker-b")):
      q=subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'s','agent_id':worker}),env=env,capture_output=True,text=True)
      assert q.returncode==0,q.stderr
    q=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'s'}),env=env,capture_output=True,text=True)
    assert q.returncode==0 and json.loads(q.stdout)=={}
    # Mutation is blocked before required delegation is satisfied.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'pm','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    blocked=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'pm','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(blocked.stdout)['hookSpecificOutput']['permissionDecision']=='deny',blocked.stdout
    # Stop detects unsatisfied delegation instead of silently ending the turn.
    stop_unmet=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'pm'}),env=env,capture_output=True,text=True)
    stop_body=json.loads(stop_unmet.stdout)
    assert stop_body.get('decision')=='block' and stop_body.get('reason'),stop_body
    # Multi-agent/fan-out requires real concurrent overlap: two workers that
    # each start and complete before the next starts never overlap, so the
    # requirement must still read as unmet even though two distinct workers
    # were observed.
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
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps({'version':1}))
    old=subprocess.run([sys.executable,str(ENGINE),"install","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert old.returncode != 0 and 'tagged v2 uninstaller' in old.stderr
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps(m))
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
    assert not (home/'agents/bulk-worker.md').exists()
    assert not (home/'agents/balanced-worker.md').exists()
    # Settings manager preserves unrelated values and hooks, rejects invalid
    # JSON without replacing the user's file, and uninstall preserves both
    # across removal of only the protocol's own hook entries.
    spec=importlib.util.spec_from_file_location('settings', SETTINGS); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    cfg=home/'settings.json'
    cfg.write_text(json.dumps({'keep': 7, 'env': {'CUSTOM':'x'}, 'hooks': {'Notification': [{'hooks':[{'type':'command','command':'echo hi'}]}]}}))
    mod.install('claude', home, home/'hook.py', sys.executable)
    data=json.loads(cfg.read_text())
    assert data['keep']==7 and data['env']['CUSTOM']=='x'
    assert data['hooks']['Notification'][0]['hooks'][0]['command']=='echo hi'
    assert 'UserPromptSubmit' in data['hooks']
    before=cfg.read_bytes(); cfg.write_text('{invalid')
    try: mod.install('claude', home, home/'hook.py', sys.executable)
    except ValueError: pass
    else: raise AssertionError('invalid settings accepted')
    assert cfg.read_bytes()==b'{invalid'
    cfg.write_bytes(before)
    mod.uninstall('claude', home)
    data=json.loads(cfg.read_text())
    assert data['keep']==7 and data['env'].get('CUSTOM')=='x'
    assert data['hooks']['Notification'][0]['hooks'][0]['command']=='echo hi'
    assert 'UserPromptSubmit' not in data.get('hooks',{})
  print('Claude host tests: PASS')
if __name__=='__main__': main()
