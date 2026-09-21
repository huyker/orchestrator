import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.self_update import SelfUpdater


def run(args, cwd=None):
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.strip()


class SelfUpdateTests(unittest.TestCase):
    def _repos(self, root: Path):
        origin = root / "origin.git"
        seed = root / "seed"
        local = root / "local"
        run(["git", "init", "--bare", str(origin)])
        run(["git", "clone", str(origin), str(seed)])
        run(["git", "config", "user.email", "t@example.com"], seed)
        run(["git", "config", "user.name", "Test"], seed)
        (seed / "app.py").write_text("print('v1')\n")
        (seed / "projects.json").write_text(json.dumps({
            "schema_version": 1,
            "projects": [{"id": "base", "repo": "acme/base", "issues_repo": "acme/base"}],
        }, indent=2) + "\n")
        run(["git", "add", "."], seed)
        run(["git", "commit", "-m", "base"], seed)
        run(["git", "branch", "-M", "main"], seed)
        run(["git", "push", "-u", "origin", "main"], seed)
        run(["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"])
        run(["git", "clone", str(origin), str(local)])
        run(["git", "config", "user.email", "t@example.com"], local)
        run(["git", "config", "user.name", "Test"], local)
        return origin, seed, local

    def test_update_waits_for_active_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, seed, local = self._repos(root)
            old_sha = run(["git", "rev-parse", "HEAD"], local)
            (seed / "version.txt").write_text("v2")
            run(["git", "add", "."], seed)
            run(["git", "commit", "-m", "v2"], seed)
            run(["git", "push"], seed)

            updater = SelfUpdater(local, registry_file=local / "projects.json")
            status = updater.check_and_apply(active_task=True)

            self.assertEqual(status["state"], "pending")
            self.assertEqual(run(["git", "rev-parse", "HEAD"], local), old_sha)

    def test_fast_forward_preserves_runtime_registered_projects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, seed, local = self._repos(root)

            local_registry = json.loads((local / "projects.json").read_text())
            local_registry["projects"].append({
                "id": "local",
                "repo": "acme/local",
                "issues_repo": "acme/local",
            })
            (local / "projects.json").write_text(json.dumps(local_registry, indent=2) + "\n")

            remote_registry = json.loads((seed / "projects.json").read_text())
            remote_registry["projects"].append({
                "id": "remote",
                "repo": "acme/remote",
                "issues_repo": "acme/remote",
            })
            (seed / "projects.json").write_text(json.dumps(remote_registry, indent=2) + "\n")
            (seed / "app.py").write_text("print('v2')\n")
            run(["git", "add", "."], seed)
            run(["git", "commit", "-m", "v2"], seed)
            run(["git", "push"], seed)

            updater = SelfUpdater(local, registry_file=local / "projects.json")
            status = updater.check_and_apply(active_task=False)

            self.assertEqual(status["state"], "updated")
            ids = [x["id"] for x in json.loads((local / "projects.json").read_text())["projects"]]
            self.assertEqual(ids, ["base", "remote", "local"])
            self.assertEqual((local / "app.py").read_text(), "print('v2')\n")


if __name__ == "__main__":
    unittest.main()
