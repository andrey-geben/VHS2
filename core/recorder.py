"""
Модуль работы с архивом записей VHS Core.

ArchiveManager:
- выборки записей из БД;
- группировка по дням для календаря;
- обслуживание диска (очистка, удаление старых записей);
- безопасная отдача файлов с проверкой пути.

Поддерживает несколько папок записей:
- текущая (recordings_dir);
- история (extra_dirs) — старые папки после смены пути.

Это позволяет после смены папки хранить доступ к старым записям,
пока пользователь сам не удалит их с диска.
"""

import logging
import shutil
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class ArchiveManager:
    """Обёртка над таблицей recordings для удобной работы из UI/API."""

    def __init__(self, db, recordings_dir: Path,
                 extra_dirs: Optional[List[Path]] = None):
        """
        db — Database
        recordings_dir — текущая папка (абсолютная)
        extra_dirs — список дополнительных папок (история)
        """
        self.db = db
        self.recordings_dir = Path(recordings_dir).resolve()
        self.extra_dirs: List[Path] = [
            Path(p).resolve() for p in (extra_dirs or [])
        ]

    # ------------------------------------------------------------------ #
    #  Обновление путей
    # ------------------------------------------------------------------ #
    def update_dir(self, recordings_dir: Path,
                   extra_dirs: Optional[List[Path]] = None):
        """
        Обновляет текущую папку записей и список дополнительных.

        Вызывается при смене папки через UI, чтобы ArchiveManager
        знал, где искать новые и старые записи.
        """
        self.recordings_dir = Path(recordings_dir).resolve()
        if extra_dirs is not None:
            self.extra_dirs = [Path(p).resolve() for p in extra_dirs]
        logger.info(
            "ArchiveManager: recordings_dir=%s, extra_dirs=%d",
            self.recordings_dir, len(self.extra_dirs),
        )

    def _all_dirs(self) -> List[Path]:
        """Возвращает уникальный список всех разрешённых папок."""
        dirs = [self.recordings_dir] + self.extra_dirs
        seen = set()
        result = []
        for d in dirs:
            if d not in seen:
                seen.add(d)
                result.append(d)
        return result

    # ------------------------------------------------------------------ #
    #  Выборки
    # ------------------------------------------------------------------ #
    def list_recordings(
        self,
        camera_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Возвращает список записей с полями, готовыми для JSON."""
        rows = self.db.get_recordings(
            camera_id=camera_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        return [self._enrich(row) for row in rows]

    def get_recording(self, recording_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.get_recording(recording_id)
        return self._enrich(row) if row else None

    def _enrich(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Добавляет удобные поля: имя файла, размер в МБ, длительность."""
        filepath = Path(row["filepath"])
        row["filename"] = filepath.name
        row["exists"] = filepath.exists()
        if row.get("filesize"):
            row["filesize_mb"] = round(row["filesize"] / 1024 / 1024, 2)
        else:
            row["filesize_mb"] = 0.0
        # Длительность в формате mm:ss
        d = row.get("duration") or 0
        row["duration_str"] = f"{d // 60:02d}:{d % 60:02d}"
        return row

    # ------------------------------------------------------------------ #
    #  Группировка для календаря
    # ------------------------------------------------------------------ #
    def get_dates_summary(
        self, camera_id: Optional[str] = None, days_back: int = 30
    ) -> Dict[str, int]:
        """
        Возвращает { 'YYYY-MM-DD': количество_записей } за последние N дней.

        Используется для подсветки дат в календаре архива.
        """
        date_from = datetime.now() - timedelta(days=days_back)
        rows = self.db.get_recordings(
            camera_id=camera_id, date_from=date_from, limit=10000
        )
        summary: Dict[str, int] = {}
        for r in rows:
            day = r["start_time"][:10]
            summary[day] = summary.get(day, 0) + 1
        return summary

    def get_recordings_for_day(
        self, day: date, camera_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Все записи за конкретный день."""
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        return self.list_recordings(
            camera_id=camera_id, date_from=start, date_to=end, limit=2000
        )

    # ------------------------------------------------------------------ #
    #  Обслуживание диска
    # ------------------------------------------------------------------ #
    def cleanup_missing(self) -> int:
        """Удаляет из БД записи, файлы которых отсутствуют на диске."""
        removed = self.db.cleanup_missing_files()
        if removed:
            logger.info(
                "Удалено записей с отсутствующими файлами: %s", removed
            )
        return removed

    def purge_older_than(self, days: int) -> int:
        """
        Удаляет файлы и записи старше N дней.

        Возвращает количество удалённых записей.
        """
        cutoff = datetime.now() - timedelta(days=days)
        rows = self.db.get_recordings(date_to=cutoff, limit=100000)
        count = 0
        for r in rows:
            if self.db.delete_recording(r["id"]):
                count += 1
        if count:
            logger.info("Удалено записей старше %s дней: %s", days, count)
        return count

    def get_disk_usage(self) -> Dict[str, Any]:
        """
        Возвращает статистику по текущей папке записей:
        - размер архива,
        - общий размер диска,
        - свободное место.
        """
        total_bytes = 0
        if self.recordings_dir.exists():
            for cam_dir in self.recordings_dir.glob("*"):
                if cam_dir.is_dir():
                    for f in cam_dir.glob("*.avi"):
                        try:
                            total_bytes += f.stat().st_size
                        except OSError:
                            pass

        try:
            usage = shutil.disk_usage(self.recordings_dir)
            disk_total_gb = round(usage.total / 1024 / 1024 / 1024, 2)
            disk_free_gb = round(usage.free / 1024 / 1024 / 1024, 2)
            disk_used_percent = round(usage.used / usage.total * 100, 1)
        except Exception as e:
            logger.debug("Не удалось получить статистику диска: %s", e)
            disk_total_gb = 0
            disk_free_gb = 0
            disk_used_percent = 0

        return {
            "recordings_dir": str(self.recordings_dir),
            "archive_bytes": total_bytes,
            "archive_mb": round(total_bytes / 1024 / 1024, 2),
            "archive_gb": round(total_bytes / 1024 / 1024 / 1024, 2),
            "disk_total_gb": disk_total_gb,
            "disk_free_gb": disk_free_gb,
            "disk_used_percent": disk_used_percent,
        }

    # ------------------------------------------------------------------ #
    #  Безопасная отдача файлов
    # ------------------------------------------------------------------ #
    def resolve_file(self, recording_id: int) -> Optional[Path]:
        """
        Возвращает абсолютный путь к файлу записи.

        Проверяет, что путь находится внутри одной из разрешённых
        директорий (текущей или истории) — защита от directory traversal.

        Возвращает None, если:
        - запись не найдена в БД;
        - файл не существует;
        - путь вне разрешённых директорий.
        """
        rec = self.db.get_recording(recording_id)
        if not rec:
            return None

        path = Path(rec["filepath"]).resolve()
        if not path.exists():
            return None

        # Проверка безопасности: файл должен лежать в одной из
        # разрешённых директорий.
        for base in self._all_dirs():
            try:
                path.relative_to(base)
                return path
            except ValueError:
                continue

        logger.warning(
            "Попытка отдать файл вне разрешённых директорий: %s", path
        )
        return None