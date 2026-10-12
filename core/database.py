"""
Модуль работы с базой данных SQLite для VHS Core.

Таблицы:
- users            — пользователи системы
- recordings       — метаданные записанных файлов
- settings         — key-value настройки UI и системы
- markers          — маркеры событий
- access_plates    — номера в белом/чёрном списке АСКУД
- access_devices   — устройства АСКУД (сетевые реле и т.п.)
- access_log       — журнал срабатываний АСКУД
"""

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from werkzeug.security import generate_password_hash, check_password_hash


ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
VALID_ROLES = {ROLE_ADMIN, ROLE_VIEWER}

# Типы списков номеров
LIST_WHITE = "white"
LIST_BLACK = "black"
LIST_REGULAR = "regular"   # не открывать, но не тревога
VALID_LIST_TYPES = {LIST_WHITE, LIST_BLACK, LIST_REGULAR}


class Database:
    """Потокобезопасная обёртка над SQLite."""

    def __init__(self, db_path: str = "surveillance.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()
        self._migrate()

    # ------------------------------------------------------------------ #
    #  Низкоуровневые helpers
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, query: str, params: tuple = (), fetch: str = "none"):
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(query, params)
                conn.commit()
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return cur.lastrowid
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    #  Схема
    # ------------------------------------------------------------------ #
    def _init_schema(self):
        # --- Пользователи ---
        self._execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'viewer',
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # --- Записи ---
        self._execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id   TEXT     NOT NULL,
                filepath    TEXT     NOT NULL,
                start_time  DATETIME NOT NULL,
                end_time    DATETIME,
                duration    INTEGER,
                filesize    INTEGER,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_recordings_cam_time
            ON recordings (camera_id, start_time)
        """)
        # --- Настройки ---
        self._execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # --- Маркеры ---
        self._execute("""
            CREATE TABLE IF NOT EXISTS markers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id     TEXT    NOT NULL,
                marker_type   TEXT    NOT NULL DEFAULT 'event',
                description   TEXT,
                source        TEXT,
                external_id   TEXT,
                snapshot_url  TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_markers_cam_time
            ON markers (camera_id, created_at)
        """)

        # ================== АСКУД ================== #
        # Номера
        self._execute("""
            CREATE TABLE IF NOT EXISTS access_plates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                plate       TEXT    UNIQUE NOT NULL,
                owner       TEXT,
                description TEXT,
                list_type   TEXT    NOT NULL DEFAULT 'white',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_access_plates_type
            ON access_plates (list_type, enabled)
        """)

        # Устройства
        self._execute("""
            CREATE TABLE IF NOT EXISTS access_devices (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT    NOT NULL,
                type              TEXT    NOT NULL DEFAULT 'http_relay',
                url_open          TEXT    NOT NULL,
                url_close         TEXT,
                method            TEXT    NOT NULL DEFAULT 'GET',
                open_duration_sec INTEGER NOT NULL DEFAULT 5,
                username          TEXT,
                password          TEXT,
                headers_json      TEXT,
                enabled           INTEGER NOT NULL DEFAULT 1,
                created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Журнал
        self._execute("""
            CREATE TABLE IF NOT EXISTS access_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                plate         TEXT,
                camera_id     TEXT,
                action        TEXT    NOT NULL,
                device_id     INTEGER,
                device_name   TEXT,
                event_id      INTEGER,
                snapshot_url  TEXT,
                details       TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_access_log_time
            ON access_log (created_at)
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_access_log_plate
            ON access_log (plate, created_at)
        """)

    def _migrate(self):
        """Миграции для существующих БД."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(users)")
                cols = {row[1] for row in cur.fetchall()}
                if "role" not in cols:
                    cur.execute(
                        "ALTER TABLE users "
                        "ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"
                    )
                    conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------ #
    #  Пользователи
    # ------------------------------------------------------------------ #
    def create_user(self, username: str, password: str,
                    role: str = ROLE_VIEWER) -> int:
        if role not in VALID_ROLES:
            raise ValueError(f"Недопустимая роль: {role}")
        pwd_hash = generate_password_hash(password)
        return self._execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, pwd_hash, role),
        )

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM users WHERE username = ?", (username,), fetch="one"
        )
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM users WHERE id = ?", (user_id,), fetch="one"
        )
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT id, username, role, created_at FROM users ORDER BY username",
            fetch="all",
        )
        return [dict(r) for r in rows]

    def verify_password(self, username: str, password: str) -> bool:
        user = self.get_user(username)
        if not user:
            return False
        return check_password_hash(user["password_hash"], password)

    def user_exists(self, username: str) -> bool:
        return self.get_user(username) is not None

    def set_password(self, user_id: int, new_password: str):
        pwd_hash = generate_password_hash(new_password)
        self._execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (pwd_hash, user_id),
        )

    def set_role(self, user_id: int, role: str):
        if role not in VALID_ROLES:
            raise ValueError(f"Недопустимая роль: {role}")
        self._execute(
            "UPDATE users SET role = ? WHERE id = ?", (role, user_id)
        )

    def delete_user(self, user_id: int) -> bool:
        user = self.get_user_by_id(user_id)
        if not user:
            return False
        if user["role"] == ROLE_ADMIN:
            admins = self._execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = ?",
                (ROLE_ADMIN,),
                fetch="one",
            )
            if admins["c"] <= 1:
                raise ValueError("Нельзя удалить последнего администратора")
        self._execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True

    def ensure_default_admin(self, username: str, password: str):
        if not self.list_users():
            self.create_user(username, password, role=ROLE_ADMIN)

    # ------------------------------------------------------------------ #
    #  Настройки
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._execute(
            "SELECT value FROM settings WHERE key = ?", (key,), fetch="one"
        )
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self._execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def get_all_settings(self) -> Dict[str, str]:
        rows = self._execute("SELECT key, value FROM settings", fetch="all")
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------------ #
    #  Записи (архив)
    # ------------------------------------------------------------------ #
    def add_recording(self, camera_id, filepath, start_time,
                      end_time=None, duration=None, filesize=None) -> int:
        return self._execute(
            """
            INSERT INTO recordings
                (camera_id, filepath, start_time, end_time, duration, filesize)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                camera_id, filepath,
                start_time.isoformat(timespec="seconds"),
                end_time.isoformat(timespec="seconds") if end_time else None,
                duration, filesize,
            ),
        )

    def update_recording_end(self, recording_id, end_time, duration, filesize):
        self._execute(
            """
            UPDATE recordings
            SET end_time = ?, duration = ?, filesize = ?
            WHERE id = ?
            """,
            (end_time.isoformat(timespec="seconds"), duration, filesize, recording_id),
        )

    def get_recordings(self, camera_id=None, date_from=None, date_to=None,
                       limit=500) -> List[Dict[str, Any]]:
        query = "SELECT * FROM recordings WHERE 1=1"
        params: List[Any] = []
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if date_from:
            query += " AND start_time >= ?"
            params.append(date_from.isoformat(timespec="seconds"))
        if date_to:
            query += " AND start_time <= ?"
            params.append(date_to.isoformat(timespec="seconds"))
        query += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)
        rows = self._execute(query, tuple(params), fetch="all")
        return [dict(r) for r in rows]

    def get_recording(self, recording_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM recordings WHERE id = ?", (recording_id,), fetch="one"
        )
        return dict(row) if row else None

    def delete_recording(self, recording_id: int) -> bool:
        rec = self.get_recording(recording_id)
        if not rec:
            return False
        try:
            Path(rec["filepath"]).unlink(missing_ok=True)
        except OSError:
            pass
        self._execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        return True

    def cleanup_missing_files(self) -> int:
        rows = self._execute("SELECT id, filepath FROM recordings", fetch="all")
        missing = [r["id"] for r in rows if not Path(r["filepath"]).exists()]
        for rid in missing:
            self._execute("DELETE FROM recordings WHERE id = ?", (rid,))
        return len(missing)

    # ------------------------------------------------------------------ #
    #  Маркеры
    # ------------------------------------------------------------------ #
    def add_marker(self, camera_id, marker_type="event", description=None,
                   source=None, external_id=None, snapshot_url=None,
                   created_at=None) -> int:
        ts = (created_at or datetime.now()).isoformat(timespec="seconds")
        return self._execute(
            """
            INSERT INTO markers
                (camera_id, marker_type, description, source,
                 external_id, snapshot_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (camera_id, marker_type, description, source,
             external_id, snapshot_url, ts),
        )

    def get_markers(self, camera_id=None, date_from=None, date_to=None,
                    limit=500) -> List[Dict[str, Any]]:
        query = "SELECT * FROM markers WHERE 1=1"
        params: List[Any] = []
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if date_from:
            query += " AND created_at >= ?"
            params.append(date_from.isoformat(timespec="seconds"))
        if date_to:
            query += " AND created_at <= ?"
            params.append(date_to.isoformat(timespec="seconds"))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._execute(query, tuple(params), fetch="all")
        return [dict(r) for r in rows]

    def get_marker(self, marker_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM markers WHERE id = ?", (marker_id,), fetch="one"
        )
        return dict(row) if row else None

    def delete_marker(self, marker_id: int) -> bool:
        marker = self.get_marker(marker_id)
        if not marker:
            return False
        self._execute("DELETE FROM markers WHERE id = ?", (marker_id,))
        return True

    def purge_markers_older_than(self, days: int) -> int:
        cutoff = datetime.now() - timedelta(days=days)
        row = self._execute(
            "SELECT COUNT(*) AS c FROM markers WHERE created_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
            fetch="one",
        )
        count = row["c"] if row else 0
        self._execute(
            "DELETE FROM markers WHERE created_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )
        return count

    # ================================================================== #
    #  АСКУД: номера
    # ================================================================== #
    def add_access_plate(self, plate: str, owner: str = None,
                         description: str = None,
                         list_type: str = LIST_WHITE,
                         enabled: bool = True) -> int:
        if list_type not in VALID_LIST_TYPES:
            raise ValueError(f"Недопустимый тип списка: {list_type}")
        return self._execute(
            """
            INSERT INTO access_plates (plate, owner, description, list_type, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (plate.strip().upper(), owner, description, list_type,
             1 if enabled else 0),
        )

    def get_access_plate(self, plate_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM access_plates WHERE id = ?", (plate_id,), fetch="one"
        )
        return dict(row) if row else None

    def find_access_plate_by_number(self, plate: str) -> Optional[Dict[str, Any]]:
        """Ищет запись по нормализованному номеру."""
        if not plate:
            return None
        row = self._execute(
            "SELECT * FROM access_plates WHERE plate = ?",
            (plate.strip().upper(),),
            fetch="one",
        )
        return dict(row) if row else None

    def list_access_plates(self, list_type: str = None,
                           enabled_only: bool = False,
                           search: str = None,
                           limit: int = 500) -> List[Dict[str, Any]]:
        query = "SELECT * FROM access_plates WHERE 1=1"
        params: List[Any] = []
        if list_type:
            query += " AND list_type = ?"
            params.append(list_type)
        if enabled_only:
            query += " AND enabled = 1"
        if search:
            query += " AND (plate LIKE ? OR owner LIKE ? OR description LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])
        query += " ORDER BY plate LIMIT ?"
        params.append(limit)
        rows = self._execute(query, tuple(params), fetch="all")
        return [dict(r) for r in rows]

    def update_access_plate(self, plate_id: int, plate: str = None,
                            owner: str = None, description: str = None,
                            list_type: str = None, enabled: bool = None) -> bool:
        row = self.get_access_plate(plate_id)
        if not row:
            return False

        updates = []
        params: List[Any] = []
        if plate is not None:
            updates.append("plate = ?")
            params.append(plate.strip().upper())
        if owner is not None:
            updates.append("owner = ?")
            params.append(owner)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if list_type is not None:
            if list_type not in VALID_LIST_TYPES:
                raise ValueError(f"Недопустимый тип списка: {list_type}")
            updates.append("list_type = ?")
            params.append(list_type)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)

        if not updates:
            return True

        params.append(plate_id)
        self._execute(
            f"UPDATE access_plates SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        return True

    def delete_access_plate(self, plate_id: int) -> bool:
        if not self.get_access_plate(plate_id):
            return False
        self._execute("DELETE FROM access_plates WHERE id = ?", (plate_id,))
        return True

    # ================================================================== #
    #  АСКУД: устройства
    # ================================================================== #
    def add_access_device(self, name: str, url_open: str,
                          type: str = "http_relay",
                          url_close: str = None,
                          method: str = "GET",
                          open_duration_sec: int = 5,
                          username: str = None,
                          password: str = None,
                          headers_json: str = None,
                          enabled: bool = True) -> int:
        return self._execute(
            """
            INSERT INTO access_devices
                (name, type, url_open, url_close, method, open_duration_sec,
                 username, password, headers_json, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, type, url_open, url_close, method, open_duration_sec,
             username, password, headers_json, 1 if enabled else 0),
        )

    def get_access_device(self, device_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM access_devices WHERE id = ?", (device_id,), fetch="one"
        )
        return dict(row) if row else None

    def list_access_devices(self, enabled_only: bool = False
                            ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM access_devices"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name"
        rows = self._execute(query, fetch="all")
        return [dict(r) for r in rows]

    def update_access_device(self, device_id: int, **kwargs) -> bool:
        row = self.get_access_device(device_id)
        if not row:
            return False

        allowed = {"name", "type", "url_open", "url_close", "method",
                   "open_duration_sec", "username", "password",
                   "headers_json", "enabled"}
        updates = []
        params: List[Any] = []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "enabled":
                v = 1 if v else 0
            updates.append(f"{k} = ?")
            params.append(v)

        if not updates:
            return True

        params.append(device_id)
        self._execute(
            f"UPDATE access_devices SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        return True

    def delete_access_device(self, device_id: int) -> bool:
        if not self.get_access_device(device_id):
            return False
        self._execute("DELETE FROM access_devices WHERE id = ?", (device_id,))
        return True

    # ================================================================== #
    #  АСКУД: журнал
    # ================================================================== #
    def add_access_log(self, plate: str = None, camera_id: str = None,
                       action: str = "unknown", device_id: int = None,
                       device_name: str = None, event_id: int = None,
                       snapshot_url: str = None,
                       details: str = None) -> int:
        return self._execute(
            """
            INSERT INTO access_log
                (plate, camera_id, action, device_id, device_name,
                 event_id, snapshot_url, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plate, camera_id, action, device_id, device_name,
             event_id, snapshot_url, details),
        )

    def get_access_log(self, plate: str = None, camera_id: str = None,
                       action: str = None, date_from=None, date_to=None,
                       limit: int = 200, offset: int = 0
                       ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM access_log WHERE 1=1"
        params: List[Any] = []
        if plate:
            query += " AND plate LIKE ?"
            params.append(f"%{plate}%")
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if action:
            query += " AND action = ?"
            params.append(action)
        if date_from:
            query += " AND created_at >= ?"
            params.append(date_from.isoformat(timespec="seconds"))
        if date_to:
            query += " AND created_at <= ?"
            params.append(date_to.isoformat(timespec="seconds"))
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._execute(query, tuple(params), fetch="all")
        return [dict(r) for r in rows]

    def count_access_log(self, plate: str = None, camera_id: str = None,
                         action: str = None) -> int:
        query = "SELECT COUNT(*) AS c FROM access_log WHERE 1=1"
        params: List[Any] = []
        if plate:
            query += " AND plate LIKE ?"
            params.append(f"%{plate}%")
        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if action:
            query += " AND action = ?"
            params.append(action)
        row = self._execute(query, tuple(params), fetch="one")
        return row["c"] if row else 0

    def purge_access_log_older_than(self, days: int) -> int:
        cutoff = datetime.now() - timedelta(days=days)
        row = self._execute(
            "SELECT COUNT(*) AS c FROM access_log WHERE created_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
            fetch="one",
        )
        count = row["c"] if row else 0
        self._execute(
            "DELETE FROM access_log WHERE created_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )
        return count