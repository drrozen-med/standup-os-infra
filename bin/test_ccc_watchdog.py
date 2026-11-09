"""The watchdog switches a disabled or unloaded launchd job back on (dry run, no real launchctl changes)."""
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class Watchdog(unittest.TestCase):
    def run_dog(self, jobs, plists):
        with tempfile.TemporaryDirectory() as d:
            for j in plists:
                open(os.path.join(d, j + ".plist"), "w").close()
            env = dict(os.environ, WATCHDOG_DRY="1", WATCHDOG_JOBS=jobs, WATCHDOG_AGENTS_DIR=d,
                       WATCHDOG_STATE=os.path.join(d, "state"), CCC_URL="http://127.0.0.1:9/")
            return subprocess.run(["bash", os.path.join(HERE, "ccc-watchdog.sh")], env=env,
                                  capture_output=True, text=True, timeout=60).stdout

    def test_unloaded_job_is_switched_back_on(self):
        out = self.run_dog("com.example.watchdog-test-notloaded", ["com.example.watchdog-test-notloaded"])
        self.assertIn("switched off, switching it back on", out)
        self.assertIn("DRY: launchctl enable", out)
        self.assertIn("DRY: launchctl bootstrap", out)
        self.assertIn("DRY: python3", out)

    def test_job_without_plist_is_left_alone(self):
        out = self.run_dog("com.example.watchdog-test-noplist", [])
        self.assertNotIn("switching it back on", out)


if __name__ == "__main__":
    unittest.main()
