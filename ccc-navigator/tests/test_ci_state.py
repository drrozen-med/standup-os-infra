"""The board's CI rule: a commit is red only if its newest attempt failed.

A commit keeps every check-run it ever had. If any of them failed, the commit
is red for ever - even after the job was re-run and passed. That pinned
shared-marketing red after a smoke run that failed while .251 was down, long
after the next smoke run passed.

The rule under test: only the newest attempt of each check counts.
"""
import importlib.util
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent.parent / "bin" / "watchtower-collect.py"


def load():
    spec = importlib.util.spec_from_file_location("watchtower_collect", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wc = load()


def run(id_, name, conclusion, status="completed"):
    return {"id": id_, "name": name, "conclusion": conclusion, "status": status}


class CiStateTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_api(path):
            self.calls.append(path)
            if "check-runs" in path:
                return {"check_runs": self.runs}
            return {"state": self.state, "statuses": self.statuses}

        self.runs, self.state, self.statuses = [], "success", []
        wc.api = fake_api

    def state_of(self):
        return wc.ci_state("drrozen-med/x", "abc123")

    def test_a_passing_commit_is_green(self):
        self.runs = [run(1, "lint", "success"), run(2, "typecheck", "success")]
        self.assertEqual(self.state_of(), "green")

    def test_a_failing_check_is_red(self):
        self.runs = [run(1, "lint", "success"), run(2, "typecheck", "failure")]
        self.assertEqual(self.state_of(), "red")

    def test_a_re_run_that_passed_clears_the_red(self):
        # the first attempt failed (a runner died), the re-run passed
        self.runs = [run(1, "lint", "failure"), run(2, "lint", "success"),
                     run(3, "typecheck", "success")]
        self.assertEqual(self.state_of(), "green")

    def test_a_later_failure_still_counts(self):
        self.runs = [run(1, "lint", "success"), run(2, "lint", "failure")]
        self.assertEqual(self.state_of(), "red")

    def test_a_cancelled_check_is_red(self):
        self.runs = [run(1, "resident-mcq", "cancelled")]
        self.assertEqual(self.state_of(), "red")

    def test_a_re_run_after_a_cancellation_clears_it(self):
        self.runs = [run(1, "resident-mcq", "cancelled"), run(2, "resident-mcq", "success")]
        self.assertEqual(self.state_of(), "green")

    def test_a_running_check_is_pending(self):
        self.runs = [run(1, "lint", None, status="in_progress")]
        self.assertEqual(self.state_of(), "pending")

    def test_a_legacy_commit_status_failure_is_red(self):
        self.runs = [run(1, "lint", "success")]
        self.state = "failure"
        self.assertEqual(self.state_of(), "red")


if __name__ == "__main__":
    unittest.main()
