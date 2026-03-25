import tempfile
import unittest
from pathlib import Path

from mas004_rpi_databridge.db import DB
from mas004_rpi_databridge.inbox import Inbox
from mas004_rpi_databridge.outbox import Outbox


class QueueBehaviorTests(unittest.TestCase):
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
