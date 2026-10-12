"""
Модуль работы с базой данных SQLite для VHS-Analytics.

Таблицы:
- users     — пользователи системы (логин, хеш пароля, роль)
- events    — события детекции движения
- settings  — key-value настройки UI и системы

Роли:
- 'admin'  — полный доступ
- 'viewer' — только просмотр событий
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


class Database:
    """Потокобезопасная обёртка над SQLite."""

    def __init__(self, db_path: str = "analytics.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------ #
    #  Низкоуровневые helpers
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, query: str, params: tuple = (), fetch: str = "none"):
        """
        Выполняет запрос под блокировкой.
        fetch: 'none' | 'one' | 'all'
        """
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
        self._execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'viewer',
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id       TEXT    NOT NULL,
                camera_name     TEXT,
                zone_name       TEXT,
                event_type      TEXT    NOT NULL DEFAULT 'motion',
                confidence      REAL    DEFAULT 1.0,
                bbox_x          INTEGER,
                bbox_y          INTEGER,
                bbox_w          INTEGER,
                bbox_h          INTEGER,
                area            INTEGER,
                snapshot_path   TEXT,
                extra           TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_events_cam_time
            ON events (camera_id, created_at)
        """)
        self._execute("""
            CREATE INDEX IF NOT EXISTS idx_events_type_time
            ON events (event_type, created_at)
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

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
    #  Настройки (key-value)
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
    #  События
    # ------------------------------------------------------------------ #
    def add_event(
        self,
        camera_id: str,
        camera_name: Optional[str] = None,
        zone_name: Optional[str] = None,
        event_type: str = "motion",
        confidence: float = 1.0,
        bbox: Optional[tuple] = None,
        area: Optional[int] = None,
        snapshot_path: Optional[str] = None,
        extra: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> int:
        """
        Добавляет событие.

        bbox: (x, y, w, h) или None.
        created_at: если None — текущее время.
        """
        bx, by, bw, bh = (bbox if bbox else (None, None, None, None))
        ts = (created_at or datetime.now()).isoformat(timespec="seconds")

        return self._execute(
            """
            INSERT INTO events
                (camera_id, camera_name, zone_name, event_type, confidence,
                 bbox_x, bbox_y, bbox_w, bbox_h, area,
                 snapshot_path, extra, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                camera_id, camera_name, zone_name, event_type, confidence,
                bx, by, bw, bh, area,
                snapshot_path, extra, ts,
            ),
        )

    def get_events(
        self,
        camera_id: Optional[str] = None,
        event_type: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM events WHERE 1=1"
        params: List[Any] = []

        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
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

    def count_events(
        self,
        camera_id: Optional[str] = None,
        event_type: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        query = "SELECT COUNT(*) AS c FROM events WHERE 1=1"
        params: List[Any] = []

        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if date_from:
            query += " AND created_at >= ?"
            params.append(date_from.isoformat(timespec="seconds"))
        if date_to:
            query += " AND created_at <= ?"
            params.append(date_to.isoformat(timespec="seconds"))

        row = self._execute(query, tuple(params), fetch="one")
        return row["c"] if row else 0

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT * FROM events WHERE id = ?", (event_id,), fetch="one"
        )
        return dict(row) if row else None

    def delete_event(self, event_id: int) -> bool:
        """Удаляет событие и его снапшот с диска."""
        event = self.get_event(event_id)
        if not event:
            return False
        if event.get("snapshot_path"):
            try:
                Path(event["snapshot_path"]).unlink(missing_ok=True)
            except OSError:
                pass
        self._execute("DELETE FROM events WHERE id = ?", (event_id,))
        return True

    def purge_older_than(self, days: int) -> int:
        """Удаляет события и их снапшоты старше N дней. Возвращает счётчик."""
        cutoff = datetime.now() - timedelta(days=days)
        rows = self._execute(
            "SELECT id, snapshot_path FROM events WHERE created_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
            fetch="all",
        )
        count = 0
        for r in rows:
            if r["snapshot_path"]:
                try:
                    Path(r["snapshot_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            self._execute("DELETE FROM events WHERE id = ?", (r["id"],))
            count += 1
        return count

    # ------------------------------------------------------------------ #
    #  Статистика для UI
    # ------------------------------------------------------------------ #
    def get_events_summary(
        self,
        camera_id: Optional[str] = None,
        days_back: int = 30,
    ) -> Dict[str, int]:
        """Возвращает { 'YYYY-MM-DD': количество_событий } за N дней."""
        date_from = datetime.now() - timedelta(days=days_back)
        query = "SELECT created_at FROM events WHERE created_at >= ?"
        params: List[Any] = [date_from.isoformat(timespec="seconds")]

        if camera_id:
            query += " AND camera_id = ?"
            params.append(camera_id)

        rows = self._execute(query, tuple(params), fetch="all")
        summary: Dict[str, int] = {}
        for r in rows:
            day = r["created_at"][:10]
            summary[day] = summary.get(day, 0) + 1
        return summary

    def get_hourly_stats(
        self, days_back: int = 7
    ) -> Dict[str, List[int]]:
        """Распределение событий по часам суток за последние N дней."""
        date_from = datetime.now() - timedelta(days=days_back)
        rows = self._execute(
            "SELECT created_at FROM events WHERE created_at >= ?",
            (date_from.isoformat(timespec="seconds"),),
            fetch="all",
        )
        hourly = [0] * 24
        for r in rows:
            try:
                hour = int(r["created_at"][11:13])
                hourly[hour] += 1
            except (ValueError, IndexError):
                pass
        return {"hourly": hourly}