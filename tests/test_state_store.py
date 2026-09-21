import json
import os
import stat
import unittest

from session_sync.model import SyncState
from session_sync.state_store import StateUnusable, StoredState, encode_state, load_state, save_state
from tests.fs_helpers import Sandbox, X, Y


class StateStore(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.path = self.box.state_dir / "state.json"

    def test_no_file_yet_is_an_empty_state(self):
        loaded = load_state(self.path)

        self.assertEqual(loaded.sync, SyncState())
        self.assertEqual((loaded.cache, loaded.logins, loaded.reported, loaded.last_success_ms), ({}, {}, "", 0))

    def test_everything_survives_a_round_trip(self):
        stored = StoredState(
            sync=SyncState(agreed={X: "h1"}, seen={"/p/a": {X, Y}}, placed={"/p/b": {Y: "h2"}}),
            cache={"/p/a": {X: (12345678901234, 42, "h1", 99), Y: (5, 6, None, 0)}},
            logins={"/data/Claude": ("aaaaaaaa-0000-4000-8000-000000000001", 777)},
            reported="digest", last_success_ms=1234)

        save_state(self.path, stored)

        self.assertEqual(load_state(self.path), stored)

    def test_an_encoded_snapshot_shares_nothing_with_the_live_state(self):
        # A run compares the state with a snapshot taken at its start to decide whether to save.
        stored = StoredState(sync=SyncState(agreed={X: "h1"}, seen={"/p/a": {X}}, placed={"/p/b": {X: "h1"}}),
                             cache={"/p/a": {X: (1, 2, "h1", 3)}}, logins={"/root": ("acct", 5)})
        snapshot = encode_state(stored)
        before = json.dumps(snapshot, sort_keys=True)

        stored.sync.agreed[Y] = "h2"
        stored.sync.seen["/p/a"].add(Y)
        stored.sync.placed["/p/b"][Y] = "h2"
        stored.cache["/p/a"][Y] = (4, 5, "h2", 6)
        stored.logins["/root"] = ("other", 9)

        self.assertEqual(json.dumps(snapshot, sort_keys=True), before)

    def test_the_file_is_private_and_written_whole(self):
        save_state(self.path, StoredState(sync=SyncState(agreed={X: "h1"})))

        self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.path.parent).st_mode), 0o700)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_a_failed_write_leaves_the_previous_state_in_place(self):
        save_state(self.path, StoredState(sync=SyncState(agreed={X: "old"})))
        unserialisable = StoredState(sync=SyncState(agreed={X: object()}))

        with self.assertRaises(TypeError):
            save_state(self.path, unserialisable)

        self.assertEqual(load_state(self.path).sync.agreed, {X: "old"})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_a_file_that_cannot_be_trusted_stops_the_run_and_says_how_to_recover(self):
        self.path.parent.mkdir(parents=True)
        complete = {"version": 1, "agreed": {}, "seen": {}, "placed": {}, "cache": {}, "logins": {},
                    "reported": "", "last_success_ms": 0}
        cases = {"torn": "{", "not an object": "[]", "from a newer version": json.dumps(dict(complete, version=999)),
                 "wrong shape": json.dumps(dict(complete, agreed=[]))}
        self.path.write_text(json.dumps(complete))
        load_state(self.path)  # the control: the complete shape itself is accepted
        for name, content in cases.items():
            with self.subTest(case=name):
                self.path.write_text(content)

                with self.assertRaises(StateUnusable) as raised:
                    load_state(self.path)

                self.assertIn(str(self.path), str(raised.exception))
                self.assertIn("--reset-state", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
