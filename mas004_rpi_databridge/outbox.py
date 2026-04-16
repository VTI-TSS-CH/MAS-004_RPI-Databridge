import json
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional
from mas004_rpi_databridge.db import DB, now_ts

@dataclass
class OutboxJob:
    id: int
    created_ts: float
    method: str
    url: str
    headers_json: str
    body_json: Optional[str]
    idempotency_key: str
    priority: int
    dedupe_key: Optional[str]
    retry_count: int
    next_attempt_ts: float

class Outbox:
    def __init__(self, db: DB):
        self.db = db

    @staticmethod
    def _normalize_prefixes(prefixes: Optional[Iterable[str]]) -> list[str]:
        items = []
        for raw in prefixes or []:
            base = (raw or "").strip().rstrip("/")
            if base and base not in items:
                items.append(base)
        return items

    def enqueue(
        self,
        method: str,
        url: str,
        headers: dict,
        body: Optional[dict],
        idempotency_key: Optional[str] = None,
        priority: int = 100,
        dedupe_key: Optional[str] = None,
        drop_if_duplicate: bool = False,
    ):
        if idempotency_key is None:
            idempotency_key = str(uuid.uuid4())

        headers = dict(headers or {})
        headers.setdefault("X-Idempotency-Key", idempotency_key)
        headers.setdefault("Content-Type", "application/json")
        body_json = json.dumps(body) if body is not None else None
        dedupe_key = (dedupe_key or "").strip() or None
        try:
            priority = int(priority)
        except Exception:
            priority = 100
        priority = max(0, min(1000, priority))

        with self.db._conn() as c:
            if dedupe_key and drop_if_duplicate:
                latest = c.execute(
                    """SELECT idempotency_key, body_json
                       FROM outbox
                       WHERE method=?
                         AND url=?
                         AND dedupe_key=?
                       ORDER BY created_ts DESC
                       LIMIT 1""",
                    (method.upper(), url, dedupe_key),
                ).fetchone()
                if latest and latest[0] and (latest[1] or None) == body_json:
                    return str(latest[0])
            c.execute(
                """INSERT INTO outbox(
                       created_ts,method,url,headers_json,body_json,idempotency_key,priority,dedupe_key
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (now_ts(), method.upper(), url, json.dumps(headers), body_json, idempotency_key, priority, dedupe_key)
            )
        return idempotency_key

    def next_due(
        self,
        url_prefixes: Optional[Iterable[str]] = None,
        exclude_url_prefixes: Optional[Iterable[str]] = None,
    ) -> Optional[OutboxJob]:
        where = ["next_attempt_ts <= ?"]
        params: list[object] = [now_ts()]

        include_prefixes = self._normalize_prefixes(url_prefixes)
        if include_prefixes:
            parts = []
            for prefix in include_prefixes:
                parts.append("(url = ? OR url LIKE ?)")
                params.extend([prefix, prefix + "/%"])
            where.append("(" + " OR ".join(parts) + ")")

        excluded_prefixes = self._normalize_prefixes(exclude_url_prefixes)
        if excluded_prefixes:
            parts = []
            for prefix in excluded_prefixes:
                parts.append("(url = ? OR url LIKE ?)")
                params.extend([prefix, prefix + "/%"])
            where.append("NOT (" + " OR ".join(parts) + ")")

        sql = f"""
            SELECT id,created_ts,method,url,headers_json,body_json,idempotency_key,priority,dedupe_key,retry_count,next_attempt_ts
            FROM outbox
            WHERE {" AND ".join(where)}
            ORDER BY next_attempt_ts ASC, priority ASC, retry_count ASC, created_ts ASC
            LIMIT 1
        """
        with self.db._conn() as c:
            row = c.execute(sql, params).fetchone()
        return OutboxJob(*row) if row else None

    def delete(self, job_id: int):
        with self.db._conn() as c:
            c.execute("DELETE FROM outbox WHERE id=?", (job_id,))

    def reschedule(self, job_id: int, retry_count: int, next_attempt_ts: float):
        with self.db._conn() as c:
            c.execute("UPDATE outbox SET retry_count=?, next_attempt_ts=? WHERE id=?",
                      (retry_count, next_attempt_ts, job_id))

    def count(self) -> int:
        with self.db._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def clear(self) -> int:
        with self.db._conn() as c:
            deleted = int(c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            c.execute("DELETE FROM outbox")
        return deleted
