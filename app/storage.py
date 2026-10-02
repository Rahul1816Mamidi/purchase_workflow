import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        Open a SQLite connection, commit successful operations,
        rollback failed operations, and always close the connection.

        Explicitly closing the connection is important on Windows because
        an open SQLite connection can prevent a temporary database file
        from being deleted.
        """
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row

        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    content_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    total REAL,
                    deterministic_findings_json TEXT NOT NULL,
                    vendor_result_json TEXT,
                    budget_result_json TEXT,
                    model_result_json TEXT,
                    human_review_json TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

        if row is None:
            return None

        result = dict(row)

        result["content_json"] = json.loads(result["content_json"])
        result["deterministic_findings"] = json.loads(
            result["deterministic_findings_json"]
        )

        if result["vendor_result_json"] is not None:
            result["vendor_result"] = json.loads(
                result["vendor_result_json"]
            )
        else:
            result["vendor_result"] = None

        if result["budget_result_json"] is not None:
            result["budget_result"] = json.loads(
                result["budget_result_json"]
            )
        else:
            result["budget_result"] = None

        if result["model_result_json"] is not None:
            result["model_result"] = json.loads(
                result["model_result_json"]
            )
        else:
            result["model_result"] = None

        if result["human_review_json"] is not None:
            result["human_review"] = json.loads(
                result["human_review_json"]
            )
        else:
            result["human_review"] = None

        return result

    def create_request(
        self,
        request_data: dict[str, Any],
        state: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO requests (
                    request_id,
                    content_json,
                    state,
                    total,
                    deterministic_findings_json
                )
                VALUES (?, ?, ?, NULL, ?)
                """,
                (
                    request_data["request_id"],
                    json.dumps(request_data),
                    state,
                    json.dumps([]),
                ),
            )

    def update_request(
        self,
        request_id: str,
        **fields: Any,
    ) -> None:
        allowed = {
            "state",
            "total",
            "deterministic_findings_json",
            "vendor_result_json",
            "budget_result_json",
            "model_result_json",
            "human_review_json",
        }

        assignments = []
        values = []

        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(
                    f"Unsupported storage field: {key}"
                )

            assignments.append(f"{key} = ?")

            if key.endswith("_json"):
                values.append(json.dumps(value))
            else:
                values.append(value)

        if not assignments:
            return

        values.append(request_id)

        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE requests
                SET {', '.join(assignments)}
                WHERE request_id = ?
                """,
                values,
            )

    def add_event(
        self,
        request_id: str,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    request_id,
                    event_type,
                    details_json
                )
                VALUES (?, ?, ?)
                """,
                (
                    request_id,
                    event_type,
                    json.dumps(details or {}),
                ),
            )

    def events(
        self,
        request_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    event_type,
                    details_json,
                    created_at
                FROM events
                WHERE request_id = ?
                ORDER BY id
                """,
                (request_id,),
            ).fetchall()

        return [
            {
                "event_type": row["event_type"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def final_record_count(
        self,
        request_id: str,
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM requests
                WHERE request_id = ?
                  AND state IN ('APPROVED', 'REJECTED')
                """,
                (request_id,),
            ).fetchone()

        return int(row["count"])