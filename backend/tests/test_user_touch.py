"""The profile write must not ride along on every API request.

upsert_user is already a no-op when nothing changed, but under PostgreSQL the
statement still costs BEGIN + statement + COMMIT — three round-trips to Neon on
every GET, and a Mini App screen fires several. The question "did anything
change recently" belongs in the process, not in the database.
"""
import unittest

import user_touch


class UserTouchTests(unittest.TestCase):
    def setUp(self):
        user_touch._reset_for_tests()

    def tearDown(self):
        user_touch._reset_for_tests()

    def _user(self, **overrides):
        base = {"id": 1, "first_name": "Denys", "username": "denys", "photo_url": None}
        return {**base, **overrides}

    def test_the_first_request_writes(self):
        self.assertTrue(user_touch.should_persist(self._user(), now=0.0))

    def test_a_repeat_within_the_ttl_does_not(self):
        user_touch.should_persist(self._user(), now=0.0)
        self.assertFalse(user_touch.should_persist(self._user(), now=1.0))
        self.assertFalse(user_touch.should_persist(self._user(), now=user_touch.TTL_SECONDS - 0.1))

    def test_the_gate_reopens_after_the_ttl(self):
        user_touch.should_persist(self._user(), now=0.0)
        self.assertTrue(user_touch.should_persist(self._user(), now=user_touch.TTL_SECONDS + 1))

    def test_a_renamed_user_writes_through_immediately(self):
        """Waiting out the TTL would show a stale name to their partner."""
        user_touch.should_persist(self._user(), now=0.0)
        self.assertTrue(user_touch.should_persist(self._user(first_name="Денис"), now=1.0))

    def test_a_new_avatar_writes_through_immediately(self):
        user_touch.should_persist(self._user(), now=0.0)
        self.assertTrue(user_touch.should_persist(
            self._user(photo_url="https://t.me/i/userpic/a.jpg"), now=1.0))

    def test_a_changed_username_writes_through_immediately(self):
        user_touch.should_persist(self._user(), now=0.0)
        self.assertTrue(user_touch.should_persist(self._user(username="new_handle"), now=1.0))

    def test_users_do_not_share_a_gate(self):
        user_touch.should_persist(self._user(id=1), now=0.0)
        self.assertTrue(user_touch.should_persist(self._user(id=2), now=1.0))

    def test_the_table_cannot_grow_without_bound(self):
        """Public app: an unbounded dict keyed by user input is a slow leak."""
        original = user_touch._MAX_ENTRIES
        try:
            user_touch._MAX_ENTRIES = 10
            for index in range(50):
                user_touch.should_persist(self._user(id=index), now=float(index))
            self.assertLessEqual(len(user_touch._seen), user_touch._MAX_ENTRIES)
        finally:
            user_touch._MAX_ENTRIES = original


if __name__ == "__main__":
    unittest.main()
