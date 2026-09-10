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

def test_routing_and_limits(env):
  def invoke(event, payload):
    result = subprocess.run([sys.executable, str(HOOK), event],
        input=json.dumps(payload), env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
  parent = {'session_id': 'routing-test'}
  for event, payload, hook_event in (
      ('prompt', dict(parent, prompt='Review this module.'), 'UserPromptSubmit'),
      ('worker-start', dict(parent, agent_id='routing-child', agent_type='bulk-worker'), 'SubagentStart')):
    body = invoke(event, payload)['hookSpecificOutput']
    assert body['hookEventName'] == hook_event
    for text in ('lowest capable worker', 'Skip unnecessary tiers', 'may route upward',
        'minimum adequate supported', 'without mandatory retries',
        'strictly downward', '128', '64', '32', '16'):
      assert text in body['additionalContext'], (text, body)
  for tier, limit in (('quick-worker',128), ('bulk-worker',64),
      ('balanced-worker',32), ('frontier-worker',16)):
    # Model and effort overrides are no longer ADP capability bans. A
    # requested native turn cap may narrow but cannot exceed tier policy.
    for requested in (None, 1, limit):
      assert invoke('pre-mutation', dict(parent, tool_name='Agent',
          tool_input={'subagent_type':tier, 'max_turns':requested,
              'model':'any-supported-model', 'effort':'low'})) == {}
    for requested in (limit + 1, 0, True, '16'):
      body = invoke('pre-mutation', dict(parent, tool_name='Agent',
          tool_input={'subagent_type':tier, 'max_turns':requested}))
      assert body['hookSpecificOutput']['permissionDecision'] == 'deny', body
    actor = dict(parent, agent_id='open-'+tier, agent_type=tier)
    for name in ('Read', 'Edit', 'Bash', 'functions.exec', 'opaque_tool'):
      assert invoke('pre-mutation', dict(actor, tool_name=name)) == {}, (tier, name)
    assert invoke('turn-stop', actor) == {}
  # Parent frontier admission has no balanced-first prerequisite or generic
  # profile ban. Workload floors still apply to parent mutation, not reads.
  assert invoke('pre-mutation', dict(parent, tool_name='Agent',
      tool_input={'subagent_type':'frontier-worker'})) == {}
  assert invoke('pre-mutation', dict(parent, tool_name='Agent',
      tool_input={'subagent_type':'general-purpose'})) == {}

def test_tool_budgets(home, env):
  import hashlib
  def invoke(event, payload):
    result = subprocess.run([sys.executable, str(HOOK), event],
        input=json.dumps(payload), env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
  def call(actor, call_id, tool='opaque_tool'):
    return invoke('pre-mutation', dict(actor, tool_name=tool, tool_use_id=call_id))
  def denied(body):
    assert body['hookSpecificOutput']['permissionDecision'] == 'deny', body
  def ledger(worker):
    key = hashlib.sha256(('worker-tool-budget:'+worker).encode()).hexdigest()
    return home / '.delegation-protocol/hook-state' / (key+'.json')
  for tier, limit in (('quick_worker',128), ('bulk_worker',64),
      ('balanced_worker',32), ('frontier_worker',16)):
    actor = {'session_id':'budget-parent', 'agent_id':'budget-'+tier, 'agent_type':tier}
    invoke('worker-start', actor)
    for index in range(limit):
      assert call(actor, str(index), ('Read','Edit','Bash','functions.exec')[index % 4]) == {}
      if index == 0:
        assert call(actor, str(index)) == {}  # duplicate hook, not a new attempt
    before = ledger(actor['agent_id']).read_bytes()
    body = call(actor, 'excess')
    denied(body)
    assert '0 remaining' in body['hookSpecificOutput']['permissionDecisionReason']
    assert 'plain final report' in body['hookSpecificOutput']['permissionDecisionReason']
    assert ledger(actor['agent_id']).read_bytes() == before
    assert call(actor, '0') == {}  # duplicate is idempotent even at the boundary
    assert invoke('turn-stop', actor) == {}  # final textual result is never blocked
    invoke('worker-complete', actor)
    invoke('prompt', {'session_id':'budget-parent', 'prompt':'Start a fresh task.'})
    invoke('worker-start', dict(actor, agent_type='quick_worker'))  # resume cannot refill
    denied(call(dict(actor, agent_type='quick_worker'), 'resumed'))
    denied(call(dict(actor, session_id='different-parent'), 'moved'))
    assert ledger(actor['agent_id']).read_bytes() == before
  # Missing/invalid call ids each consume one attempt; unknown identities use
  # the smallest tier cap and cannot claim a larger tier after initialization.
  actor = {'session_id':'unknown-parent', 'agent_id':'unknown-tier'}
  invoke('worker-start', actor)
  for index in range(16):
    assert call(dict(actor, agent_type='quick-worker'), None if index % 2 else 7) == {}
  denied(call(actor, 'extra'))
  assert json.loads(ledger('unknown-tier').read_text())['used'] == 16
  # Native start pins a known tier before the first tool event omits metadata.
  actor = {'session_id':'pin', 'agent_id':'pin-worker', 'agent_type':'bulk-worker'}
  invoke('worker-start', actor)
  assert call({'session_id':'pin', 'agent_id':'pin-worker'}, 'first') == {}
  assert json.loads(ledger('pin-worker').read_text())['limit'] == 64
  # Workers have isolated counters, regardless of a shared parent session.
  assert call({'session_id':'pin', 'agent_id':'other', 'agent_type':'frontier-worker'}, 'first') == {}
  assert json.loads(ledger('other').read_text())['used'] == 1
  # Corruption never silently grants a fresh budget.
  ledger('other').write_text('{broken')
  denied(call({'session_id':'pin', 'agent_id':'other', 'agent_type':'quick-worker'}, 'second'))
  assert ledger('other').read_text() == '{broken'
  # Native worker identity suffices even when parent session metadata is absent.
  assert call({'agent_id':'sessionless', 'agent_type':'frontier-worker'}, 'first') == {}
  assert json.loads(ledger('sessionless').read_text())['used'] == 1
  # No native worker identity means no claim of a lifetime worker budget.
  parent = {'session_id':'unidentified', 'agent_type':'frontier-worker'}
  for index in range(17):
    assert call(parent, str(index)) == {}
  # Recursive admission still applies, and all hook attempts count even if
  # another admission rule denies the attempted operation.
  actor = {'session_id':'recursion', 'agent_id':'recursive', 'agent_type':'quick_worker'}
  body = invoke('pre-mutation', dict(actor, tool_name='spawn_agent', tool_use_id='spawn',
      tool_input={'agent_type':'bulk_worker'}))
  denied(body)
  assert json.loads(ledger('recursive').read_text())['used'] == 1

def test_budget_failures(home):
  import importlib.util
  from unittest.mock import patch
  from concurrent.futures import ThreadPoolExecutor
  sys.path.insert(0, str(ROOT / 'scripts/hosts'))
  spec = importlib.util.spec_from_file_location('budget_test_adapter', ROOT/'scripts/hosts/hook_adapter.py')
  adapter = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(adapter)
  classifier = adapter._classifier(home)
  payload = {'agent_id':'failed-ledger', 'agent_type':'frontier-worker', 'tool_use_id':'call'}
  for target, error in (('_locked', TimeoutError('busy')), ('_save', PermissionError('denied'))):
    with patch.object(adapter, target, side_effect=error):
      result = adapter._worker_tool_budget(home, payload, classifier)
      assert result['hookSpecificOutput']['permissionDecision'] == 'deny', result
  # Simultaneous duplicate delivery spends one unit under the ledger lock.
  payload = dict(payload, agent_id='concurrent-ledger')
  with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(lambda _: adapter._worker_tool_budget(home, payload, classifier), range(8)))
  assert results == [None]*8, results
  path, _ = adapter._paths(home, 'worker-tool-budget:concurrent-ledger')
  assert json.loads(path.read_text())['used'] == 1
  for corrupted in ({}, [], {'limit':16,'used':-1,'seen':[]},
      {'limit':16,'used':0,'seen':'bad'}, {'limit':True,'used':0,'seen':[]}):
    path.write_text(json.dumps(corrupted))
    result = adapter._worker_tool_budget(home, payload, classifier)
    assert result['hookSpecificOutput']['permissionDecision'] == 'deny', result
    assert json.loads(path.read_text()) == corrupted

def main():
  test_powershell_wrappers()
  with tempfile.TemporaryDirectory(prefix="codex-v2-") as raw:
    home=Path(raw); env=dict(os.environ, CODEX_HOME=str(home))
    r=subprocess.run([sys.executable,str(ENGINE),"install","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    test_routing_and_limits(env)
    test_tool_budgets(home, env)
    test_budget_failures(home)
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
      assert 'lowest capable worker' in json.loads(audit.stdout)['hookSpecificOutput']['additionalContext'],audit.stdout
    audit_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'audit-0','tool_name':'exec_command','tool_input':{'cmd':'touch changed.txt'}}),env=env,capture_output=True,text=True)
    assert json.loads(audit_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    audit_read=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'audit-0','tool_name':'exec_command','tool_input':{'cmd':'git status --short'}}),env=env,capture_output=True,text=True)
    # Analysis has no parent/model-specific read prohibition.
    assert json.loads(audit_read.stdout)=={},audit_read.stdout
    p=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'s','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'s','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny'
    # A plain read needs no delegation floor on a simple turn.
    ctx_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctxpull','tool_name':'exec_command','tool_input':{'cmd':'cat file.txt'}}),env=env,capture_output=True,text=True)
    assert json.loads(ctx_denied.stdout)=={},ctx_denied.stdout
    subprocess.run([sys.executable,str(HOOK),'worker-start'],input=json.dumps({'session_id':'ctxpull','agent_id':'worker-a'}),env=env,capture_output=True,text=True)
    ctx_allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctxpull','tool_name':'exec_command','tool_input':{'cmd':'cat file.txt'}}),env=env,capture_output=True,text=True)
    assert json.loads(ctx_allowed.stdout)=={},ctx_allowed.stdout
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
    # Explicit, single-use, text-based authorization is the sole remaining
    # override -- no marker file, and it does not persist as a standing
    # bypass. It allows exactly the one otherwise-blocked action it names,
    # then enforcement reverts to normal, including for an immediate repeat
    # of the same tool call.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'byp','prompt':'Update 12 files across independent modules. I explicitly authorize this action.'}),env=env,capture_output=True,text=True)
    allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'byp','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(allowed.stdout)=={},allowed.stdout
    byp_denied_again=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'byp','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(byp_denied_again.stdout)['hookSpecificOutput']['permissionDecision']=='deny',byp_denied_again.stdout
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","codex","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
    assert not (home/'.delegation-protocol/hook_adapter.py').exists()
    assert not (home/'.delegation-protocol').exists()
  print('Codex host tests: PASS')
if __name__=='__main__': main()
