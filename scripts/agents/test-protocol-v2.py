#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; CTL=ROOT/"scripts/agents/delegationctl.py"; ADAPTER=ROOT/"scripts/agents/reference-adapter.py"
sys.path.insert(0,str(ROOT/"scripts/agents"))
from lane_service import Lane
class V2(unittest.TestCase):
 def runctl(self,*args): return subprocess.run([sys.executable,str(CTL),*args],text=True,capture_output=True)
 def test_catalog(self): self.assertEqual(self.runctl("validate").returncode,0)
 def test_select_runtime(self):
  r=self.runctl("select","--route","bulk","--runtime","codex","--platform","linux","--mode","read","--workspace","shared","--function","audit"); self.assertEqual(json.loads(r.stdout)["id"],"native-codex-bulk")
 def test_select_rejects_wrong_runtime(self):
  r=self.runctl("select","--route","bulk","--runtime","claude","--platform","linux","--mode","read","--workspace","shared","--function","audit"); self.assertEqual(json.loads(r.stdout)["id"],"native-claude-bulk")
 def test_reference_run_and_session(self):
  env={**os.environ,"DELEGATION_V2_STATE":tempfile.mkdtemp()}
  run=subprocess.run([sys.executable,str(ADAPTER)],input=json.dumps({"schema_version":2,"task":{"prompt":"hello"}}),text=True,capture_output=True,env=env); self.assertEqual(json.loads(run.stdout)["status"],"success")
  start=subprocess.run([sys.executable,str(ADAPTER)],input='{"schema_version":2,"operation":"start"}',text=True,capture_output=True,env=env); token=json.loads(start.stdout)["token"]
  step=subprocess.run([sys.executable,str(ADAPTER)],input=json.dumps({"schema_version":2,"operation":"step","token":token}),text=True,capture_output=True,env=env); self.assertEqual(json.loads(step.stdout)["status"],"complete")
 def test_lane_fifo_reentry_expiry_and_crash_release(self):
  lane=Lane(lease_seconds=1)
  token=lane.acquire("a"); self.assertEqual(lane.acquire("a",token),token)
  self.assertFalse(lane.release("b",token)); self.assertTrue(lane.heartbeat("a",token)); self.assertTrue(lane.release("a",token))
  token=lane.acquire("a"); import time; time.sleep(1.05); self.assertFalse(lane.release("a",token)); self.assertEqual(lane.acquire("b"),lane.current.token)
if __name__=="__main__": unittest.main()
