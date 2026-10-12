"""
Менеджер подпроцесса VHS-Analytics.

Запускает, останавливает, мониторит Analytics как дочерний процесс Core.
Связь через HTTP API.

Особенности:
- при завершении Core автоматически останавливает Analytics;
- автоперезапуск при падении с ограничением N раз в час;
- уведомления в трей через callback notify(title, message, level).

Автопоиск Analytics:
Если файл по указанному пути не найден, Core пробует найти его в
стандартных местах относительно папки Core.

Поддерживает запуск как .exe (продакшен), так и .py (разработка).
"""

import os
import logging
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import requests

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


class AnalyticsManager:
    """Управляет жизненным циклом VHS-Analytics как подпроцесса."""

    STATE_STOPPED = "stopped"
    STATE_STARTING = "starting"
    STATE_RUNNING = "running"
    STATE_STOPPING = "stopping"
    STATE_ERROR = "error"

    # Стандартные места, где Core ищет Analytics, если указанный путь
    # не существует. Пути относительны папки Core.
    SEARCH_PATHS = [
        # Продакшен: рядом с Core в соседней папке
        "../analytics/VHS-Analytics.exe",
        "../VHS-Analytics/VHS-Analytics.exe",
        # Сборка из исходников (dist)
        "../analytics/dist/VHS-Analytics/VHS-Analytics.exe",
        "../dist/VHS-Analytics/VHS-Analytics.exe",
        # Рядом с Core (в одной папке)
        "./VHS-Analytics.exe",
        "./analytics/VHS-Analytics.exe",
        # Разработка: .py
        "../analytics/analytics_app.py",
        "./analytics_app.py",
    ]

    def __init__(
        self,
        exe_path: str,
        base_dir: Path,
        enabled: bool = True,
        auto_start: bool = False,
        auto_restart: bool = True,
        max_restarts_per_hour: int = 3,
        url: str = "http://localhost:8100",
        notify: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.base_dir = Path(base_dir)
        self.enabled = bool(enabled)
        self.auto_start = bool(auto_start)
        self.auto_restart = bool(auto_restart)
        self.max_restarts_per_hour = max(1, int(max_restarts_per_hour))
        self.url = url.rstrip("/")
        self.notify = notify

        # Запомним «сырой» путь из конфига — для отображения в UI
        self._raw_exe_path = str(exe_path)

        # Резолвим путь с автопоиском
        self.exe_path: Path = self._resolve_path(exe_path)

        # Состояние
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._state: str = self.STATE_STOPPED
        self._last_error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._restart_times: List[float] = []

        # Мониторинг
        self._stop_monitor = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stopping = False

    # ------------------------------------------------------------------ #
    #  Разрешение пути с автопоиском
    # ------------------------------------------------------------------ #
    def _resolve_path(self, exe_path: str) -> Path:
        """
        Принимает путь из конфига. Если файл не найден — ищет в
        стандартных местах рядом с Core.
        """
        p = Path(exe_path)

        # Абсолютный — принимаем как есть
        if p.is_absolute():
            if p.exists():
                logger.info("Analytics: путь из конфига — %s", p)
                return p
            logger.warning(
                "Analytics: файл по абсолютному пути не найден: %s", p
            )
            return p

        # Относительный — от папки Core
        resolved = (self.base_dir / p).resolve()
        if resolved.exists():
            logger.info("Analytics: путь из конфига — %s", resolved)
            return resolved

        logger.warning(
            "Analytics: файл не найден по пути '%s' (%s). "
            "Ищу в стандартных местах...",
            exe_path, resolved,
        )

        # Автопоиск
        for rel in self.SEARCH_PATHS:
            cand = (self.base_dir / rel).resolve()
            if cand.exists():
                logger.info("Analytics найден автоматически: %s", cand)
                self._raw_exe_path = rel
                return cand

        logger.error(
            "Analytics: файл не найден нигде. Проверьте настройки."
        )
        return resolved

    def update_exe_path(self, new_path: str):
        """Обновляет путь к exe с автопоиском."""
        self._raw_exe_path = new_path
        self.exe_path = self._resolve_path(new_path)

    # ------------------------------------------------------------------ #
    #  Публичное API
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        with self._lock:
            if self._state in (self.STATE_RUNNING, self.STATE_STARTING):
                return True

            if not self.enabled:
                self._last_error = "Управление Analytics отключено"
                logger.info("Analytics не запущен: enabled=False")
                return False

            if not self.exe_path.exists():
                self._last_error = f"Файл не найден: {self.exe_path}"
                logger.error("Analytics: %s", self._last_error)
                self._notify("Analytics", self._last_error, "error")
                return False

            self._state = self.STATE_STARTING
            self._last_error = None

        if self._health_check(timeout=1.0):
            with self._lock:
                self._state = self.STATE_RUNNING
                self._started_at = time.time()
            logger.info("Analytics уже работает на %s", self.url)
            self._start_monitor()
            return True

        try:
            self._process = self._spawn()
        except Exception as e:
            with self._lock:
                self._state = self.STATE_ERROR
                self._last_error = f"Ошибка запуска: {e}"
            logger.exception("Не удалось запустить Analytics")
            self._notify("Analytics", self._last_error, "error")
            return False

        ready = self._wait_ready(timeout=60.0)
        with self._lock:
            if ready:
                self._state = self.STATE_RUNNING
                self._started_at = time.time()
                self._restart_times.append(time.time())
            else:
                self._state = self.STATE_ERROR
                if not self._last_error:
                    self._last_error = "Analytics не отвечает на health-check"

        if ready:
            pid = self._process.pid if self._process else "?"
            logger.info("Analytics запущен: PID=%s, %s", pid, self.url)
            self._notify("Analytics", "Модуль аналитики запущен", "info")
            self._start_monitor()
            return True
        else:
            logger.error("Analytics не запустился: %s", self._last_error)
            self._notify("Analytics", self._last_error or "Не запустился",
                         "error")
            self._kill_process()
            return False

    def stop(self) -> bool:
        with self._lock:
            if self._state == self.STATE_STOPPED:
                return True
            self._stopping = True
            self._state = self.STATE_STOPPING

        self._stop_monitor.set()

        try:
            requests.post(f"{self.url}/api/shutdown", timeout=3)
            logger.info("Analytics: отправлен HTTP shutdown")
        except Exception:
            pass

        if self._process:
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("Analytics не завершился по HTTP, kill")
                self._kill_process()

        with self._lock:
            self._process = None
            self._state = self.STATE_STOPPED
            self._started_at = None
            self._stopping = False

        logger.info("Analytics остановлен")
        return True

    def restart(self) -> bool:
        self._notify("Analytics", "Перезапуск...", "info")
        self.stop()
        time.sleep(1)
        return self.start()

    def is_running(self) -> bool:
        with self._lock:
            return self._state == self.STATE_RUNNING

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            uptime = int(now - self._started_at) if self._started_at else 0
            return {
                "state": self._state,
                "enabled": self.enabled,
                "auto_start": self.auto_start,
                "auto_restart": self.auto_restart,
                "exe_path": str(self.exe_path),
                "exe_path_raw": self._raw_exe_path,
                "exe_exists": self.exe_path.exists(),
                "url": self.url,
                "uptime_sec": uptime,
                "pid": self._process.pid if self._process else None,
                "last_error": self._last_error,
                "restarts_last_hour": self._restarts_last_hour(),
            }

    def update_settings(self, **kwargs):
        with self._lock:
            if "enabled" in kwargs:
                self.enabled = bool(kwargs["enabled"])
            if "auto_start" in kwargs:
                self.auto_start = bool(kwargs["auto_start"])
            if "auto_restart" in kwargs:
                self.auto_restart = bool(kwargs["auto_restart"])
            if "max_restarts_per_hour" in kwargs:
                self.max_restarts_per_hour = max(
                    1, int(kwargs["max_restarts_per_hour"])
                )
            if "url" in kwargs:
                self.url = str(kwargs["url"]).rstrip("/")
            if "exe_path" in kwargs:
                self.update_exe_path(kwargs["exe_path"])

    def shutdown(self):
        logger.info("AnalyticsManager: завершение работы")
        self.stop()

    # ------------------------------------------------------------------ #
    #  Внутренние
    # ------------------------------------------------------------------ #
    def _spawn(self) -> subprocess.Popen:
        env = os.environ.copy()

        if self.exe_path.suffix.lower() == ".exe":
            cmd = [str(self.exe_path)]
        else:
            cmd = [sys.executable, str(self.exe_path)]

        creationflags = 0
        if IS_WINDOWS:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        logger.info("Analytics: запускаю %s", cmd)
        return subprocess.Popen(
            cmd,
            cwd=str(self.exe_path.parent),
            env=env,
            creationflags=creationflags,
            stdout=None,
            stderr=None,
        )

    def _kill_process(self):
        if not self._process:
            return
        try:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        except Exception as e:
            logger.exception("Не удалось убить Analytics: %s", e)
        finally:
            self._process = None

    def _health_check(self, timeout: float = 2.0) -> bool:
        try:
            r = requests.get(f"{self.url}/api/ping", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def _wait_ready(self, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                with self._lock:
                    self._last_error = (
                        f"Процесс завершился с кодом "
                        f"{self._process.returncode}"
                    )
                return False
            if self._health_check(timeout=1.0):
                return True
            time.sleep(1.0)
        return False

    def _restarts_last_hour(self) -> int:
        cutoff = time.time() - 3600
        self._restart_times = [t for t in self._restart_times if t > cutoff]
        return len(self._restart_times)

    def _notify(self, title: str, message: str, level: str = "info"):
        if self.notify:
            try:
                self.notify(title, message, level)
            except Exception as e:
                logger.debug("Ошибка notify callback: %s", e)

    def _start_monitor(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="analytics-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self):
        logger.info("Analytics: мониторинг запущен")
        while not self._stop_monitor.wait(10.0):
            if self._stopping:
                break

            with self._lock:
                state = self._state
                proc = self._process
                enabled = self.enabled

            if not enabled or state != self.STATE_RUNNING:
                continue

            proc_dead = (proc is None) or (proc.poll() is not None)
            health = self._health_check(timeout=3.0)

            if proc_dead or not health:
                logger.warning(
                    "Analytics: сбой (proc_dead=%s, health=%s)",
                    proc_dead, health,
                )
                self._handle_crash()

    def _handle_crash(self):
        with self._lock:
            self._state = self.STATE_ERROR
            if not self._last_error:
                self._last_error = "Процесс не отвечает"
            restarts = self._restarts_last_hour()
            can_restart = (
                self.auto_restart
                and restarts < self.max_restarts_per_hour
            )

        self._kill_process()

        if can_restart:
            self._notify(
                "Analytics",
                f"Сбой. Перезапуск "
                f"({restarts + 1}/{self.max_restarts_per_hour})",
                "warning",
            )
            logger.info(
                "Analytics: попытка перезапуска %d/%d",
                restarts + 1, self.max_restarts_per_hour,
            )
            time.sleep(3)
            self.start()
        else:
            msg = (
                f"Analytics остановлен: превышен лимит перезапусков "
                f"({self.max_restarts_per_hour}/час) или автоперезапуск "
                f"отключён в настройках."
            )
            logger.error(msg)
            self._notify("Analytics", msg, "error")