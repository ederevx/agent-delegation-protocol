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
    assert json.loads(p.stdout)=={},p.stdout
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
    assert relay.returncode==0 and json.loads(relay.stdout)=={},relay.stdout
    relay_stop=subprocess.run([sys.executable,str(HOOK),'turn-stop'],input=json.dumps({'session_id':'relay'}),env=env,capture_output=True,text=True)
    assert json.loads(relay_stop.stdout)=={},relay_stop.stdout
    # Mutation is blocked before required delegation is satisfied.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'pm','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    blocked=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'pm','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(blocked.stdout)['hookSpecificOutput']['permissionDecision']=='deny',blocked.stdout
    # A context-pulling tool (Read, Grep, Glob...) is gated too, even on a
    # session that never had a prompt classified as requiring delegation at
    # all -- reading/searching costs parent context regardless of what the
    # classifier decided.
    ctx_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctx','tool_name':'Read'}),env=env,capture_output=True,text=True)
    assert json.loads(ctx_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny',ctx_denied.stdout
    subprocess.run([sys.executable,str(HOOK),'worker-start'],input=json.dumps({'session_id':'ctx','agent_id':'worker-a'}),env=env,capture_output=True,text=True)
    ctx_allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctx','tool_name':'Read'}),env=env,capture_output=True,text=True)
    assert json.loads(ctx_allowed.stdout)=={},ctx_allowed.stdout
    # Plain (non-mutating) Bash execution is exempt from the context-pulling
    # gate specifically -- the parent can run a read-only shell command with
    # zero lifecycle-visible workers observed, even on a session where
    # delegation is required. A mutating bash command on that same
    # zero-worker session is still denied, via the separate, untouched
    # _mutating check.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'ctxbash','prompt':'Update 12 files across independent modules.'}),env=env,capture_output=True,text=True)
    bash_allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctxbash','tool_name':'Bash','tool_input':{'command':'git status'}}),env=env,capture_output=True,text=True)
    assert json.loads(bash_allowed.stdout)=={},bash_allowed.stdout
    bash_mutating_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctxbash','tool_name':'Bash','tool_input':{'command':'rm -rf build'}}),env=env,capture_output=True,text=True)
    assert json.loads(bash_mutating_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny',bash_mutating_denied.stdout
    # An analysis-flagged turn ("review", "audit", ...) loses the plain-Bash
    # exemption: a read-only command is denied same as Read/Grep would be --
    # analysis is reserved for delegated agents with no escape hatch, so this
    # stays denied even after a worker has started, unlike the plain
    # context-pulling floor below.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'ctxbashaudit','prompt':'Please review and audit this module.'}),env=env,capture_output=True,text=True)
    bash_audit_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctxbashaudit','tool_name':'Bash','tool_input':{'command':'git status'}}),env=env,capture_output=True,text=True)
    assert json.loads(bash_audit_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny',bash_audit_denied.stdout
    subprocess.run([sys.executable,str(HOOK),'worker-start'],input=json.dumps({'session_id':'ctxbashaudit','agent_id':'worker-a'}),env=env,capture_output=True,text=True)
    bash_audit_still_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'ctxbashaudit','tool_name':'Bash','tool_input':{'command':'git status'}}),env=env,capture_output=True,text=True)
    assert json.loads(bash_audit_still_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny',bash_audit_still_denied.stdout
    # A small, non-research change with no signals at all stays parent-executable.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'small','prompt':'Fix the typo in the README.'}),env=env,capture_output=True,text=True)
    small_allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'small','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(small_allowed.stdout)=={},small_allowed.stdout
    # In-depth-research wording pushes execution to a worker even though the
    # turn is too small to trip the general delegation requirement.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'research','prompt':'Please figure out why this fails.'}),env=env,capture_output=True,text=True)
    research_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'research','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(research_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny',research_denied.stdout
    subprocess.run([sys.executable,str(HOOK),'worker-start'],input=json.dumps({'session_id':'research','agent_id':'worker-a'}),env=env,capture_output=True,text=True)
    research_allowed=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'research','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(research_allowed.stdout)=={},research_allowed.stdout
    # A stated budget at or above 5% of the window (but below the 25%
    # general-delegation threshold) pushes execution to a worker too.
    subprocess.run([sys.executable,str(HOOK),'prompt'],input=json.dumps({'session_id':'exectok','prompt':'Do this with a budget of 15000 tokens.'}),env=env,capture_output=True,text=True)
    exectok_denied=subprocess.run([sys.executable,str(HOOK),'pre-mutation'],input=json.dumps({'session_id':'exectok','tool_name':'Edit'}),env=env,capture_output=True,text=True)
    assert json.loads(exectok_denied.stdout)['hookSpecificOutput']['permissionDecision']=='deny',exectok_denied.stdout
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
      assert invoke('prompt', dict(worker_payload, prompt='Say hi.'), hook_env) == {}
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
