import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.modules.setdefault("ping3", types.SimpleNamespace(ping=lambda *args, **kwargs: 1.0))

from mas004_rpi_databridge.webui import MULTIPART_AVAILABLE, build_app


class MachineSetupAuthTests(unittest.TestCase):
    def build_client(self) -> TestClient:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        cfg_path = root / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "db_path": str(root / "databridge.db"),
                    "master_params_xlsx_path": str(root / "master" / "Parameterliste_master.xlsx"),
                    "master_ios_xlsx_path": str(root / "master" / "SAR41-MAS-004_SPS_I-Os.xlsx"),
                    "backup_root_path": str(root / "backups"),
                    "ui_token": "",
                    "shared_secret": "",
                    "esp_simulation": True,
                    "moxa1_simulation": True,
                    "moxa2_simulation": True,
                    "vj6530_simulation": True,
                    "vj3350_simulation": True,
                    "smart_unwinder_simulation": True,
                    "smart_rewinder_simulation": True,
                }
            ),
            encoding="utf-8",
        )
        return TestClient(build_app(str(cfg_path)))

    def tearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def test_machine_setup_requires_login(self):
        client = self.build_client()
        page = client.get("/ui/machine-setup/motors", follow_redirects=False)
        self.assertEqual(303, page.status_code)
        self.assertEqual("/ui/machine-setup/login?next=/ui/machine-setup/motors", page.headers.get("location"))

        io_page = client.get("/ui/machine-setup/io", follow_redirects=False)
        self.assertEqual(303, io_page.status_code)
        self.assertEqual("/ui/machine-setup/login?next=/ui/machine-setup/io", io_page.headers.get("location"))

        commissioning_page = client.get("/ui/machine-setup/commissioning", follow_redirects=False)
        self.assertEqual(303, commissioning_page.status_code)
        self.assertEqual("/ui/machine-setup/login?next=/ui/machine-setup/commissioning", commissioning_page.headers.get("location"))

        backups_page = client.get("/ui/machine-setup/backups", follow_redirects=False)
        self.assertEqual(303, backups_page.status_code)
        self.assertEqual("/ui/machine-setup/login?next=/ui/machine-setup/backups", backups_page.headers.get("location"))

        api = client.get("/api/motors/overview")
        self.assertEqual(401, api.status_code)
        self.assertEqual("Machine-Setup login required", api.json()["detail"])

        io_api = client.get("/api/io/overview")
        self.assertEqual(401, io_api.status_code)
        self.assertEqual("Machine-Setup login required", io_api.json()["detail"])

        commissioning_api = client.get("/api/commissioning/overview")
        self.assertEqual(401, commissioning_api.status_code)
        self.assertEqual("Machine-Setup login required", commissioning_api.json()["detail"])

        backups_api = client.get("/api/backups/overview")
        self.assertEqual(401, backups_api.status_code)
        self.assertEqual("Machine-Setup login required", backups_api.json()["detail"])

    def test_machine_setup_login_unlocks_ui_and_api(self):
        client = self.build_client()

        login = client.post(
            "/ui/machine-setup/login",
            json={
                "username": "Admin",
                "password": "VideojetMAS004!",
                "next": "/ui/machine-setup/motors",
            },
        )
        self.assertEqual(200, login.status_code)
        self.assertEqual("/ui/machine-setup/motors", login.json()["redirect"])
        self.assertIn("mas004_machine_setup=", login.headers.get("set-cookie", ""))

        page = client.get("/ui/machine-setup/motors")
        self.assertEqual(200, page.status_code)
        self.assertIn("Machine-Setup", page.text)
        self.assertIn(">Motors<", page.text)
        self.assertNotIn('window.open(target, "_blank"', page.text)

        io_page = client.get("/ui/machine-setup/io")
        self.assertEqual(200, io_page.status_code)
        self.assertIn("Hardware I/O", io_page.text)
        self.assertIn(">I/O<", io_page.text)

        process_page = client.get("/ui/machine-setup/process")
        self.assertEqual(200, process_page.status_code)
        self.assertIn("Machine Process", process_page.text)
        self.assertIn(">Process<", process_page.text)

        commissioning_page = client.get("/ui/machine-setup/commissioning")
        self.assertEqual(200, commissioning_page.status_code)
        self.assertIn("Machine Commissioning", commissioning_page.text)
        self.assertIn(">Commissioning<", commissioning_page.text)

        backups_page = client.get("/ui/machine-setup/backups")
        self.assertEqual(200, backups_page.status_code)
        self.assertIn("Machine Backups", backups_page.text)
        self.assertIn(">Backups<", backups_page.text)

        api = client.get("/api/motors/overview")
        self.assertEqual(200, api.status_code)
        payload = api.json()
        self.assertIn("motors", payload)
        self.assertEqual(9, len(payload["motors"]))

        io_api = client.get("/api/io/overview")
        self.assertEqual(200, io_api.status_code)
        self.assertIn("points", io_api.json())

        process_api = client.get("/api/machine/overview")
        self.assertEqual(200, process_api.status_code)
        self.assertIn("current_state", process_api.json())

        commissioning_api = client.get("/api/commissioning/overview")
        self.assertEqual(200, commissioning_api.status_code)
        self.assertIn("bootstrap_script", commissioning_api.json())

        backups_api = client.get("/api/backups/overview")
        self.assertEqual(200, backups_api.status_code)
        self.assertIn("backups", backups_api.json())

        identity_api = client.post(
            "/api/backups/identity",
            json={"machine_serial_number": "MAS004-LOGIN-TEST", "machine_name": "Auth Test Rig"},
        )
        self.assertEqual(200, identity_api.status_code)
        self.assertEqual("MAS004-LOGIN-TEST", identity_api.json()["identity"]["machine_serial_number"])

        create_backup = client.post(
            "/api/backups/create",
            json={"backup_type": "settings", "name": "auth-test-backup", "note": "created from auth test"},
        )
        self.assertEqual(200, create_backup.status_code)
        backup_id = create_backup.json()["backup"]["backup_id"]

        download = client.get(f"/api/backups/{backup_id}/download")
        self.assertEqual(200, download.status_code)

        import_route = client.post("/api/backups/import")
        if MULTIPART_AVAILABLE:
            self.assertEqual(422, import_route.status_code)
        else:
            self.assertEqual(503, import_route.status_code)
            self.assertEqual("Backup-Import nicht verfuegbar (python-multipart fehlt)", import_route.json()["detail"])

        legacy = client.get("/ui/motors", follow_redirects=False)
        self.assertEqual(303, legacy.status_code)
        self.assertEqual("/ui/machine-setup/motors", legacy.headers.get("location"))


if __name__ == "__main__":
    unittest.main()
