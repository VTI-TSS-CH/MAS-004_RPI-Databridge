import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.modules.setdefault("ping3", types.SimpleNamespace(ping=lambda *args, **kwargs: 1.0))

from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.webui import MULTIPART_AVAILABLE, build_app


class MachineSetupAuthTests(unittest.TestCase):
    def build_client(self, *, config_overrides=None, raise_server_exceptions: bool = True) -> TestClient:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        cfg_path = root / "config.json"
        cfg = {
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
        if config_overrides:
            cfg.update(config_overrides)
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self._cfg = cfg
        return TestClient(build_app(str(cfg_path)), raise_server_exceptions=raise_server_exceptions)

    def tearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    def insert_map_param(
        self,
        pkey: str,
        default_v: str,
        *,
        min_v: float | None = None,
        max_v: float | None = None,
    ) -> None:
        db = DB(self._cfg["db_path"])
        with db._conn() as c:
            c.execute(
                """INSERT INTO params(
                    pkey,ptype,pid,min_v,max_v,default_v,unit,rw,esp_rw,dtype,name,format_relevant,
                    message,possible_cause,effects,remedy,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pkey) DO UPDATE SET
                    min_v=excluded.min_v,
                    max_v=excluded.max_v,
                    default_v=excluded.default_v,
                    updated_ts=excluded.updated_ts""",
                (
                    pkey,
                    "MAP",
                    pkey[-4:],
                    min_v,
                    max_v,
                    default_v,
                    "1/10mm",
                    "W",
                    "R",
                    "uint16",
                    pkey,
                    "YES",
                    pkey,
                    "",
                    "",
                    "",
                    now_ts(),
                ),
            )

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

        production_page = client.get("/ui/machine-setup/production", follow_redirects=False)
        self.assertEqual(303, production_page.status_code)
        self.assertEqual("/ui/machine-setup/login?next=/ui/machine-setup/production", production_page.headers.get("location"))

        visualization_page = client.get("/ui/machine-setup/visualization", follow_redirects=False)
        self.assertEqual(303, visualization_page.status_code)
        self.assertEqual(
            "/ui/machine-setup/login?next=/ui/machine-setup/visualization",
            visualization_page.headers.get("location"),
        )

        mae0048_page = client.get("/ui/machine-setup/mae0048", follow_redirects=False)
        self.assertEqual(303, mae0048_page.status_code)
        self.assertEqual(
            "/ui/machine-setup/login?next=/ui/machine-setup/mae0048",
            mae0048_page.headers.get("location"),
        )

        calibration_page = client.get("/ui/machine-setup/calibration", follow_redirects=False)
        self.assertEqual(303, calibration_page.status_code)
        self.assertEqual(
            "/ui/machine-setup/login?next=/ui/machine-setup/calibration",
            calibration_page.headers.get("location"),
        )

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

        production_api = client.get("/api/production-setup/parameters")
        self.assertEqual(401, production_api.status_code)
        self.assertEqual("Machine-Setup login required", production_api.json()["detail"])

        audit_api = client.get("/api/machine/audit")
        self.assertEqual(401, audit_api.status_code)
        self.assertEqual("Machine-Setup login required", audit_api.json()["detail"])

        visualization_api = client.get("/api/machine/production-visualization")
        self.assertEqual(401, visualization_api.status_code)
        self.assertEqual("Machine-Setup login required", visualization_api.json()["detail"])

        mae0048_api = client.get("/api/machine/mae0048-diagnostics")
        self.assertEqual(401, mae0048_api.status_code)
        self.assertEqual("Machine-Setup login required", mae0048_api.json()["detail"])

        calibration_api = client.get("/api/machine/motor3-calibration/status")
        self.assertEqual(401, calibration_api.status_code)
        self.assertEqual("Machine-Setup login required", calibration_api.json()["detail"])

        led_test_api = client.post("/api/machine/led-test", json={"action": "start", "duration_ms": 1000})
        self.assertEqual(401, led_test_api.status_code)
        self.assertEqual("Machine-Setup login required", led_test_api.json()["detail"])

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
        self.assertIn("Machine Control", process_page.text)
        self.assertIn("Virtuelle Maschinentasten", process_page.text)
        self.assertIn(">Control / Audit<", process_page.text)

        commissioning_page = client.get("/ui/machine-setup/commissioning")
        self.assertEqual(200, commissioning_page.status_code)
        self.assertIn("Machine Commissioning", commissioning_page.text)
        self.assertIn(">Commissioning<", commissioning_page.text)

        backups_page = client.get("/ui/machine-setup/backups")
        self.assertEqual(200, backups_page.status_code)
        self.assertIn("Machine Backups", backups_page.text)
        self.assertIn(">Backups<", backups_page.text)

        production_page = client.get("/ui/machine-setup/production")
        self.assertEqual(200, production_page.status_code)
        self.assertIn("Produktion", production_page.text)
        self.assertIn("Formatprofile", production_page.text)
        self.assertIn(">Produktion<", production_page.text)

        visualization_page = client.get("/ui/machine-setup/visualization")
        self.assertEqual(200, visualization_page.status_code)
        self.assertIn("Produktionsvisualisierung", visualization_page.text)
        self.assertIn("Controller Rot", visualization_page.text)
        self.assertIn(">Visualisierung<", visualization_page.text)

        mae0048_page = client.get("/ui/machine-setup/mae0048")
        self.assertEqual(200, mae0048_page.status_code)
        self.assertIn("MAE0048 Diagnose", mae0048_page.text)
        self.assertIn("Stopptoleranz", mae0048_page.text)
        self.assertIn(">MAE0048<", mae0048_page.text)

        calibration_page = client.get("/ui/machine-setup/calibration")
        self.assertEqual(200, calibration_page.status_code)
        self.assertIn("Motor 3 / Encoder Kalibrierung", calibration_page.text)
        self.assertIn("2000-mm-Fahrt starten", calibration_page.text)
        self.assertIn(">Kalibrierung<", calibration_page.text)

        for nav_page in (process_page, visualization_page, mae0048_page, calibration_page):
            self.assertIn(".topnav{", nav_page.text)
            self.assertIn(".navbtn{", nav_page.text)
            self.assertIn('class="navbtn', nav_page.text)

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

        visualization_api = client.get("/api/machine/production-visualization")
        self.assertEqual(200, visualization_api.status_code)
        self.assertIn("track", visualization_api.json())
        self.assertIn("active_labels", visualization_api.json())
        self.assertIn("completed_labels", visualization_api.json())

        mae0048_api = client.get("/api/machine/mae0048-diagnostics")
        self.assertEqual(200, mae0048_api.status_code)
        self.assertIn("params", mae0048_api.json())
        self.assertIn("registration", mae0048_api.json())
        self.assertIn("motor3", mae0048_api.json())
        self.assertIn("findings", mae0048_api.json())

        calibration_api = client.get("/api/machine/motor3-calibration/status")
        self.assertEqual(200, calibration_api.status_code)
        self.assertIn("params", calibration_api.json())
        self.assertIn("MAP0077", calibration_api.json()["params"])

        led_test_api = client.post("/api/machine/led-test", json={"action": "start", "duration_ms": 1000})
        self.assertEqual(200, led_test_api.status_code)
        self.assertTrue(led_test_api.json()["ok"])
        self.assertTrue(led_test_api.json()["simulation"])

        led_stop_api = client.post("/api/machine/led-test", json={"action": "stop"})
        self.assertEqual(200, led_stop_api.status_code)
        self.assertTrue(led_stop_api.json()["ok"])

        audit_api = client.get("/api/machine/audit?hours=1&limit=50")
        self.assertEqual(200, audit_api.status_code)
        self.assertIn("entries", audit_api.json())

        bypass_api = client.get("/api/machine/bypass")
        self.assertEqual(200, bypass_api.status_code)
        self.assertIn("parameters", bypass_api.json())
        self.assertTrue(any(item["pkey"] == "MAP0067" for item in bypass_api.json()["parameters"]))
        self.assertTrue(any(item["pkey"] == "MAP0080" for item in bypass_api.json()["parameters"]))

        audit_retention = client.post("/api/machine/audit/retention", json={"keep_hours": 24})
        self.assertEqual(200, audit_retention.status_code)
        self.assertEqual(24, audit_retention.json()["keep_hours"])

        audit_download = client.get("/api/machine/audit/download?hours=1&limit=50")
        self.assertEqual(200, audit_download.status_code)
        self.assertIn("MAS-004 Machine Audit Log", audit_download.text)

        production_api = client.get("/api/production-setup/parameters")
        self.assertEqual(200, production_api.status_code)
        self.assertIn("parameters", production_api.json())
        self.assertIn("production_status", production_api.json())

        save_profile = client.post(
            "/api/production-setup/profiles",
            json={"name": "Auth Test Format", "note": "smoke", "values": {"MAP0014": "100"}},
        )
        self.assertEqual(200, save_profile.status_code)
        self.assertEqual("Auth Test Format", save_profile.json()["profile"]["name"])

        load_profile = client.get("/api/production-setup/profiles/Auth%20Test%20Format")
        self.assertEqual(200, load_profile.status_code)
        self.assertEqual("100", load_profile.json()["profile"]["values"]["MAP0014"])

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

    def test_visualization_marker_edit_stores_print_distance_default(self):
        client = self.build_client()
        login = client.post(
            "/ui/machine-setup/login",
            json={
                "username": "Admin",
                "password": "VideojetMAS004!",
                "next": "/ui/machine-setup/visualization",
            },
        )
        self.assertEqual(200, login.status_code)
        self.insert_map_param("MAP0016", "0", min_v=0, max_v=1)
        self.insert_map_param("MAP0004", "100", min_v=0, max_v=2500)
        self.insert_map_param("MAP0006", "0", min_v=-50, max_v=50)
        self.insert_map_param("MAP0019", "6000", min_v=3000, max_v=10000)

        api = client.post("/api/machine/production-visualization/component", json={"key": "print", "mm": 607.0})

        self.assertEqual(200, api.status_code)
        saved = api.json()["saved"]
        self.assertEqual("MAP0019", saved["pkey"])
        self.assertEqual("5970", saved["value"])
        db = DB(self._cfg["db_path"])
        with db._conn() as c:
            row = c.execute(
                """SELECT p.default_v, v.value
                   FROM params p
                   LEFT JOIN param_values v ON v.pkey=p.pkey
                   WHERE p.pkey='MAP0019'"""
            ).fetchone()
        self.assertEqual(("5970", "5970"), tuple(row))

    def test_visualization_marker_edit_syncs_led_start_to_esp(self):
        client = self.build_client(
            config_overrides={
                "esp_simulation": False,
                "esp_host": "192.168.2.101",
                "esp_port": 3010,
            }
        )
        login = client.post(
            "/ui/machine-setup/login",
            json={
                "username": "Admin",
                "password": "VideojetMAS004!",
                "next": "/ui/machine-setup/visualization",
            },
        )
        self.assertEqual(200, login.status_code)
        self.insert_map_param("MAP0066", "9600", min_v=0, max_v=20000)
        calls: list[tuple[str, bool]] = []

        class FakeEspPlcClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def exchange_line(self, line, read_timeout_s=None, *, priority=False, **_kwargs):
                calls.append((line, bool(priority)))
                if line == "SYNC MAP0066=9600":
                    return "ACK_MAP0066=9600"
                if line == "MAP0066=?":
                    return "MAP0066=9600"
                return "NAK_Syntax"

        with patch("mas004_rpi_databridge.webui.EspPlcClient", FakeEspPlcClient):
            api = client.post("/api/machine/production-visualization/component", json={"key": "led_start", "mm": 960.0})

        self.assertEqual(200, api.status_code)
        saved = api.json()["saved"]
        self.assertEqual("MAP0066", saved["pkey"])
        self.assertEqual("9600", saved["value"])
        self.assertIn(("SYNC MAP0066=9600", True), calls)
        self.assertIn(("MAP0066=?", True), calls)
        self.assertTrue(saved["esp_sync"]["ok"])
        self.assertEqual("9600", saved["esp_sync"]["actual"])

    def test_settings_ui_and_config_api_expose_light_curtain_auto_reset(self):
        client = self.build_client(config_overrides={"light_curtain_auto_reset_enabled": False})

        page = client.get("/ui/settings")
        self.assertEqual(200, page.status_code)
        self.assertIn('id="light_curtain_auto_reset_enabled"', page.text)

        with patch("mas004_rpi_databridge.webui.subprocess.call") as restart:
            response = client.post("/api/config", json={"light_curtain_auto_reset_enabled": True})

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])
        restart.assert_called_once()
        config = client.get("/api/config").json()["config"]
        self.assertTrue(config["light_curtain_auto_reset_enabled"])

    def test_motor_refresh_esp_connection_failure_is_bad_gateway_not_500(self):
        client = self.build_client(
            config_overrides={"esp_simulation": False, "esp_host": "127.0.0.1", "esp_port": 1},
            raise_server_exceptions=False,
        )
        login = client.post(
            "/ui/machine-setup/login",
            json={
                "username": "Admin",
                "password": "VideojetMAS004!",
                "next": "/ui/machine-setup/motors",
            },
        )
        self.assertEqual(200, login.status_code)

        refresh = client.post("/api/motors/1/refresh", json={})
        self.assertEqual(502, refresh.status_code)
        self.assertIn("ESP motor communication failed during REFRESH", refresh.json()["detail"])


if __name__ == "__main__":
    unittest.main()
