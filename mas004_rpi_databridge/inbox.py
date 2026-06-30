import json
from dataclasses import dataclass
from typing import Optional
from mas004_rpi_databridge.db import DB, now_ts

@dataclass
class InboxMsg:
    id: int
    received_ts: float
    source: Optional[str]
    headers_json: str
    body_json: Optional[str]
    idempotency_key: str
    state: str

class Inbox:
    def __init__(self, db: DB):
        self.db = db

    def store(self, source: Optional[str], headers: dict, body: Optional[dict], idempotency_key: str) -> bool:
        headers = dict(headers or {})
        with self.db._conn() as c:
            try:
                c.execute(
                    "INSERT INTO inbox(received_ts,source,headers_json,body_json,idempotency_key,state) VALUES(?,?,?,?,?, 'pending')",
                    (now_ts(), source, json.dumps(headers), json.dumps(body) if body is not None else None, idempotency_key)
                )
                return True
            except Exception:
                # likely UNIQUE constraint -> duplicate idempotency key
                return False

    def next_pending(self) -> Optional[InboxMsg]:
        with self.db._conn() as c:
            row = c.execute(
                """SELECT id,received_ts,source,headers_json,body_json,idempotency_key,state
                   FROM inbox
                   WHERE state='pending'
                   ORDER BY received_ts ASC
                   LIMIT 1"""
            ).fetchone()
        return InboxMsg(*row) if row else None

    def claim_next_pending(self) -> Optional[InboxMsg]:
        """
        Atomar: nimmt die älteste pending Nachricht und setzt sie auf 'processing',
        damit parallel laufende Worker sie nicht doppelt ziehen.
        """
        # Fast idle path: avoid a write transaction every router tick when the
        # inbox is empty. The service has one router worker; if a message arrives
        # just after this read it is picked up on the next short tick.
        msg = self.next_pending()
        if msg is None:
            return None

        with self.db._conn() as c:
            c.execute("BEGIN IMMEDIATE;")
            row = c.execute(
                """SELECT id,received_ts,source,headers_json,body_json,idempotency_key,state
                   FROM inbox
                   WHERE state='pending'
                   ORDER BY received_ts ASC
                   LIMIT 1"""
            ).fetchone()
            if not row:
                c.execute("COMMIT;")
                return None

            msg_id = row[0]
            c.execute("UPDATE inbox SET state='processing' WHERE id=? AND state='pending'", (msg_id,))
            c.execute("COMMIT;")

        return InboxMsg(*row)

    def ack(self, msg_id: int):
        with self.db._conn() as c:
            c.execute("UPDATE inbox SET state='done' WHERE id=?", (msg_id,))

    def nack(self, msg_id: int):
        # falls du mal retry willst
        with self.db._conn() as c:
            c.execute("UPDATE inbox SET state='pending' WHERE id=?", (msg_id,))

    def recover_stale_processing(self, max_age_s: float = 300.0) -> int:
        cutoff = now_ts() - max(1.0, float(max_age_s or 300.0))
        with self.db._conn() as c:
            stale = int(
                c.execute(
                    "SELECT COUNT(*) FROM inbox WHERE state='processing' AND received_ts<?",
                    (cutoff,),
                ).fetchone()[0]
            )
            if stale:
                c.execute(
                    "UPDATE inbox SET state='stale' WHERE state='processing' AND received_ts<?",
                    (cutoff,),
                )
            return stale

    def count_pending(self) -> int:
        with self.db._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0])

    def clear(self, state: Optional[str] = None) -> int:
        with self.db._conn() as c:
            if state:
                deleted = int(c.execute("SELECT COUNT(*) FROM inbox WHERE state=?", (state,)).fetchone()[0])
                c.execute("DELETE FROM inbox WHERE state=?", (state,))
                return deleted
            deleted = int(c.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])
            c.execute("DELETE FROM inbox")
            return deleted
