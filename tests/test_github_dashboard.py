import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from orchestrator.dashboard import make_server
from orchestrator.github_client import GitHubClient


class FakePagedGitHub(GitHubClient):
    def __init__(self):
        pass
    def request(self, method, path, data=None):
        from urllib.parse import urlparse, parse_qs
        page = int(parse_qs(urlparse(path).query).get("page", [1])[0])
        if page == 1:
            return [{"id":i} for i in range(100)]
        if page == 2:
            return [{"id":100}]
        return []


class FakeEngine:
    def __init__(self): self.paused=False
    def snapshot(self): return {"paused":self.paused,"active":None,"projects":[],"issues":[],"events":[]}
    def pause(self): self.paused=True
    def resume(self): self.paused=False
    def tick(self): pass
    def sync_projects(self): return []
    def request_retry(self): pass
    def update_graphify(self, project): return {"project":project}


class GitHubDashboardTests(unittest.TestCase):
    def test_pagination_reads_past_100(self):
        client = FakePagedGitHub()
        self.assertEqual(len(client.paged("/x")), 101)

    def test_dashboard_health_and_pause(self):
        engine = FakeEngine()
        server = make_server(engine,"127.0.0.1",0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            port=server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as r:
                self.assertTrue(json.loads(r.read())["ok"])
            req=urllib.request.Request(f"http://127.0.0.1:{port}/api/control/pause",data=b"",method="POST")
            with urllib.request.urlopen(req) as r:
                self.assertTrue(json.loads(r.read())["ok"])
            self.assertTrue(engine.paused)
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
