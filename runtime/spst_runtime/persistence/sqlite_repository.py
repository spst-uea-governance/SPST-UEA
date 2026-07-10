import json
import sqlite3
from typing import Any
from spst_runtime.persistence.repository import PersistenceRepository

class SQLiteRepository(PersistenceRepository):
    """SQLite key/value repository with WAL mode enabled for state recovery."""

    def __init__(self, path: str = "spst.db"):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        return conn

    async def save(self, key: str, value: dict[str, Any]) -> None:
        serialized = json.dumps(value, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO state_store(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, serialized),
            )

    async def load(self, key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state_store WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
