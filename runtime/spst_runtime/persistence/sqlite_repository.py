import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator

from spst_runtime.persistence.repository import PersistenceRepository


LEGACY_DEFAULT_PROVENANCE_KEY = "spst-local-provenance-v1"
PROVENANCE_KEY_ENV = "SPST_PROVENANCE_KEY"
PROVENANCE_KEY_FILE_ENV = "SPST_PROVENANCE_KEY_FILE"


class SQLiteRepository(PersistenceRepository):
    """SQLite key/value repository with WAL mode enabled for state recovery."""

    _registry_lock = Lock()
    _path_locks: dict[str, RLock] = {}
    _initialized_paths: set[str] = set()

    def __init__(self, path: str = "spst.db", *, use_legacy_default_key: bool = False):
        self.path = path
        self._provenance_secret, self._provenance_key_source = self._load_provenance_secret(
            use_legacy_default_key=use_legacy_default_key
        )

    def connect(self) -> sqlite3.Connection:
        self._initialize()
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection and always close its OS handle afterwards."""
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def save(self, key: str, value: dict[str, Any]) -> None:
        serialized = json.dumps(value, sort_keys=True)
        for attempt in range(6):
            try:
                with self.locked(), self.connection() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO state_store(key, value)
                        VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (key, serialized),
                    )
                    self._append_provenance(conn, key, serialized)
                return
            except sqlite3.OperationalError as exc:
                if not self._is_locked(exc) or attempt == 5:
                    raise
                time.sleep(0.02 * (2**attempt))

    async def load(self, key: str) -> dict[str, Any] | None:
        for attempt in range(6):
            try:
                with self.connection() as conn:
                    row = conn.execute(
                        "SELECT value FROM state_store WHERE key = ?", (key,)
                    ).fetchone()
                break
            except sqlite3.OperationalError as exc:
                if not self._is_locked(exc) or attempt == 5:
                    raise
                time.sleep(0.02 * (2**attempt))
        if row is None:
            return None
        return json.loads(row[0])

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize in-process write sequences for one database path."""
        lock = self._lock_for_path()
        with lock:
            yield

    def _initialize(self) -> None:
        path_key = self._path_key()
        with self._lock_for_path():
            if path_key in self._initialized_paths:
                return
            if self.path != ":memory:":
                Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=30000;")
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state_store (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state_provenance (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        record_key TEXT NOT NULL,
                        record_hash TEXT NOT NULL,
                        previous_hash TEXT NOT NULL,
                        chain_hash TEXT NOT NULL,
                        signature TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
                self._initialized_paths.add(path_key)
            finally:
                conn.close()

    def _lock_for_path(self) -> RLock:
        path_key = self._path_key()
        with self._registry_lock:
            return self._path_locks.setdefault(path_key, RLock())

    def _path_key(self) -> str:
        if self.path == ":memory:":
            return self.path
        return str(Path(self.path).expanduser().resolve())

    @staticmethod
    def _is_locked(error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return "locked" in message or "busy" in message

    def verify_provenance(self) -> dict[str, Any]:
        """Verify the HMAC-protected append-only hash chain and latest state hashes."""
        with self.connection() as conn:
            entries = conn.execute(
                """
                SELECT sequence, record_key, record_hash, previous_hash, chain_hash, signature
                FROM state_provenance ORDER BY sequence ASC
                """
            ).fetchall()
            previous_hash = ""
            latest_hashes: dict[str, str] = {}
            for _, record_key, record_hash, stored_previous, chain_hash, signature in entries:
                expected_chain = self._chain_hash(previous_hash, record_key, record_hash)
                expected_signature = self._sign(expected_chain)
                if (
                    stored_previous != previous_hash
                    or chain_hash != expected_chain
                    or not hmac.compare_digest(signature, expected_signature)
                ):
                    return {
                        "valid": False,
                        "entries": len(entries),
                        "reason": "chain_verification_failed",
                        "key_source": self._provenance_key_source,
                    }
                previous_hash = chain_hash
                latest_hashes[record_key] = record_hash
            for record_key, expected_hash in latest_hashes.items():
                row = conn.execute(
                    "SELECT value FROM state_store WHERE key = ?",
                    (record_key,),
                ).fetchone()
                if row is None or self._record_hash(row[0]) != expected_hash:
                    return {
                        "valid": False,
                        "entries": len(entries),
                        "reason": "state_hash_mismatch",
                        "key_source": self._provenance_key_source,
                    }
        return {
            "valid": True,
            "entries": len(entries),
            "latest_hash": previous_hash,
            "key_source": self._provenance_key_source,
        }

    def rotate_provenance_key(
        self,
        new_secret: str | None = None,
        *,
        key_file: str | None = None,
    ) -> dict[str, Any]:
        """Re-sign current state records with fresh local HMAC key material."""
        current = self.verify_provenance()
        if not current.get("valid"):
            raise ValueError(f"Cannot rotate invalid provenance chain: {current.get('reason')}")

        if new_secret is None:
            target_key_file = Path(key_file) if key_file else self._default_key_file()
            self._write_key_file(target_key_file, secrets.token_hex(32))
            self._provenance_secret = self._read_key_file(target_key_file)
            self._provenance_key_source = "local_key_file"
        else:
            self._provenance_secret = new_secret.encode("utf-8")
            self._provenance_key_source = "runtime_override"
            if key_file:
                target_key_file = Path(key_file)
                self._write_key_file(target_key_file, new_secret)
                self._provenance_key_source = "local_key_file"

        with self.locked(), self.connection() as conn:
            rows = conn.execute("SELECT key, value FROM state_store ORDER BY key ASC").fetchall()
            conn.execute("DELETE FROM state_provenance")
            for key, serialized in rows:
                self._append_provenance(conn, key, serialized)
        return self.verify_provenance()

    def _append_provenance(self, conn: sqlite3.Connection, key: str, serialized: str) -> None:
        row = conn.execute(
            "SELECT chain_hash FROM state_provenance ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = row[0] if row else ""
        record_hash = self._record_hash(serialized)
        chain_hash = self._chain_hash(previous_hash, key, record_hash)
        conn.execute(
            """
            INSERT INTO state_provenance(
                record_key,
                record_hash,
                previous_hash,
                chain_hash,
                signature
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, record_hash, previous_hash, chain_hash, self._sign(chain_hash)),
        )

    @staticmethod
    def _record_hash(serialized: str) -> str:
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _chain_hash(previous_hash: str, key: str, record_hash: str) -> str:
        return hashlib.sha256(f"{previous_hash}:{key}:{record_hash}".encode("utf-8")).hexdigest()

    def _sign(self, chain_hash: str) -> str:
        return hmac.new(
            self._provenance_secret,
            chain_hash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _load_provenance_secret(self, *, use_legacy_default_key: bool) -> tuple[bytes, str]:
        environment_secret = os.getenv(PROVENANCE_KEY_ENV)
        if environment_secret:
            return environment_secret.encode("utf-8"), "environment"

        configured_key_file = os.getenv(PROVENANCE_KEY_FILE_ENV)
        if configured_key_file:
            path = Path(configured_key_file)
            if not path.exists():
                self._write_key_file(path, secrets.token_hex(32))
            return self._read_key_file(path), "key_file"

        if use_legacy_default_key:
            return LEGACY_DEFAULT_PROVENANCE_KEY.encode("utf-8"), "legacy_default"

        if self.path == ":memory:":
            return secrets.token_hex(32).encode("utf-8"), "ephemeral"

        local_key_file = self._default_key_file()
        with self._lock_for_path():
            if local_key_file.exists():
                return self._read_key_file(local_key_file), "local_key_file"

            if self._database_has_provenance():
                return LEGACY_DEFAULT_PROVENANCE_KEY.encode("utf-8"), "legacy_default"

            self._write_key_file(local_key_file, secrets.token_hex(32))
            return self._read_key_file(local_key_file), "local_key_file"

    def _default_key_file(self) -> Path:
        return Path(f"{self.path}.provenance_key").expanduser().resolve()

    def _database_has_provenance(self) -> bool:
        path = Path(self.path).expanduser()
        if not path.exists():
            return False
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        try:
            table = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'state_provenance'
                """
            ).fetchone()
            if table is None:
                return False
            count = conn.execute("SELECT COUNT(*) FROM state_provenance").fetchone()[0]
            return int(count) > 0
        except sqlite3.DatabaseError:
            return False
        finally:
            conn.close()

    @staticmethod
    def _read_key_file(path: Path) -> bytes:
        secret = path.read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError(f"Empty provenance key file: {path}")
        return secret.encode("utf-8")

    @staticmethod
    def _write_key_file(path: Path, secret: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
        temp_path.write_text(f"{secret}\n", encoding="utf-8")
        temp_path.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
