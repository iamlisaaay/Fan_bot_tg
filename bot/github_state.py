"""Keeps the state files in the GitHub repository, since Render's free disk is wiped
on every restart. Uses the GitHub contents API, so no git is needed on the server.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)
API = "https://api.github.com"
# [skip render] stops these commits from redeploying (and so restarting) the bot.
COMMIT_MESSAGE = "Update state [skip ci] [skip render]"


class GitHubState:
    def __init__(self, repo: str, token: str, folder: Path, branch: str = "main"):
        self.url = f"{API}/repos/{repo}/contents/state"
        self.branch = branch
        self.folder = folder
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        self.uploaded: dict[str, tuple[str, str]] = {}  # file name -> (content, sha)

    def _fetch(self, name: str) -> tuple[str, str] | None:
        response = requests.get(
            f"{self.url}/{name}", headers=self.headers, params={"ref": self.branch}, timeout=30
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    def download(self, names: list[str]) -> None:
        """Brings the saved state onto the local disk before the bot starts."""
        self.folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            found = self._fetch(name)
            if found:
                (self.folder / name).write_text(found[0], encoding="utf-8")
                self.uploaded[name] = found
                log.info("State %s loaded from GitHub", name)

    def upload(self, path: Path) -> None:
        """Commits the file to GitHub, but only when its content actually changed."""
        name = path.name
        content = path.read_text(encoding="utf-8")
        previous = self.uploaded.get(name)
        if previous and previous[0] == content:
            return
        for attempt in range(2):
            body = {
                "message": COMMIT_MESSAGE,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": self.branch,
            }
            if previous:
                body["sha"] = previous[1]
            response = requests.put(f"{self.url}/{name}", headers=self.headers, json=body, timeout=30)
            if response.status_code in (409, 422) and attempt == 0:
                # Someone else changed the file meanwhile. While the bot runs its own copy
                # is the newest, so take the current sha and write over it.
                previous = self._fetch(name)
                continue
            if response.status_code >= 400:
                # GitHub explains refusals (e.g. a token without write access) in "message".
                try:
                    reason = response.json().get("message", "")
                except ValueError:
                    reason = response.text[:200]
                raise RuntimeError(f"GitHub {response.status_code}: {reason}")
            self.uploaded[name] = (content, response.json()["content"]["sha"])
            return
