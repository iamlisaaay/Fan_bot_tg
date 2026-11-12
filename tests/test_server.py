import base64
import json
import threading
import urllib.request

import pytest

from bot import github_state, server
from bot.github_state import GitHubState
from bot.state import State


class FakeGitHub:
    """Stands in for the GitHub contents API."""

    def __init__(self):
        self.files = {}  # name -> (content, sha)
        self.commits = []
        self.next_sha = 0

    def _sha(self):
        self.next_sha += 1
        return f"sha{self.next_sha}"

    def get(self, url, headers=None, params=None, timeout=None):
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            return Response(404, {})
        content, sha = self.files[name]
        return Response(200, {"content": base64.b64encode(content.encode()).decode(), "sha": sha})

    def put(self, url, headers=None, json=None, timeout=None):
        name = url.rsplit("/", 1)[1]
        current = self.files.get(name)
        if current and json.get("sha") != current[1]:
            return Response(409, {})
        content = base64.b64decode(json["content"]).decode()
        sha = self._sha()
        self.files[name] = (content, sha)
        self.commits.append((name, json["message"]))
        return Response(200, {"content": {"sha": sha}})


class Response:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture
def hub(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github_state.requests, "get", fake.get)
    monkeypatch.setattr(github_state.requests, "put", fake.put)
    return fake


def test_download_brings_saved_state_and_skips_missing(hub, tmp_path):
    hub.files["x.json"] = ('{"seen": {"x:a": ["1"]}}', "s0")
    GitHubState("o/r", "t", tmp_path).download(["x.json", "youtube.json"])
    assert State(tmp_path / "x.json").seen("x:a") == ["1"]
    assert not (tmp_path / "youtube.json").exists()


def test_saves_commit_only_real_changes_and_skip_redeploys(hub, tmp_path, monkeypatch):
    hub.files["x.json"] = ('{"seen": {}, "meta": {}, "blocked_until": 0.0}', "s0")
    store = GitHubState("o/r", "t", tmp_path)
    store.download(["x.json"])
    monkeypatch.setattr(State, "on_save", store.upload)

    state = State(tmp_path / "x.json")
    state.save()  # content unchanged apart from formatting
    state.mark_seen("x:a", ["1"])
    state.save()
    state.save()  # nothing new
    assert len(hub.commits) <= 2
    assert all("[skip render]" in message for _, message in hub.commits)
    assert '"1"' in hub.files["x.json"][0]


def test_upload_overwrites_after_someone_else_changed_the_file(hub, tmp_path):
    hub.files["x.json"] = ("old", "s0")
    store = GitHubState("o/r", "t", tmp_path)
    store.download(["x.json"])
    hub.files["x.json"] = ("changed elsewhere", "s-other")
    (tmp_path / "x.json").write_text("mine", encoding="utf-8")
    store.upload(tmp_path / "x.json")
    assert hub.files["x.json"][0] == "mine"


def test_first_upload_creates_the_file(hub, tmp_path):
    store = GitHubState("o/r", "t", tmp_path)
    (tmp_path / "youtube.json").write_text("{}", encoding="utf-8")
    store.upload(tmp_path / "youtube.json")
    assert hub.files["youtube.json"][0] == "{}"


def test_ping_page_answers_ok():
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Ping)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/") as reply:
            assert reply.status == 200 and reply.read() == b"ok"
    finally:
        httpd.shutdown()
