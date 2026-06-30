import tempfile
import time
import unittest
from pathlib import Path

from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.outbox import Outbox
from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.peers import peer_request_headers


class QueueBehaviorTests(unittest.TestCase):
    def test_peer_request_headers_adds_diclient_adapter_key_for_microtom_peers(self):
        cfg = Settings(
            peer_base_url="https://microtom-primary:9090",
            peer_base_url_secondary="https://microtom-secondary:9090",
            diclient_adapter_key="secret-key",
        )

        headers = peer_request_headers(cfg, "https://microtom-primary:9090/api/inbox", {"X-Test": "1"})

        self.assertEqual("1", headers["X-Test"])
        self.assertEqual("secret-key", headers["X-DIClient-Adapter-Key"])
        self.assertNotIn(
            "X-DIClient-Adapter-Key",
            peer_request_headers(cfg, "https://other-target:9090/api/inbox", {}),
        )

    def test_outbox_prioritizes_lower_priority_number_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "LOW", "source": "raspi"}, priority=100)
            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "HIGH", "source": "raspi"}, priority=10)

            job = outbox.next_due()
            self.assertIsNotNone(job)
            self.assertEqual(10, job.priority)
            self.assertIn('"HIGH"', job.body_json or "")

    def test_outbox_skips_identical_duplicates_for_same_dedupe_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            first = outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "MAS0025=128", "source": "raspi", "origin": "esp-plc"},
                dedupe_key="esp-plc:MAS0025",
                drop_if_duplicate=True,
            )
            second = outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "MAS0025=128", "source": "raspi", "origin": "esp-plc"},
                dedupe_key="esp-plc:MAS0025",
                drop_if_duplicate=True,
            )

            self.assertEqual(first, second)
            self.assertEqual(1, outbox.count())

            outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "MAS0025=129", "source": "raspi", "origin": "esp-plc"},
                dedupe_key="esp-plc:MAS0025",
                drop_if_duplicate=True,
            )
            self.assertEqual(2, outbox.count())

    def test_outbox_keeps_non_consecutive_value_changes_for_same_dedupe_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "TTS0001=3", "source": "raspi", "origin": "vj6530"},
                dedupe_key="vj6530:TTS0001",
                drop_if_duplicate=True,
            )
            outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "TTS0001=0", "source": "raspi", "origin": "vj6530"},
                dedupe_key="vj6530:TTS0001",
                drop_if_duplicate=True,
            )
            outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "TTS0001=3", "source": "raspi", "origin": "vj6530"},
                dedupe_key="vj6530:TTS0001",
                drop_if_duplicate=True,
            )

            self.assertEqual(3, outbox.count())

    def test_outbox_can_replace_pending_state_for_same_dedupe_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "MAS0028=1", "source": "raspi", "origin": "esp-plc"},
                dedupe_key="state:MAS0028",
                replace_existing=True,
            )
            outbox.enqueue(
                "POST",
                "https://peer/api/inbox",
                {},
                {"msg": "MAS0028=0", "source": "raspi", "origin": "machine-runtime"},
                dedupe_key="state:MAS0028",
                replace_existing=True,
            )

            self.assertEqual(1, outbox.count())
            job = outbox.next_due()
            self.assertIn("MAS0028=0", job.body_json or "")

    def test_outbox_deletes_pending_status_updates_without_touching_ack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "MAS0028=1", "source": "raspi"})
            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "ACK_MAS0028=0", "source": "raspi"})

            self.assertEqual(1, outbox.delete_status_updates("MAS0028"))
            self.assertEqual(1, outbox.count())
            job = outbox.next_due()
            self.assertIn("ACK_MAS0028=0", job.body_json or "")

    def test_outbox_can_select_primary_and_non_primary_lanes_independently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue("POST", "http://primary/api/inbox", {}, {"msg": "PRIMARY", "source": "raspi"}, priority=10)
            outbox.enqueue("POST", "https://secondary/api/inbox", {}, {"msg": "SECONDARY", "source": "raspi"}, priority=10)

            primary = outbox.next_due(url_prefixes=["http://primary"])
            self.assertIsNotNone(primary)
            self.assertEqual("http://primary/api/inbox", primary.url)

            non_primary = outbox.next_due(exclude_url_prefixes=["http://primary"])
            self.assertIsNotNone(non_primary)
            self.assertEqual("https://secondary/api/inbox", non_primary.url)

    def test_outbox_lane_filter_matches_exact_base_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue("POST", "https://secondary", {}, {"msg": "BASE", "source": "raspi"}, priority=10)
            outbox.enqueue("POST", "https://secondary/api/inbox", {}, {"msg": "PATH", "source": "raspi"}, priority=20)

            job = outbox.next_due(url_prefixes=["https://secondary"])
            self.assertIsNotNone(job)
            self.assertEqual("https://secondary", job.url)

    def test_outbox_claim_leases_job_until_delete_or_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)

            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "A", "source": "raspi"})

            first = outbox.claim_next_due(lease_s=60.0)
            self.assertIsNotNone(first)
            self.assertIsNone(outbox.claim_next_due(lease_s=60.0))

            outbox.reschedule(first.id, first.retry_count + 1, time.time() - 1.0)
            second = outbox.claim_next_due(lease_s=60.0)
            self.assertIsNotNone(second)
            self.assertEqual(first.id, second.id)

    def test_inbox_recover_stale_processing_quarantines_old_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            inbox = Inbox(db)

            inbox.store("microtom", {}, {"msg": "TTS0001=3"}, "idem-processing")
            msg = inbox.claim_next_pending()
            self.assertIsNotNone(msg)

            with db._conn() as c:
                c.execute("UPDATE inbox SET received_ts=? WHERE id=?", (time.time() - 1000.0, msg.id))

            self.assertEqual(1, inbox.recover_stale_processing(max_age_s=300.0))
            self.assertIsNone(inbox.claim_next_pending())
            with db._conn() as c:
                state = c.execute("SELECT state FROM inbox WHERE id=?", (msg.id,)).fetchone()[0]
            self.assertEqual("stale", state)

    def test_inbox_and_outbox_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DB(str(Path(tmpdir) / "db.sqlite3"))
            outbox = Outbox(db)
            inbox = Inbox(db)

            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "TTP00002=55", "source": "raspi"})
            outbox.enqueue("POST", "https://peer/api/inbox", {}, {"msg": "TTP00003=7", "source": "raspi"})
            inbox.store("microtom", {}, {"msg": "TTP00002=?"}, "idem-1")
            inbox.store("microtom", {}, {"msg": "TTP00003=?"}, "idem-2")

            self.assertEqual(2, outbox.clear())
            self.assertEqual(0, outbox.count())
            self.assertEqual(2, inbox.clear())
            self.assertEqual(0, inbox.count_pending())


if __name__ == "__main__":
    unittest.main()
