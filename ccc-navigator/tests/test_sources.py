import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources


class OutsourcererTests(unittest.TestCase):
    def setUp(self):
        sources._outsourcerer_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs_dir = Path(self.tmp.name) / "jobs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_folder_parsing_missing_files(self):
        job_dir = self.jobs_dir / "job1"
        job_dir.mkdir(parents=True)
        (job_dir / "status").write_text("running\n")
        (job_dir / "meta.json").write_text(json.dumps({"id": "job1", "provider": "openai", "model": "gpt-4", "cwd": "/code/foo", "verb": "test"}))
        rows, meta = sources.outsourcerer_jobs(str(self.jobs_dir))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "job1")
        self.assertEqual(rows[0]["engine"], "outsourcerer")
        self.assertEqual(rows[0]["job_status"], "running")
        self.assertTrue(rows[0]["is_live"])
        self.assertEqual(rows[0]["last_text"], "")

    def test_max_age_filter(self):
        job_dir = self.jobs_dir / "job2"
        job_dir.mkdir(parents=True)
        status = job_dir / "status"
        status.write_text("done\n")
        old = time.time() - 10 * 86400
        os.utime(status, (old, old))
        rows, meta = sources.outsourcerer_jobs(str(self.jobs_dir), max_age_days=7)
        self.assertEqual(len(rows), 0)

    def test_live_flags_and_prompt(self):
        job_dir = self.jobs_dir / "job3"
        job_dir.mkdir(parents=True)
        (job_dir / "status").write_text("launching\n")
        (job_dir / "meta.json").write_text(json.dumps({"id": "job3", "provider": "anthropic", "model": "claude", "cwd": "/tmp", "prompt": "hello world " * 10}))
        (job_dir / "last.txt").write_text("x" * 500)
        rows, _ = sources.outsourcerer_jobs(str(self.jobs_dir))
        self.assertTrue(rows[0]["is_live"])
        self.assertEqual(rows[0]["last_text"], "x" * 400)
        self.assertIn("hello world", rows[0]["display_name"])


class VM251Tests(unittest.TestCase):
    def setUp(self):
        sources._vm251_cache = {"at": 0.0, "val": ([], {"found": False})}
        os.environ.pop("CCC_NAV_VM251", None)

    def tearDown(self):
        os.environ.pop("CCC_NAV_VM251", None)

    @mock.patch("subprocess.run")
    def test_vm251_parsing(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps({
            "generated_at": time.time(),
            "host": "251",
            "containers": [{"name": "app1", "state": "RUNNING", "agents": [{"pid": 1234, "cmd": "claude -p test"}]}]
        }).encode()
        rows, meta = sources.vm251_agents(host="187.124.181.251", ttl=180)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["session_id"].startswith("251:"))
        self.assertEqual(rows[0]["engine"], "vm251")
        self.assertTrue(rows[0]["is_live"])

    def test_disabled_env_var(self):
        os.environ["CCC_NAV_VM251"] = "0"
        rows, meta = sources.vm251_agents()
        self.assertEqual(rows, [])
        self.assertTrue(meta.get("disabled"))

    @mock.patch("subprocess.run")
    def test_ssh_failure_returns_cached_or_empty_without_raising(self, mock_run):
        mock_run.side_effect = Exception("ssh failed")
        rows, meta = sources.vm251_agents(host="187.124.181.251", ttl=0.001)  # will trigger refresh
        time.sleep(0.1)
        # Should not raise; returns cached/empty
        self.assertIsInstance(rows, list)
        self.assertIsInstance(meta, dict)


if __name__ == "__main__":
    unittest.main()
