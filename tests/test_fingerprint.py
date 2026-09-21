import json
import unittest

from session_sync.fingerprint import fingerprint

SID = "11111111-1111-4111-8111-111111111111"


def record(**fields):
    body = {"sessionId": "local_" + SID, "title": "t", "lastActivityAt": 100}
    body.update(fields)
    return json.dumps(body).encode()


class Fingerprint(unittest.TestCase):
    def test_what_the_app_rewrites_with_no_user_action_is_not_state(self):
        # F6: making a session visible rewrites lastFocusedAt. At load each login stamps
        # its own errorAt on a side session that never started, which would read as a tie.
        before = fingerprint(SID, record(error="never started", errorAt=111))
        after = fingerprint(SID, record(error="never started", errorAt=222, lastFocusedAt=999))

        self.assertEqual(before.state_hash, after.state_hash)

    def test_a_string_cut_through_an_emoji_is_still_a_readable_record(self):
        # The app truncates by UTF-16 code unit, so a record can hold half a surrogate pair.
        cut = b'{"sessionId": "local_%s", "lastTurnReport": "done \\ud83d", "lastActivityAt": 7}' % SID.encode()
        whole = b'{"sessionId": "local_%s", "lastTurnReport": "done", "lastActivityAt": 7}' % SID.encode()

        self.assertTrue(fingerprint(SID, cut).readable)
        self.assertEqual(fingerprint(SID, cut).last_activity_at, 7)
        self.assertNotEqual(fingerprint(SID, cut).state_hash, fingerprint(SID, whole).state_hash)

    def test_key_order_and_spacing_do_not_change_the_state(self):
        one = fingerprint(SID, b'{"sessionId":"local_%s","title":"t"}' % SID.encode())
        two = fingerprint(SID, b'{ "title": "t",\n "sessionId": "local_%s" }' % SID.encode())

        self.assertEqual(one.state_hash, two.state_hash)

    def test_any_other_field_changes_the_state(self):
        for change in ({"title": "renamed"}, {"isArchived": True}, {"cliSessionId": "c2"}, {"lastActivityAt": 101}):
            with self.subTest(change=change):
                self.assertNotEqual(fingerprint(SID, record()).state_hash, fingerprint(SID, record(**change)).state_hash)

    def test_a_volatile_key_nested_deeper_is_still_state(self):
        plain = fingerprint(SID, record(spawnSeed={}))
        nested = fingerprint(SID, record(spawnSeed={"lastFocusedAt": 1}))

        self.assertNotEqual(plain.state_hash, nested.state_hash)

    def test_activity_is_read_from_the_record(self):
        self.assertEqual(fingerprint(SID, record(lastActivityAt=1234)).last_activity_at, 1234)

    def test_a_time_written_as_a_float_is_floored(self):
        # The app falls back to a file's mtimeMs for an imported session, and that is fractional.
        self.assertEqual(fingerprint(SID, record(lastActivityAt=1234.0)).last_activity_at, 1234)
        self.assertEqual(fingerprint(SID, record(lastActivityAt=1234.9)).last_activity_at, 1234)

    def test_missing_or_odd_activity_reads_as_zero(self):
        for value in (None, "soon", True, -3, -0.5, float("inf"), float("nan")):
            with self.subTest(value=value):
                self.assertEqual(fingerprint(SID, record(lastActivityAt=value)).last_activity_at, 0)

    def test_what_the_app_would_not_accept_is_unreadable(self):
        cases = {
            "torn write": b'{"sessionId": "local_',
            "empty file": b"",
            "not an object": b"[1, 2]",
            "another session": record(sessionId="local_22222222-2222-4222-8222-222222222222"),
            "no session id": b'{"title": "t"}',
            "not utf-8": b"\xff\xfe{}",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assertFalse(fingerprint(SID, data).readable)


if __name__ == "__main__":
    unittest.main()
