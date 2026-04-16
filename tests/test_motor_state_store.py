import tempfile
import unittest
from pathlib import Path

from mas004_rpi_databridge.config import Settings
from mas004_rpi_databridge.motor_state_store import MotorStateStore


class MotorStateStoreTests(unittest.TestCase):
    def test_simulation_ids_and_cache_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Settings(db_path=str(Path(tmp) / "databridge.db"))
            store = MotorStateStore(cfg)
            ids = store.set_simulation(3, True)
            self.assertEqual({3}, ids)
            store.remember_motors([{"id": 3, "state": {"feedback_tenths_mm": 42}, "config": {"speed_mm_s": 12}}])

            again = MotorStateStore(cfg)
            self.assertEqual({3}, again.simulation_ids())
            cached = again.cached_motors()
            self.assertEqual(42, cached[3]["state"]["feedback_tenths_mm"])
            self.assertEqual(12, cached[3]["config"]["speed_mm_s"])


if __name__ == "__main__":
    unittest.main()
