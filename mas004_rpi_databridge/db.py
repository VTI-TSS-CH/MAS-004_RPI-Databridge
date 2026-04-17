import sqlite3
import time
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_ts REAL NOT NULL,
  method TEXT NOT NULL,
  url TEXT NOT NULL,
  headers_json TEXT NOT NULL,
  body_json TEXT,
  idempotency_key TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  dedupe_key TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_next ON outbox(next_attempt_ts, created_ts);

CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_ts REAL NOT NULL,
  source TEXT,
  headers_json TEXT NOT NULL,
  body_json TEXT,
  idempotency_key TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_dedupe ON inbox(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_inbox_state ON inbox(state, received_ts);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  channel TEXT NOT NULL,
  direction TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_ch_ts ON logs(channel, ts);

-- ===== Parameter-Tabellen =====
CREATE TABLE IF NOT EXISTS params (
  pkey TEXT PRIMARY KEY,          -- z.B. TTP00002
  ptype TEXT NOT NULL,            -- z.B. TTP
  pid TEXT NOT NULL,              -- z.B. 00002
  min_v REAL,
  max_v REAL,
  default_v TEXT,
  unit TEXT,
  rw TEXT,                        -- R / W / R/W
  esp_rw TEXT,                    -- R / W / N from ESP32 perspective
  dtype TEXT,
  name TEXT,
  format_relevant TEXT,
  message TEXT,
  possible_cause TEXT,
  effects TEXT,
  remedy TEXT,
  ai_instructions TEXT,
  updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_params_type_id ON params(ptype, pid);

CREATE TABLE IF NOT EXISTS param_values (
  pkey TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_ts REAL NOT NULL,
  FOREIGN KEY(pkey) REFERENCES params(pkey) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS param_device_map (
  pkey TEXT PRIMARY KEY,
  esp_key TEXT,
  zbc_mapping TEXT,
  zbc_message_id INTEGER,
  zbc_command_id INTEGER,
  zbc_value_codec TEXT,
  zbc_scale REAL,
  zbc_offset REAL,
  ultimate_set_cmd TEXT,
  ultimate_get_cmd TEXT,
  ultimate_var_name TEXT,
  updated_ts REAL NOT NULL,
  FOREIGN KEY(pkey) REFERENCES params(pkey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_param_device_map_zbc ON param_device_map(zbc_message_id, zbc_command_id);

CREATE TABLE IF NOT EXISTS io_master_meta (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  source_path TEXT NOT NULL,
  source_filename TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  imported_ts REAL NOT NULL,
  file_mtime_ts REAL NOT NULL DEFAULT 0,
  channel_count INTEGER NOT NULL DEFAULT 0,
  notes_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS io_points (
  io_key TEXT PRIMARY KEY,
  device_code TEXT NOT NULL,
  device_label TEXT NOT NULL,
  sheet_name TEXT NOT NULL,
  zone_label TEXT,
  pin_label TEXT NOT NULL,
  io_dir TEXT NOT NULL,
  channel_no INTEGER,
  function_text TEXT,
  is_reserved INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  source_row INTEGER NOT NULL,
  updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_io_points_device ON io_points(device_code, pin_label);
CREATE INDEX IF NOT EXISTS idx_io_points_sheet ON io_points(sheet_name, source_row);

CREATE TABLE IF NOT EXISTS io_values (
  io_key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  quality TEXT NOT NULL DEFAULT 'unknown',
  source TEXT NOT NULL DEFAULT '',
  updated_ts REAL NOT NULL,
  FOREIGN KEY(io_key) REFERENCES io_points(io_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_io_values_quality ON io_values(quality, updated_ts);

CREATE TABLE IF NOT EXISTS machine_state (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  current_state INTEGER NOT NULL DEFAULT 1,
  requested_state INTEGER NOT NULL DEFAULT 1,
  state_source TEXT NOT NULL DEFAULT 'runtime',
  warning_active INTEGER NOT NULL DEFAULT 0,
  purge_active INTEGER NOT NULL DEFAULT 0,
  production_label TEXT NOT NULL DEFAULT '',
  last_label_no INTEGER NOT NULL DEFAULT 0,
  info_json TEXT NOT NULL DEFAULT '{}',
  updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS machine_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_machine_events_ts ON machine_events(ts);
CREATE INDEX IF NOT EXISTS idx_machine_events_type_ts ON machine_events(event_type, ts);

CREATE TABLE IF NOT EXISTS label_register (
  production_label TEXT NOT NULL,
  label_no INTEGER NOT NULL,
  created_ts REAL NOT NULL,
  completed_ts REAL,
  zero_mm REAL,
  exit_mm REAL,
  material_ok INTEGER NOT NULL DEFAULT 1,
  print_ok INTEGER NOT NULL DEFAULT 1,
  verify_ok INTEGER NOT NULL DEFAULT 1,
  removed INTEGER NOT NULL DEFAULT 0,
  production_ok INTEGER NOT NULL DEFAULT 1,
  emitted_to_microtom INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (production_label, label_no)
);
CREATE INDEX IF NOT EXISTS idx_label_register_created ON label_register(created_ts);
CREATE INDEX IF NOT EXISTS idx_label_register_emit ON label_register(emitted_to_microtom, completed_ts);

CREATE TABLE IF NOT EXISTS label_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  production_label TEXT NOT NULL DEFAULT '',
  label_no INTEGER NOT NULL DEFAULT 0,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_label_events_ts ON label_events(ts);
CREATE INDEX IF NOT EXISTS idx_label_events_label ON label_events(production_label, label_no, ts);
"""

_init_lock = threading.Lock()
_initialized_paths = set()


class DB:
    def __init__(self, path: str):
        self.path = path
        self._init_once()

    @contextmanager
    def _conn(self):
        # isolation_level=None => autocommit
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA busy_timeout=5000;")
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA synchronous=NORMAL;")
            yield c
        finally:
            c.close()

    def _init_once(self):
        global _initialized_paths
        with _init_lock:
            if self.path in _initialized_paths:
                return

            # Retry a few times if another thread/process is initializing
            for i in range(10):
                try:
                    with self._conn() as c:
                        c.executescript(SCHEMA)
                        _apply_migrations(c)
                    _initialized_paths.add(self.path)
                    return
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        time.sleep(0.2 * (i + 1))
                        continue
                    raise


def now_ts() -> float:
    return time.time()


def _apply_migrations(conn: sqlite3.Connection):
    outbox_cols = {row[1] for row in conn.execute("PRAGMA table_info(outbox)").fetchall()}
    if "priority" not in outbox_cols:
        conn.execute("ALTER TABLE outbox ADD COLUMN priority INTEGER NOT NULL DEFAULT 100")
    if "dedupe_key" not in outbox_cols:
        conn.execute("ALTER TABLE outbox ADD COLUMN dedupe_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_sched ON outbox(next_attempt_ts, priority, retry_count, created_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_dedupe ON outbox(method, url, dedupe_key)"
    )
    param_cols = {row[1] for row in conn.execute("PRAGMA table_info(params)").fetchall()}
    if "esp_rw" not in param_cols:
        conn.execute("ALTER TABLE params ADD COLUMN esp_rw TEXT")
    if "ai_instructions" not in param_cols:
        conn.execute("ALTER TABLE params ADD COLUMN ai_instructions TEXT")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(param_device_map)").fetchall()}
    if "zbc_mapping" not in cols:
        conn.execute("ALTER TABLE param_device_map ADD COLUMN zbc_mapping TEXT")
    io_point_cols = {row[1] for row in conn.execute("PRAGMA table_info(io_points)").fetchall()}
    if io_point_cols and "device_label" not in io_point_cols:
        conn.execute("ALTER TABLE io_points ADD COLUMN device_label TEXT NOT NULL DEFAULT ''")
    if io_point_cols and "sheet_name" not in io_point_cols:
        conn.execute("ALTER TABLE io_points ADD COLUMN sheet_name TEXT NOT NULL DEFAULT ''")
    if io_point_cols and "zone_label" not in io_point_cols:
        conn.execute("ALTER TABLE io_points ADD COLUMN zone_label TEXT")
    if io_point_cols and "channel_no" not in io_point_cols:
        conn.execute("ALTER TABLE io_points ADD COLUMN channel_no INTEGER")
    if io_point_cols and "source_row" not in io_point_cols:
        conn.execute("ALTER TABLE io_points ADD COLUMN source_row INTEGER NOT NULL DEFAULT 0")
    io_value_cols = {row[1] for row in conn.execute("PRAGMA table_info(io_values)").fetchall()}
    if io_value_cols and "source" not in io_value_cols:
        conn.execute("ALTER TABLE io_values ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    machine_state_cols = {row[1] for row in conn.execute("PRAGMA table_info(machine_state)").fetchall()}
    if machine_state_cols and "production_label" not in machine_state_cols:
        conn.execute("ALTER TABLE machine_state ADD COLUMN production_label TEXT NOT NULL DEFAULT ''")
    if machine_state_cols and "last_label_no" not in machine_state_cols:
        conn.execute("ALTER TABLE machine_state ADD COLUMN last_label_no INTEGER NOT NULL DEFAULT 0")
