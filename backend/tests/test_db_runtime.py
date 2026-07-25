import unittest

from db_runtime import PostgresConnection


class _Transaction:
    def __init__(self):
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self):
        self.started = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _Connection:
    def __init__(self):
        self.transactions = []

    def transaction(self):
        tx = _Transaction()
        self.transactions.append(tx)
        return tx

    async def execute(self, *_args):
        return "UPDATE 1"

    async def fetch(self, *_args):
        return []


class PostgresConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_commits_open_independent_transactions(self):
        raw = _Connection()
        connection = PostgresConnection(raw)

        await connection.execute("INSERT INTO migrations VALUES (?)", ("one",))
        await connection.commit()
        await connection.execute("CREATE INDEX idx_example ON films (id)")
        await connection.commit()

        self.assertEqual(len(raw.transactions), 2)
        self.assertTrue(all(tx.started and tx.committed for tx in raw.transactions))
        self.assertFalse(connection.has_transaction)
