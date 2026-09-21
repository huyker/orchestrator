import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.models import Settings
from orchestrator.workspace import WorkspaceManager


def settings_for(td):
    root = Path(td)
    root.mkdir(parents=True, exist_ok=True)
    reg = root / "projects.json"
    reg.write_text('{"schema_version":1,"projects":[]}')
    return Settings(
        "o/c", "", reg, root / "runtime", root / "managed", 5, "ssh",
        "agy", "medium", 30, 10, 10, "127.0.0.1", 0, ("o",)
    )


class LocalWorkspace(WorkspaceManager):
    def __init__(self, settings, origin):
        super().__init__(settings)
        self.origin = origin

    def clone_url(self, repo):
        return str(self.origin)


class WorkspaceTests(unittest.TestCase):
    def _origin(self, root: Path):
        origin = root / "origin.git"
        seed = root / "seed"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "clone", str(origin), str(seed)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
        (seed / "README.md").write_text("v1")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        return origin, seed

    def test_managed_repo_clones_under_owner_repo_and_pulls_latest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            origin, seed = self._origin(root)
            settings = settings_for(root / "orch")
            wm = LocalWorkspace(settings, origin)

            managed = wm.sync_project("acme/game", None)
            self.assertEqual(managed, (settings.workspace_root / "acme" / "game").resolve())
            self.assertEqual((managed / "README.md").read_text(), "v1")

            (seed / "README.md").write_text("v2")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "v2"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(seed), "push"], check=True, stdout=subprocess.DEVNULL)

            managed_again = wm.sync_project("acme/game", "main")
            self.assertEqual(managed_again, managed)
            self.assertEqual((managed / "README.md").read_text(), "v2")
            self.assertFalse(wm.inspect_repo(managed)["dirty"])

    def test_task_worktree_uses_managed_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            origin, _ = self._origin(root)
            settings = settings_for(root / "orch")
            wm = LocalWorkspace(settings, origin)
            managed = wm.sync_project("o/r", "main")
            wt, branch = wm.prepare_task("o/r", "main", 11, "LOCAL")
            self.assertNotEqual(wt.resolve(), managed.resolve())
            self.assertTrue((wt / ".git").exists())
            self.assertTrue(branch.startswith("task/issue-11-"))

    def test_remote_task_branch_resumes_after_managed_cache_loss(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            origin, _ = self._origin(root)
            settings = settings_for(root / "local")
            wm = LocalWorkspace(settings, origin)
            wt, branch = wm.prepare_task("o/r", "main", 7, "T")
            subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(wt), "config", "user.name", "Test"], check=True)
            (wt / "work.txt").write_text("task")
            sha = wm.commit_push(wt, branch, "task")

            subprocess.run(["git", "-C", str(wm.repo_dir("o/r")), "worktree", "remove", "--force", str(wt)], check=True)
            import shutil
            shutil.rmtree(wm.repo_dir("o/r"))
            shutil.rmtree(settings.runtime_dir / "worktrees", ignore_errors=True)

            wm2 = LocalWorkspace(settings, origin)
            wt2, branch2 = wm2.prepare_task("o/r", "main", 7, "T")
            resumed = subprocess.check_output(["git", "-C", str(wt2), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(branch2, branch)
            self.assertEqual(resumed, sha)


if __name__ == "__main__":
    unittest.main()
