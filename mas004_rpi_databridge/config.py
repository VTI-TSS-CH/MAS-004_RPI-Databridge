import json
import os
from dataclasses import dataclass

DEFAULT_CFG_PATH = "/etc/mas004_rpi_databridge/config.json"

@dataclass
class Settings:
    # Storage
    db_path: str = "/var/lib/mas004_rpi_databridge/databridge.db"
    master_params_xlsx_path: str = "/var/lib/mas004_rpi_databridge/master/Parameterliste_master.xlsx"
    master_ios_xlsx_path: str = "/var/lib/mas004_rpi_databridge/master/SAR41-MAS-004_SPS_I-Os.xlsx"
    backup_root_path: str = "/var/lib/mas004_rpi_databridge/backups"
    machine_serial_number: str = ""
    machine_name: str = ""

    # Web UI
    webui_host: str = "0.0.0.0"
    webui_port: int = 8080
    webui_https: bool = False
    webui_ssl_certfile: str = "/etc/mas004_rpi_databridge/certs/raspi.crt"
    webui_ssl_keyfile: str = "/etc/mas004_rpi_databridge/certs/raspi.key"
    device_inbox_http_enabled: bool = True
    device_inbox_http_host: str = "0.0.0.0"
    device_inbox_http_port: int = 8081

    # Interface labels (Info)
    eth0_ip: str = ""
    eth1_ip: str = ""
    eth0_subnet: str = "24"
    eth0_gateway: str = ""
    eth0_dns: str = ""
    eth1_subnet: str = "24"
    eth1_gateway: str = ""
    eth1_dns: str = ""
    eth0_source_ip: str = ""  # outgoing source bind for HttpClient (optional)

    # Peer (Microtom)
    peer_base_url: str = "http://127.0.0.1:9090"
    peer_base_url_secondary: str = ""  # optional parallel target (e.g. local Microtom test tool via VPN/WireGuard)
    peer_watchdog_host: str = "127.0.0.1"
    peer_health_path: str = "/health"

    # Watchdog
    watchdog_interval_s: float = 2.0
    watchdog_timeout_s: float = 1.0
    watchdog_down_after: int = 3

    # HTTP client
    http_timeout_s: float = 10.0
    tls_verify: bool = False

    # NTP sync
    ntp_server: str = ""
    ntp_sync_interval_min: int = 60

    # Retry
    retry_base_s: float = 1.0
    retry_cap_s: float = 60.0

    # Device endpoints (optional; editable in UI)
    # ESP-PLC58 on ETH1
    esp_host: str = "192.168.2.101"
    esp_port: int = 3010
    esp_simulation: bool = True
    esp_watchdog_host: str = ""
    esp_io_poll_interval_s: float = 1.0
    raspi_plc_model: str = "RPIPLC_21"
    raspi_io_simulation: bool = True
    raspi_io_poll_interval_s: float = 1.0

    # Moxa ioLogik E1211 on ETH1 (Modbus/TCP direct, default port 502)
    moxa1_host: str = "192.168.2.102"
    moxa1_port: int = 502
    moxa1_simulation: bool = True
    moxa2_host: str = "192.168.2.103"
    moxa2_port: int = 502
    moxa2_simulation: bool = True
    moxa_poll_interval_s: float = 1.0

    # Printers / Wickler on ETH0
    vj3350_host: str = "192.168.210.21"
    vj3350_port: int = 20000
    vj3350_simulation: bool = True
    vj6530_host: str = "192.168.210.22"
    vj6530_port: int = 3002
    vj6530_simulation: bool = True
    vj6530_poll_interval_s: float = 15.0
    vj6530_async_enabled: bool = True
    smart_unwinder_host: str = "192.168.210.23"
    smart_unwinder_port: int = 3011
    smart_unwinder_simulation: bool = True
    smart_rewinder_host: str = "192.168.210.24"
    smart_rewinder_port: int = 3012
    smart_rewinder_simulation: bool = True

    # Daily log-file retention (days)
    logs_keep_days_all: int = 30
    logs_keep_days_esp: int = 30
    logs_keep_days_tto: int = 30
    logs_keep_days_laser: int = 30

    # UI/API auth
    ui_token: str = "change-me"
    shared_secret: str = ""

    @classmethod
    def load(cls, path: str = DEFAULT_CFG_PATH) -> "Settings":
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cfg = cls()
            cfg.save(path)
            return cfg
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        cfg = cls()
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def save(self, path: str = DEFAULT_CFG_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2, sort_keys=False)
