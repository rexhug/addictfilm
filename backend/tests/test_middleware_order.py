import unittest

import main


class MiddlewareOrderTests(unittest.TestCase):
    def test_observability_wraps_request_database_scope(self):
        stack = main.app.build_middleware_stack()
        names = []
        current = stack
        while current is not None:
            dispatch = getattr(current, "dispatch_func", None)
            names.append(getattr(dispatch, "__name__", ""))
            current = getattr(current, "app", None)

        self.assertLess(
            names.index("log_slow_requests"),
            names.index("database_request_scope"),
        )

