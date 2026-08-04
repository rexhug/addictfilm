import unittest

from db_runtime import PostgresConnection, _postgres_sql


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


class PostgresPlaceholderTests(unittest.TestCase):
    """`?` rewriting must understand SQL lexis, not just count characters.

    A question mark inside a literal or a comment used to consume a parameter
    slot and shift every following one.  Nothing raised — asyncpg simply bound
    the wrong value to the wrong column.
    """

    def test_converts_plain_placeholders_in_order(self):
        self.assertEqual(_postgres_sql("SELECT ?, ? FROM t WHERE b = ?"),
                         "SELECT $1, $2 FROM t WHERE b = $3")

    def test_ignores_question_mark_inside_string_literal(self):
        # The LIKE pattern used to consume $1 and shift the real parameter.
        self.assertEqual(_postgres_sql("SELECT * FROM t WHERE title LIKE '%?%' AND id = ?"),
                         "SELECT * FROM t WHERE title LIKE '%?%' AND id = $1")

    def test_ignores_escaped_quote_inside_literal(self):
        self.assertEqual(_postgres_sql("SELECT 'it''s ok?', ?"), "SELECT 'it''s ok?', $1")

    def test_ignores_quoted_identifier(self):
        self.assertEqual(_postgres_sql('SELECT "col?name" FROM t WHERE x = ?'),
                         'SELECT "col?name" FROM t WHERE x = $1')

    def test_ignores_line_comment(self):
        self.assertEqual(_postgres_sql("SELECT 1 -- really?\nWHERE a = ?"),
                         "SELECT 1 -- really?\nWHERE a = $1")

    def test_ignores_nested_block_comment(self):
        self.assertEqual(_postgres_sql("SELECT /* a? /* b? */ */ ?"),
                         "SELECT /* a? /* b? */ */ $1")

    def test_ignores_dollar_quoted_body(self):
        self.assertEqual(_postgres_sql("SELECT $tag$a?b$tag$, ?"), "SELECT $tag$a?b$tag$, $1")

    def test_rejects_ambiguous_jsonb_operator(self):
        with self.assertRaises(ValueError):
            _postgres_sql("SELECT * FROM t WHERE data ?| array['a']")

    def test_leaves_sql_without_placeholders_untouched(self):
        self.assertEqual(_postgres_sql("SELECT 1"), "SELECT 1")
