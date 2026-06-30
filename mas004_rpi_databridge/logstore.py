import json
import os
import re
from datetime import datetime, timedelta, date
from typing import List, Dict, Any, Optional

from mas004_rpi_databridge.config import Settings, DEFAULT_CFG_PATH
from mas004_rpi_databridge.db import DB, now_ts
from mas004_rpi_databridge.params import ParamStore
from mas004_rpi_databridge.production_logs import ProductionLogManager, DEFAULT_PRODUCTION_LOG_DIR
from mas004_rpi_databridge.timeutil import format_local_timestamp, local_date

DEFAULT_LOG_DIR = "/var/lib/mas004_rpi_databridge/logs"

# Channels that should ALWAYS exist in the UI dropdown, even if no logs exist yet.
# "all" is a virtual channel (aggregates all channels).
DEFAULT_LOG_CHANNELS = ["all", "raspi", "machine", "esp-plc", "moxa1", "moxa2", "moxa3", "raspi-io", "vj3350", "vj6530"]

DAILY_GROUP_ALL = "all"
DAILY_GROUP_ESP = "esp"
DAILY_GROUP_TTO = "tto"
DAILY_GROUP_LASER = "laser"
DAILY_GROUPS = [DAILY_GROUP_ALL, DAILY_GROUP_ESP, DAILY_GROUP_TTO, DAILY_GROUP_LASER]

DAILY_GROUP_LABELS = {
    DAILY_GROUP_ALL: "Raspi All Communications",
    DAILY_GROUP_ESP: "ESP32-PLC",
    DAILY_GROUP_TTO: "TTO 6530",
    DAILY_GROUP_LASER: "Laser 3350",
}

DAILY_GROUP_PREFIX = {
    DAILY_GROUP_ALL: "raspi_all",
    DAILY_GROUP_ESP: "esp32_plc",
    DAILY_GROUP_TTO: "tto_6530",
    DAILY_GROUP_LASER: "laser_3350",
}

DAILY_CHANNEL_GROUP = {
    "esp-plc": DAILY_GROUP_ESP,
    "vj6530": DAILY_GROUP_TTO,
    "vj3350": DAILY_GROUP_LASER,
}

AUDIT_CHANNEL_LABELS = {
    "raspi": "Raspi / Microtom",
    "machine": "Maschinensteuerung",
    "esp-plc": "ESP32-PLC",
    "raspi-io": "Raspberry PLC 21 I/O",
    "moxa1": "Moxa E1213 #1",
    "moxa2": "Moxa E1213 #2",
    "moxa3": "Moxa E1213 #3",
    "vj6530": "Videojet 6530 TTO",
    "vj3350": "Videojet 3350 Laser",
    "smartwickler": "Smart Wickler",
}


class LogStore:
    def __init__(
        self,
        db: DB,
        log_dir: str = DEFAULT_LOG_DIR,
        cfg_path: str = DEFAULT_CFG_PATH,
        production_log_dir: Optional[str] = None,
    ):
        self.db = db
        self.log_dir = log_dir
        self.cfg_path = cfg_path
        self._next_housekeeping_ts = 0.0
        self._production = ProductionLogManager(db, log_dir=production_log_dir or DEFAULT_PRODUCTION_LOG_DIR)
        self._params = ParamStore(db)
        self._meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        os.makedirs(self.log_dir, exist_ok=True)

    def log(self, channel: str, direction: str, message: str):
        ts = now_ts()
        channel = (channel or "raspi").strip() or "raspi"
        direction = (direction or "").strip().upper()

        # DB
        with self.db._conn() as c:
            c.execute(
                "INSERT INTO logs(ts, channel, direction, message) VALUES (?,?,?,?)",
                (ts, channel, direction, message),
            )
            # Retention in DB: per channel keep last ~5000 entries.
            c.execute(
                """DELETE FROM logs
                   WHERE channel=?
                     AND id NOT IN (
                       SELECT id FROM logs WHERE channel=? ORDER BY id DESC LIMIT 5000
                     )""",
                (channel, channel),
            )

        self._write_daily_logfiles(ts, channel, direction, message)
        self._write_production_logfiles(ts, channel, direction, message)
        self._maybe_housekeeping(ts)

    def _log_line(self, ts: float, channel: str, direction: str, message: str) -> str:
        enriched = self._enrich_message(message)
        return f"[{format_local_timestamp(ts)}] [{channel}] {direction} {enriched}\n"

    def _extract_pkey(self, message: str) -> Optional[str]:
        s = (message or "").strip()
        if not s:
            return None
        m = re.search(r"(?:ACK_)?([A-Z]{3})(\d{4,5})\s*=", s)
        if not m:
            return None
        return f"{m.group(1)}{m.group(2)}"

    def _meta_for_pkey(self, pkey: str) -> Optional[Dict[str, Any]]:
        if pkey in self._meta_cache:
            return self._meta_cache[pkey]
        meta = self._params.get_meta(pkey)
        self._meta_cache[pkey] = meta
        return meta

    def _clean_meta_text(self, value: Any) -> str:
        txt = str(value or "").replace("\r", " ").replace("\n", " ")
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt

    def _enrich_message(self, message: str) -> str:
        pkey = self._extract_pkey(message)
        if not pkey:
            return message
        meta = self._meta_for_pkey(pkey)
        if not meta:
            return message
        parts = [message]
        name = self._clean_meta_text(meta.get("name"))
        desc = self._clean_meta_text(meta.get("message"))
        if name:
            parts.append(f"NAME: {name}")
        if desc:
            parts.append(f"DESC: {desc}")
        return " | ".join(parts)

    def _groups_for_channel(self, channel: str) -> List[str]:
        groups = [DAILY_GROUP_ALL]
        g = DAILY_CHANNEL_GROUP.get((channel or "").strip())
        if g:
            groups.append(g)
        return groups

    def _daily_path(self, group: str, d: date) -> str:
        prefix = DAILY_GROUP_PREFIX[group]
        return os.path.join(self.log_dir, f"{prefix}_{d:%Y-%m-%d}.txt")

    def _write_daily_logfiles(self, ts: float, channel: str, direction: str, message: str):
        d = local_date(ts)
        line = self._log_line(ts, channel, direction, message)
        for group in self._groups_for_channel(channel):
            fn = self._daily_path(group, d)
            try:
                with open(fn, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                # logging errors must never break runtime path
                pass

    def _write_production_logfiles(self, ts: float, channel: str, direction: str, message: str):
        state = self._production.active_state()
        if not state:
            return
        label = str(state.get("production_label") or "").strip()
        if not label:
            return
        line = self._log_line(ts, channel, direction, message)
        for group in self._groups_for_channel(channel):
            try:
                fn = self._production.path_for_group(group, label)
                with open(fn, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    def _safe_days(self, value: Any, default_v: int) -> int:
        try:
            n = int(value)
        except Exception:
            n = int(default_v)
        return max(1, min(n, 3650))

    def retention_map_from_settings(self, cfg: Optional[Settings] = None) -> Dict[str, int]:
        cfg2 = cfg if cfg is not None else Settings.load(self.cfg_path)
        return {
            DAILY_GROUP_ALL: self._safe_days(getattr(cfg2, "logs_keep_days_all", 30), 30),
            DAILY_GROUP_ESP: self._safe_days(getattr(cfg2, "logs_keep_days_esp", 30), 30),
            DAILY_GROUP_TTO: self._safe_days(getattr(cfg2, "logs_keep_days_tto", 30), 30),
            DAILY_GROUP_LASER: self._safe_days(getattr(cfg2, "logs_keep_days_laser", 30), 30),
        }

    def apply_retention(self, cfg: Optional[Settings] = None):
        keep_map = self.retention_map_from_settings(cfg)
        today = local_date()
        items = self.list_daily_files()

        for it in items:
            group = it.get("group")
            d = it.get("_date_obj")
            path = it.get("_path")
            if not group or not d or not path:
                continue

            keep_days = keep_map.get(group, 30)
            cutoff = today - timedelta(days=max(1, keep_days) - 1)
            if d < cutoff:
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _maybe_housekeeping(self, ts: float):
        if ts < self._next_housekeeping_ts:
            return
        self._next_housekeeping_ts = ts + 300.0
        try:
            self.apply_retention()
            self.apply_audit_retention()
        except Exception:
            pass

    def _safe_hours(self, value: Any, default_v: int) -> int:
        try:
            n = int(float(value))
        except Exception:
            n = int(default_v)
        return max(1, min(n, 24 * 3650))

    def audit_keep_hours_from_settings(self, cfg: Optional[Settings] = None) -> int:
        cfg2 = cfg if cfg is not None else Settings.load(self.cfg_path)
        return self._safe_hours(getattr(cfg2, "machine_audit_keep_hours", 72), 72)

    def apply_audit_retention(self, cfg: Optional[Settings] = None):
        keep_hours = self.audit_keep_hours_from_settings(cfg)
        cutoff = now_ts() - (keep_hours * 3600.0)
        with self.db._conn() as c:
            c.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM machine_events WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM label_events WHERE ts < ?", (cutoff,))

    def _extract_pkey_value(self, message: str) -> tuple[Optional[str], Optional[str], bool]:
        s = (message or "").strip()
        if not s:
            return None, None, False
        m = re.search(r"\b(ACK_)?([A-Z]{3})(\d{4,5})\s*=\s*([^|\s]+)", s)
        if not m:
            return None, None, False
        value = str(m.group(4) or "").rstrip(";,")
        return f"{m.group(2)}{m.group(3)}", value, bool(m.group(1))

    def _audit_summary_for_log(self, channel: str, direction: str, message: str) -> dict[str, Any]:
        pkey, value, ack = self._extract_pkey_value(message)
        meta = self._meta_for_pkey(pkey) if pkey else None
        device = AUDIT_CHANNEL_LABELS.get((channel or "").strip(), channel or "Unbekannt")
        direction_text = {
            "IN": "empfangen",
            "OUT": "gesendet",
            "INFO": "Info",
            "ERR": "Fehler",
            "ERROR": "Fehler",
        }.get((direction or "").strip().upper(), (direction or "").strip() or "Info")
        title = self._clean_meta_text((meta or {}).get("name")) if meta else ""
        desc = self._clean_meta_text((meta or {}).get("message")) if meta else ""
        if pkey:
            code_text = f"{'ACK ' if ack else ''}{pkey}={value if value is not None else ''}".strip()
            human = f"{device}: {direction_text} {code_text}"
            if title:
                human += f" - {title}"
            if desc:
                human += f" ({desc})"
        else:
            human = f"{device}: {direction_text} {(message or '').strip()}"
        return {
            "pkey": pkey or "",
            "value": value if value is not None else "",
            "ack": ack,
            "device": device,
            "summary": human,
            "description": desc,
            "name": title,
        }

    def _truthy_audit_value(self, value: Any) -> bool:
        text = str(value or "").strip().lower()
        return text not in ("", "0", "false", "off", "no", "none", "null")

    def _audit_category_for_log(self, channel: str, pkey: str, ack: bool) -> str:
        channel_key = (channel or "").strip().lower()
        key = (pkey or "").strip().upper()
        if channel_key == "machine":
            return "machine"
        if ack:
            return "communication"
        if key.startswith("MAE") or key.startswith("MAW") or key == "MAS0028":
            return "machine"
        return "communication"

    def _audit_direction_for_log(self, direction: str, pkey: str, value: str, ack: bool) -> str:
        base = str(direction or "").strip().upper()
        if ack:
            return base
        key = (pkey or "").strip().upper()
        if key.startswith("MAE") and self._truthy_audit_value(value):
            return "ERROR"
        if (key.startswith("MAW") or key == "MAS0028") and self._truthy_audit_value(value):
            return "WARNING"
        return base

    def list_audit_entries(self, *, hours: Optional[int] = None, limit: int = 500) -> List[Dict[str, Any]]:
        keep_hours = self._safe_hours(hours if hours is not None else self.audit_keep_hours_from_settings(), 72)
        limit = max(1, min(int(limit), 5000))
        cutoff = now_ts() - (keep_hours * 3600.0)
        entries: List[Dict[str, Any]] = []
        with self.db._conn() as c:
            log_rows = c.execute(
                """SELECT ts, channel, direction, message
                   FROM logs
                   WHERE ts >= ?
                   ORDER BY ts DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
            event_rows = c.execute(
                """SELECT ts, event_type, severity, message, payload_json
                   FROM machine_events
                   WHERE ts >= ?
                   ORDER BY ts DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
            label_rows = c.execute(
                """SELECT ts, production_label, label_no, event_type, payload_json
                   FROM label_events
                   WHERE ts >= ?
                   ORDER BY ts DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()

        for ts, channel, direction, message in log_rows:
            summary = self._audit_summary_for_log(str(channel or ""), str(direction or ""), str(message or ""))
            category = self._audit_category_for_log(
                str(channel or ""),
                str(summary.get("pkey") or ""),
                bool(summary.get("ack")),
            )
            audit_direction = self._audit_direction_for_log(
                str(direction or ""),
                str(summary.get("pkey") or ""),
                str(summary.get("value") or ""),
                bool(summary.get("ack")),
            )
            entries.append(
                {
                    "ts": float(ts or 0.0),
                    "ts_display": format_local_timestamp(float(ts or 0.0)),
                    "category": category,
                    "source": channel,
                    "direction": audit_direction,
                    "message": message,
                    **summary,
                }
            )
        for ts, event_type, severity, message, payload_json in event_rows:
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                payload = {}
            entries.append(
                {
                    "ts": float(ts or 0.0),
                    "ts_display": format_local_timestamp(float(ts or 0.0)),
                    "category": "machine",
                    "source": "machine",
                    "direction": str(severity or "info").upper(),
                    "message": message,
                    "pkey": "",
                    "value": "",
                    "ack": False,
                    "device": "Maschinensteuerung",
                    "summary": f"Maschinenereignis: {message}",
                    "description": str(event_type or ""),
                    "name": str(event_type or ""),
                    "payload": payload,
                }
            )
        for ts, production_label, label_no, event_type, payload_json in label_rows:
            try:
                payload = json.loads(payload_json or "{}")
            except Exception:
                payload = {}
            entries.append(
                {
                    "ts": float(ts or 0.0),
                    "ts_display": format_local_timestamp(float(ts or 0.0)),
                    "category": "label",
                    "source": "label-process",
                    "direction": "INFO",
                    "message": f"Label {label_no} {event_type}",
                    "pkey": "MAS0003",
                    "value": "",
                    "ack": False,
                    "device": "Etikettenprozess",
                    "summary": f"Label {label_no} fuer Produktion {production_label}: {event_type}",
                    "description": "Label-Schieberegister / Produktions-Audit",
                    "name": "Labelereignis",
                    "payload": payload,
                }
            )
        entries.sort(key=lambda item: float(item.get("ts") or 0.0), reverse=True)
        return entries[:limit]

    def read_audit_log(self, *, hours: Optional[int] = None, limit: int = 5000) -> str:
        entries = self.list_audit_entries(hours=hours, limit=limit)
        lines = [
            "# MAS-004 Machine Audit Log",
            f"# generated: {format_local_timestamp(now_ts())}",
            f"# window_hours: {self._safe_hours(hours if hours is not None else self.audit_keep_hours_from_settings(), 72)}",
            "",
        ]
        for item in reversed(entries):
            lines.append(
                f"[{item.get('ts_display','')}] "
                f"[{item.get('category','')}] "
                f"[{item.get('source','')}] "
                f"{item.get('direction','')} "
                f"{item.get('summary','')}"
            )
        return "\n".join(lines) + "\n"

    def list_logs(self, channel: str, limit: int = 200) -> List[Dict[str, Any]]:
        """
        Returns logs newest->oldest (UI-friendly).
        For channel='all' it returns aggregated logs including the channel field.
        """
        limit = max(1, min(int(limit), 2000))
        channel = (channel or "").strip()

        with self.db._conn() as c:
            if channel == "all":
                rows = c.execute(
                    "SELECT ts, channel, direction, message FROM logs ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [
                    {
                        "ts": r[0],
                        "ts_display": format_local_timestamp(r[0]),
                        "channel": r[1],
                        "direction": r[2],
                        "message": r[3],
                    }
                    for r in rows
                ]

            rows = c.execute(
                "SELECT ts, channel, direction, message FROM logs WHERE channel=? ORDER BY ts DESC LIMIT ?",
                (channel, limit),
            ).fetchall()

        return [
            {
                "ts": r[0],
                "ts_display": format_local_timestamp(r[0]),
                "channel": r[1],
                "direction": r[2],
                "message": r[3],
            }
            for r in rows
        ]

    def read_logfile(self, channel: str, max_bytes: int = 500_000) -> str:
        """
        Legacy endpoint for test UI download.
        Returns text generated from DB to avoid dependency on legacy single-file logs.
        """
        channel = (channel or "").strip()
        limit = 2500 if channel == "all" else 1500
        items = self.list_logs(channel if channel else "all", limit=limit)
        lines = []
        for it in items:
            ts = float(it.get("ts") or 0.0)
            ch = str(it.get("channel") or "")
            direction = str(it.get("direction") or "").upper()
            msg = str(it.get("message") or "")
            if channel == "all":
                lines.append(f"[{format_local_timestamp(ts)}] [{ch}] {direction} {msg}")
            else:
                lines.append(f"[{format_local_timestamp(ts)}] {direction} {msg}")

        txt = "\n".join(lines) + ("\n" if lines else "")
        data = txt.encode("utf-8", errors="replace")
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    def clear_channel(self, channel: str) -> Dict[str, Any]:
        channel = (channel or "").strip()

        if channel == "all":
            with self.db._conn() as c:
                c.execute("DELETE FROM logs")
            return {"ok": True}

        with self.db._conn() as c:
            c.execute("DELETE FROM logs WHERE channel=?", (channel,))

        return {"ok": True}

    def list_channels(self) -> List[str]:
        """
        Returns channels with DEFAULT_LOG_CHANNELS first (stable order),
        then any additional channels sorted alphabetically.
        """
        ch = set(DEFAULT_LOG_CHANNELS)
        with self.db._conn() as c:
            rows = c.execute("SELECT DISTINCT channel FROM logs").fetchall()
            for r in rows:
                if r and r[0]:
                    ch.add(str(r[0]))

        ordered = []
        for d in DEFAULT_LOG_CHANNELS:
            if d in ch:
                ordered.append(d)

        rest = sorted([x for x in ch if x not in set(DEFAULT_LOG_CHANNELS)])
        return ordered + rest

    def list_daily_files(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            names = os.listdir(self.log_dir)
        except Exception:
            return out

        for group, prefix in DAILY_GROUP_PREFIX.items():
            rx = re.compile(rf"^{re.escape(prefix)}_(\d{{4}}-\d{{2}}-\d{{2}})\.txt$")
            for fn in names:
                m = rx.match(fn)
                if not m:
                    continue
                date_s = m.group(1)
                try:
                    d = datetime.strptime(date_s, "%Y-%m-%d").date()
                except Exception:
                    continue
                p = os.path.join(self.log_dir, fn)
                try:
                    st = os.stat(p)
                except Exception:
                    continue
                out.append(
                    {
                        "name": fn,
                        "group": group,
                        "group_label": DAILY_GROUP_LABELS.get(group, group),
                        "date": date_s,
                        "size_bytes": int(st.st_size),
                        "mtime_ts": float(st.st_mtime),
                        "_path": p,
                        "_date_obj": d,
                    }
                )

        out.sort(key=lambda x: (x.get("date", ""), x.get("name", "")), reverse=True)
        return out

    def read_daily_file(self, name: str, max_bytes: int = 5_000_000) -> str:
        safe_name = os.path.basename((name or "").strip())
        if safe_name != name:
            raise RuntimeError("invalid file name")
        if not safe_name.endswith(".txt"):
            raise RuntimeError("invalid file type")

        known = {it["name"] for it in self.list_daily_files()}
        if safe_name not in known:
            raise RuntimeError("file not found")

        p = os.path.join(self.log_dir, safe_name)
        with open(p, "rb") as f:
            data = f.read()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    def list_production_files(self) -> Dict[str, Any]:
        return self._production.ready_manifest()

    def resolve_production_file(self, name: str) -> str:
        return self._production.resolve_ready_file(name)

    def acknowledge_production_files(self) -> Dict[str, Any]:
        return self._production.acknowledge_ready()

    def consume_production_file(self, name: str, max_bytes: int = 5_000_000) -> bytes:
        return self._production.consume_ready_file(name, max_bytes=max_bytes)
