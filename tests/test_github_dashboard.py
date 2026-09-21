import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.dashboard import make_server, verify_dashboard
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


class FakeState:
    def __init__(self): self.verified=None; self.events=[]
    def mark_dashboard_verified(self,address): self.verified=address
    def add_event(self,event_type,payload): self.events.append((event_type,payload))

class FakeEngine:
    def __init__(self): self.paused=False; self.state=FakeState(); self.added_project=None
    def snapshot(self): return {"paused":self.paused,"github_auth":{"connected":True,"login":"owner","provider":"gh-cli"},"system":{"managed_root":"E:/"},"active":None,"projects":[],"issues":[],"events":[]}
    def add_managed_project(self, source):
        self.added_project={"repo":"acme/demo","id":"demo","issues_repo":"acme/demo","default_branch":"main","managed_path":"E:/acme/demo"}
        return self.added_project
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

    def test_dashboard_smoke_marks_bootstrap_verified(self):
        engine = FakeEngine()
        server = make_server(engine,"127.0.0.1",0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            port=server.server_address[1]
            address=verify_dashboard(engine,"127.0.0.1",port)
            self.assertEqual(engine.state.verified,address)
            self.assertEqual(engine.state.events[-1][0],"dashboard_verified")
        finally:
            server.shutdown(); server.server_close()

    def test_dashboard_can_add_managed_project(self):
        engine = FakeEngine()
        server = make_server(engine,"127.0.0.1",0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            port=server.server_address[1]
            payload=json.dumps({"repository":"https://github.com/acme/demo.git"}).encode()
            req=urllib.request.Request(
                f"http://127.0.0.1:{port}/api/projects/add",
                data=payload,
                method="POST",
                headers={"X-Orchestrator-UI":"1","Content-Type":"application/json"},
            )
            with urllib.request.urlopen(req) as response:
                data=json.loads(response.read())
            self.assertTrue(data["ok"])
            self.assertEqual(engine.added_project["repo"],"acme/demo")
            self.assertEqual(engine.added_project["managed_path"],"E:/acme/demo")
        finally:
            server.shutdown(); server.server_close()

    def test_dashboard_health_and_pause(self):
        engine = FakeEngine()
        server = make_server(engine,"127.0.0.1",0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            port=server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as r:
                self.assertTrue(json.loads(r.read())["ok"])
            bad=urllib.request.Request(f"http://127.0.0.1:{port}/api/control/pause",data=b"",method="POST")
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(bad)
            self.assertEqual(denied.exception.code, 403)
            req=urllib.request.Request(
                f"http://127.0.0.1:{port}/api/control/pause",
                data=b"",
                method="POST",
                headers={"X-Orchestrator-UI":"1"},
            )
            with urllib.request.urlopen(req) as r:
                self.assertTrue(json.loads(r.read())["ok"])
            self.assertTrue(engine.paused)
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
