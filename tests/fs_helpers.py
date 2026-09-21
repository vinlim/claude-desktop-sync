"""Builds throwaway partitions on disk that look like the app's."""
import json
import os
import tempfile
from pathlib import Path

ACCOUNT_A = "aaaaaaaa-0000-4000-8000-000000000001"
ORG_A = "aaaaaaaa-0000-4000-8000-0000000000a1"
ACCOUNT_B = "bbbbbbbb-0000-4000-8000-000000000002"
ORG_B = "bbbbbbbb-0000-4000-8000-0000000000b2"
X = "11111111-1111-4111-8111-111111111111"
Y = "22222222-2222-4222-8222-222222222222"
SECOND_NS = 1_000_000_000
LONG_AGO_S = 1_000_000  # well outside the racy-timestamp window


class Sandbox:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "Claude"
        self.state_dir = self.base / "state"
        self.a = self.partition(ACCOUNT_A, ORG_A)
        self.b = self.partition(ACCOUNT_B, ORG_B)

    def cleanup(self):
        self._tmp.cleanup()

    def partition(self, account, org, root=None):
        path = (root or self.root) / "claude-code-sessions" / account / org
        path.mkdir(parents=True, mode=0o700)
        return path

    def logged_in_as(self, account, root=None, at_s=None):
        """at_s is when the app last wrote its config: long ago unless a test says otherwise."""
        config = (root or self.root) / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"lastKnownAccountUuid": account}))
        written = (LONG_AGO_S if at_s is None else at_s) * SECOND_NS
        os.utime(config, ns=(written, written))


def write_record(directory, session_id, at_s=LONG_AGO_S, activity=100, **fields):
    body = {"sessionId": "local_" + session_id, "title": "t", "lastActivityAt": activity}
    body.update(fields)
    path = directory / ("local_%s.json" % session_id)
    path.write_text(json.dumps(body))
    os.chmod(path, 0o600)
    os.utime(path, ns=(at_s * SECOND_NS, at_s * SECOND_NS))
    return path


def write_tombstone(directory, session_id, deleted_at_ms, at_s=LONG_AGO_S, content=None):
    path = directory / ("deleted_%s" % session_id)
    path.write_text(str(deleted_at_ms) if content is None else content)
    os.utime(path, ns=(at_s * SECOND_NS, at_s * SECOND_NS))
    return path


def title_of(directory, session_id):
    return json.loads((directory / ("local_%s.json" % session_id)).read_text())["title"]
