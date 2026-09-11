#!/usr/bin/env python3
"""Claude host installation and native lifecycle smoke tests."""
import json, os, subprocess, sys, tempfile, importlib.util
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "scripts/hosts/install.py"
HOOK = ROOT / "claude/hooks/delegation-enforcer.py"
SETTINGS = ROOT / "scripts/hosts/settings.py"
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
    # One session id per probe: an admitted spawn holds an active-worker
    # reservation until its start arrives, and these probe max_turns only.
    for requested in (None, 1, limit):
      assert invoke('pre-mutation', dict(parent, tool_name='Agent',
          session_id=f'probe-{tier}-{requested}',
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

def test_active_worker_cap(home, env):
  """A session may hold at most MAX_ACTIVE_WORKERS workers in flight at once."""
  import hashlib
  def invoke(event, payload):
    result = subprocess.run([sys.executable, str(HOOK), event],
        input=json.dumps(payload), env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
  def denied(body):
    assert body['hookSpecificOutput']['permissionDecision'] == 'deny', body
    return body['hookSpecificOutput']['permissionDecisionReason']
  def spawn(session, **extra):
    return invoke('pre-mutation', dict({'session_id': session,
        'tool_name': 'Agent',
        'tool_input': {'subagent_type': 'bulk-worker'}}, **extra))
  def start(session, worker, **extra):
    invoke('worker-start', dict({'session_id': session, 'agent_id': worker,
        'agent_type': 'bulk-worker'}, **extra))
  def complete(session, **extra):
    invoke('worker-complete', dict({'session_id': session}, **extra))
  def state(session):
    key = hashlib.sha256(session.encode()).hexdigest()
    return json.loads(
        (home / '.delegation-protocol/hook-state' / (key + '.json')).read_text())
  FULL = 'Active worker cap reached (10/10)'
  # Ten workers in flight fill the shared per-session cap; the parent's own
  # next spawn is denied even though nothing else about it is wrong.
  for index in range(10):
    start('cap', f'cap-{index}')
  assert FULL in denied(spawn('cap'))
  # A nested spawn runs under the parent's session_id, so a worker whose own
  # tier permits the target tier is still capped by the same active set.
  assert 'Active worker cap' in denied(spawn('cap', agent_id='cap-nested',
      agent_type='frontier-worker'))
  # An admitted spawn reserves its slot until the worker actually starts, so
  # two spawn calls issued back to back cannot both read the same free slot.
  complete('cap', agent_id='cap-0')
  assert spawn('cap', tool_use_id='cap-call') == {}
  assert FULL in denied(spawn('cap'))
  # A re-delivery of that same call already owns its slot, so it is admitted
  # at 10/10 rather than denied for being delivered twice -- on the parent
  # path and on a nested worker's path alike.
  assert spawn('cap', tool_use_id='cap-call') == {}
  assert spawn('cap', tool_use_id='cap-call', agent_id='cap-nested',
      agent_type='frontier-worker') == {}
  assert state('cap')['pending_spawns'] == ['cap-call'], state('cap')
  # The matching start consumes the reservation instead of adding to it, so
  # freeing one slot afterwards admits exactly one more spawn.
  start('cap', 'cap-0', tool_use_id='cap-call')
  assert state('cap')['pending_spawns'] == [], state('cap')
  complete('cap', agent_id='cap-0')
  assert spawn('cap') == {}
  # A reservation whose spawn never started does not outlive the round: the
  # next prompt drops it, while workers still running are kept across it.
  invoke('prompt', {'session_id': 'cap', 'prompt': 'Say hi.'})
  assert spawn('cap') == {}
  start('cap', 'cap-0')
  invoke('prompt', {'session_id': 'cap', 'prompt': 'Say hi.'})
  assert FULL in denied(spawn('cap'))
  # A carry-forward follow-up is still a new user turn, so it clears
  # reservations too, and the carry path keeps running workers untouched.
  invoke('prompt', {'session_id': 'carry',
      'prompt': 'Update 12 files across independent modules.'})
  for index in range(9):
    start('carry', f'carry-{index}')
  assert spawn('carry') == {}
  assert FULL in denied(spawn('carry'))
  invoke('prompt', {'session_id': 'carry', 'prompt': 'continue'})
  carried = state('carry')
  assert carried['requires_delegation'], carried  # the follow-up carried
  assert carried['pending_spawns'] == [], carried
  assert len(carried['concurrent']) == 9, carried
  assert spawn('carry') == {}
  assert FULL in denied(spawn('carry'))
  # A spawn that fails never becomes a worker. Claude reports that as a
  # completion naming only the failed tool_use_id, which must free its slot.
  for index in range(10):
    assert spawn('fail', tool_use_id=f'fail-{index}') == {}
  assert FULL in denied(spawn('fail'))
  complete('fail', tool_use_id='fail-0')
  assert spawn('fail', tool_use_id='fail-10') == {}
  assert FULL in denied(spawn('fail'))
  # A start carries no tool_use_id, so it consumes the oldest entry rather
  # than its own. The accounting is therefore a count, not an identity map:
  # an unmatched failure must still free one entry, or the worker that
  # consumed someone else's entry stays counted as running and as pending.
  assert spawn('skew', tool_use_id='skew-a') == {}
  assert spawn('skew', tool_use_id='skew-b') == {}
  start('skew', 'skew-worker')
  complete('skew', tool_use_id='skew-a')
  skewed = state('skew')
  assert skewed['pending_spawns'] == [], skewed
  assert len(skewed['concurrent']) == 1, skewed
  # A failure reported for a spawn this hook denied never held a slot, so it
  # must not free one that an admitted spawn is still holding -- on the
  # parent path and on a nested worker's path alike.
  held = state('fail')['pending_spawns']
  # A genuine stop names a worker that consumed its reservation when it
  # started, so it must not also pop an entry another spawn is still holding.
  complete('fail', agent_id='never-started')
  assert state('fail')['pending_spawns'] == held, state('fail')
  assert FULL in denied(spawn('fail', tool_use_id='denied-parent'))
  complete('fail', tool_use_id='denied-parent')
  assert state('fail')['pending_spawns'] == held, state('fail')
  assert 'Active worker cap' in denied(spawn('fail', tool_use_id='denied-nested',
      agent_id='fail-nested', agent_type='frontier-worker'))
  complete('fail', tool_use_id='denied-nested')
  assert state('fail')['pending_spawns'] == held, state('fail')
  # The single-use explicit authorization overrides the cap exactly once,
  # like every other denial, and never becomes a standing bypass.
  invoke('prompt', {'session_id': 'cap-auth',
      'prompt': 'I explicitly authorize this action.'})
  for index in range(10):
    start('cap-auth', f'auth-{index}')
  assert spawn('cap-auth') == {}
  assert 'Active worker cap' in denied(spawn('cap-auth'))


def main():
  with tempfile.TemporaryDirectory(prefix="claude-v2-") as raw:
    home=Path(raw); env=dict(os.environ, CLAUDE_CONFIG_DIR=str(home))
    r=subprocess.run([sys.executable,str(ENGINE),"install","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    test_routing_and_limits(env)
    test_active_worker_cap(home, env)
    m=json.loads((home/'.delegation-protocol/manifest.json').read_text()); assert m['version']==3 and m['release']=='automatic_release'
    assert (home/'.delegation-protocol/hook_adapter.py').is_symlink()
    assert (home/'agents/frontier-worker.md').is_symlink()
    assert (home/'agents/balanced-worker.md').is_symlink()
    assert (home/'agents/bulk-worker.md').is_symlink()
    assert (home/'agents/quick-worker.md').is_symlink()
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
    assert 'lowest capable worker' in json.loads(p.stdout)['hookSpecificOutput']['additionalContext'],p.stdout
    # Normalized lifecycle events release foreground workers automatically.
    for event, worker in (("worker-start", "worker-a"), ("worker-start", "worker-b"), ("worker-complete", "worker-a"), ("worker-complete", "worker-b")):
      q=subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'s','agent_id':worker}),env=env,capture_output=True,text=True)
      assert q.returncode==0,q.stderr
    q=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'s'}),env=env,capture_output=True,text=True)
    assert q.returncode==0 and json.loads(q.stdout)=={}
    # A relayed task-notification is a worker's/system's words, not the
    # user's -- even one dense with trip words (steps, token counts, review/
    # verify wording) must not be classified as a fresh delegation-requiring
    # prompt, and must not disturb the (already-clear) state it lands on.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'relay','prompt':'Say hi.'}),env=env,capture_output=True,text=True)
    notif_body=('<task-notification>\n<result>1. review\n2. verify\n3. analyze\n'
                '4. check\n5. audit\nstated budget of 999999 tokens\n'
                '</result>\n</task-notification>')
    relay=subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'relay','prompt':notif_body}),env=env,capture_output=True,text=True)
    assert relay.returncode==0 and 'lowest capable worker' in json.loads(relay.stdout)['hookSpecificOutput']['additionalContext'],relay.stdout
    relay_stop=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'relay'}),env=env,capture_output=True,text=True)
    assert json.loads(relay_stop.stdout)=={},relay_stop.stdout
    # Mutation is blocked before required delegation is satisfied.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'pm','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    blocked=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'pm','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(blocked.stdout)['hookSpecificOutput']['permissionDecision']=='deny',blocked.stdout
    # All models may analyze and execute. Only outstanding workload
    # delegation floors gate mutation; there are no analysis/tiny-work bans.
    for prompt in ('Fix the typo.', 'Review and audit this module.'):
      subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'open-actions','prompt':prompt}),env=env,capture_output=True,text=True,check=True)
      for event in ('worker-start', 'worker-complete'):
        subprocess.run([sys.executable,str(HOOK),event],input=json.dumps({'session_id':'open-actions','agent_id':'worker-a'}),env=env,capture_output=True,text=True,check=True)
      for name in ('Read', 'Grep', 'Bash', 'Edit', 'opaque_tool'):
        result=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'open-actions','tool_name':name,'tool_input':{'command':'git status'}}),env=env,capture_output=True,text=True,check=True)
        assert json.loads(result.stdout) == {}, (name, result.stdout)
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
    # A process environment flag cannot identify the current hook caller.
    # Parent Agent/Task calls remain available even with that inherited flag.
    inherited_env = dict(env, CLAUDE_CODE_CHILD_SESSION="1")
    def invoke(event, payload, hook_env=inherited_env):
      result = subprocess.run([sys.executable, str(HOOK), event],
          input=json.dumps(payload), env=hook_env, capture_output=True, text=True)
      assert result.returncode == 0, result.stderr
      return json.loads(result.stdout)
    for name in ('Agent', 'Task'):
      assert invoke('pre-mutation', {'session_id': 'pm', 'tool_name': name}) == {}
    # agent_type alone also occurs on a parent launched with --agent.
    assert invoke('pre-mutation', {'session_id': 'pm', 'agent_type': 'bulk-worker',
        'tool_name': 'Agent'}) == {}
    # Workers execute within a parent's session without clearing its debt.
    invoke('prompt', {'session_id': 'worker-scope',
        'prompt': 'Review and update 12 files across independent modules.'})
    import hashlib
    state_path = home / '.delegation-protocol/hook-state' / (
        hashlib.sha256(b'worker-scope').hexdigest() + '.json')
    before = state_path.read_bytes()
    worker_payload = {'session_id': 'worker-scope', 'agent_id': 'leaf-a',
        'agent_type': 'bulk-worker'}
    for hook_env in (env, inherited_env):
      for name in ('Read', 'Grep', 'Glob', 'Edit', 'Write', 'Bash'):
        assert invoke('pre-mutation', dict(worker_payload, tool_name=name,
            tool_input={'command': 'git status'}), hook_env) == {}, name
      for name in ('Agent', 'Task'):
        denied = invoke('pre-mutation', dict(worker_payload, tool_name=name), hook_env)
        assert denied['hookSpecificOutput']['permissionDecision'] == 'deny', denied
      assert 'lowest capable worker' in invoke('prompt', dict(worker_payload, prompt='Say hi.'), hook_env)['hookSpecificOutput']['additionalContext']
      assert invoke('turn-stop', worker_payload, hook_env) == {}
    assert state_path.read_bytes() == before
    assert invoke('turn-stop', {'session_id': 'worker-scope'})['decision'] == 'block'
    # Native lifecycle events still record workers under the parent session.
    invoke('worker-start', worker_payload)
    assert 'leaf-a' in json.loads(state_path.read_text())['observed']
    invoke('worker-complete', worker_payload)
    assert 'leaf-a' not in json.loads(state_path.read_text())['active']
    # Recursive delegation: a worker may spawn another worker only of a
    # strictly lower tier than its own, identified by the caller's own
    # `agent_type` and the Agent/Task call's own `subagent_type` argument.
    # Four-tier order, highest first: frontier-worker, balanced-worker,
    # bulk-worker, quick-worker.
    ALL_TIERS = ('frontier-worker', 'balanced-worker', 'bulk-worker', 'quick-worker')
    frontier_payload = {'session_id': 'tier', 'agent_id': 'top-a',
        'agent_type': 'frontier-worker'}
    # frontier-worker -> balanced-worker/bulk-worker/quick-worker: strictly
    # lower tier, allowed.
    for target in ('balanced-worker', 'bulk-worker', 'quick-worker'):
      tier_allowed = invoke('pre-mutation', dict(frontier_payload, tool_name='Agent',
          tool_input={'subagent_type': target}), env)
      assert tier_allowed == {}, (target, tier_allowed)
    # frontier-worker -> frontier-worker: same tier as itself, denied.
    tier_top_same_denied = invoke('pre-mutation', dict(frontier_payload, tool_name='Agent',
        tool_input={'subagent_type': 'frontier-worker'}), env)
    assert tier_top_same_denied['hookSpecificOutput']['permissionDecision'] == 'deny', tier_top_same_denied
    balanced_payload = {'session_id': 'tier', 'agent_id': 'mid-a',
        'agent_type': 'balanced-worker'}
    # balanced-worker -> bulk-worker/quick-worker: strictly lower tier, allowed.
    for target in ('bulk-worker', 'quick-worker'):
      tier_allowed = invoke('pre-mutation', dict(balanced_payload, tool_name='Agent',
          tool_input={'subagent_type': target}), env)
      assert tier_allowed == {}, (target, tier_allowed)
    # balanced-worker -> balanced-worker/frontier-worker: same tier or higher,
    # denied.
    for target in ('balanced-worker', 'frontier-worker'):
      tier_same_denied = invoke('pre-mutation', dict(balanced_payload, tool_name='Agent',
          tool_input={'subagent_type': target}), env)
      assert tier_same_denied['hookSpecificOutput']['permissionDecision'] == 'deny', tier_same_denied
    bulk_payload = {'session_id': 'tier', 'agent_id': 'leaf-b',
        'agent_type': 'bulk-worker'}
    # bulk-worker -> quick-worker: strictly lower tier, allowed.
    tier_bulk_allowed = invoke('pre-mutation', dict(bulk_payload, tool_name='Agent',
        tool_input={'subagent_type': 'quick-worker'}), env)
    assert tier_bulk_allowed == {}, tier_bulk_allowed
    # bulk-worker -> bulk-worker/balanced-worker/frontier-worker: same tier or
    # higher, denied.
    for target in ('bulk-worker', 'balanced-worker', 'frontier-worker'):
      tier_bulk_denied = invoke('pre-mutation', dict(bulk_payload, tool_name='Agent',
          tool_input={'subagent_type': target}), env)
      assert tier_bulk_denied['hookSpecificOutput']['permissionDecision'] == 'deny', tier_bulk_denied
    # quick-worker is already the lowest tier and cannot delegate at all,
    # regardless of what tier it names as the target.
    quick_payload = {'session_id': 'tier', 'agent_id': 'leaf-q',
        'agent_type': 'quick-worker'}
    for target in ALL_TIERS:
      tier_quick_denied = invoke('pre-mutation', dict(quick_payload, tool_name='Agent',
          tool_input={'subagent_type': target}), env)
      assert tier_quick_denied['hookSpecificOutput']['permissionDecision'] == 'deny', tier_quick_denied
    # The parent (non-worker session, no agent_id) is unaffected by any of
    # this: it may still spawn any tier freely, as before.
    for target in ALL_TIERS:
      parent_allowed = invoke('pre-mutation', {'session_id': 'pm', 'tool_name': 'Agent',
          'tool_input': {'subagent_type': target}}, env)
      assert parent_allowed == {}, parent_allowed
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
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps({'version':1}))
    old=subprocess.run([sys.executable,str(ENGINE),"install","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True)
    assert old.returncode != 0 and 'tagged v2 uninstaller' in old.stderr
    (home/'.delegation-protocol/manifest.json').write_text(json.dumps(m))
    r=subprocess.run([sys.executable,str(ENGINE),"uninstall","--host","claude","--home",str(home),"--repo",str(ROOT)],env=env,capture_output=True,text=True); assert r.returncode==0,r.stderr
    assert not (home/'agents/frontier-worker.md').exists()
    assert not (home/'agents/balanced-worker.md').exists()
    assert not (home/'agents/bulk-worker.md').exists()
    assert not (home/'agents/quick-worker.md').exists()
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
