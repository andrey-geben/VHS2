"""
Модуль управления доступом (АСКУД).

AccessManager:
- проверяет номер по базе access_plates;
- при нахождении в белом списке — дёргает сетевое реле (HTTP);
- при чёрном списке — фиксирует тревогу;
- ведёт журнал всех срабатываний в access_log.

Логика работы:
    1. Analytics распознал номер → POST /api/access/check_plate
    2. Core нормализует номер
    3. Ищет в access_plates
    4. Действие:
         whitelist  → триггер реле → action="open"
         blacklist  → тревога      → action="alarm"
         regular    → отказ        → action="deny"
         нет в базе → неизвестный  → action="unknown"
    5. Записывает в access_log
    6. Возвращает результат
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)


class AccessManager:
    """Управление доступом: номера + реле + журнал."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        # Таймеры авто-закрытия реле: {device_id: threading.Timer}
        self._close_timers: Dict[int, threading.Timer] = {}

    # ------------------------------------------------------------------ #
    #  Проверка номера
    # ------------------------------------------------------------------ #
    def check_plate(
        self,
        plate: str,
        camera_id: Optional[str] = None,
        event_id: Optional[int] = None,
        snapshot_url: Optional[str] = None,
        device_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Проверяет номер и выполняет действие.

        Возвращает:
        {
            "action":  "open" | "deny" | "alarm" | "unknown" | "error",
            "plate":   "А123ВС77",
            "device_id":   1,                  # если сработало реле
            "device_name": "Шлагбаум",         # если сработало реле
            "message": "текст",
            "log_id":  123,                    # ID записи в журнале
        }
        """
        plate_norm = self._normalize_plate(plate)
        if not plate_norm:
            return self._log_and_return(
                plate=plate, camera_id=camera_id, action="error",
                event_id=event_id, snapshot_url=snapshot_url,
                details="Пустой номер",
                message="Пустой номер",
            )

        logger.info(
            "АСКУД: проверка номера '%s' (камера=%s)",
            plate_norm, camera_id,
        )

        # Ищем в базе
        entry = self.db.find_access_plate_by_number(plate_norm)

        if not entry:
            # Нет в базе — неизвестный
            return self._log_and_return(
                plate=plate_norm, camera_id=camera_id, action="unknown",
                event_id=event_id, snapshot_url=snapshot_url,
                details="Номер не найден в базе",
                message="Номер не найден в базе",
            )

        if not entry.get("enabled"):
            # Номер отключён
            return self._log_and_return(
                plate=plate_norm, camera_id=camera_id, action="deny",
                event_id=event_id, snapshot_url=snapshot_url,
                details=f"Номер отключён (id={entry['id']})",
                message="Номер отключён",
            )

        list_type = entry.get("list_type", "white")

        # --- Чёрный список: тревога ---
        if list_type == "black":
            return self._log_and_return(
                plate=plate_norm, camera_id=camera_id, action="alarm",
                event_id=event_id, snapshot_url=snapshot_url,
                details=f"Чёрный список (id={entry['id']}, "
                        f"owner={entry.get('owner')})",
                message="⚠️ Номер из чёрного списка",
            )

        # --- Regular: отказ без тревоги ---
        if list_type == "regular":
            return self._log_and_return(
                plate=plate_norm, camera_id=camera_id, action="deny",
                event_id=event_id, snapshot_url=snapshot_url,
                details=f"Regular-список (id={entry['id']})",
                message="Доступ не разрешён",
            )

        # --- Белый список: открываем реле ---
        if list_type == "white":
            # Выбираем устройство:
            # - если передан device_id — используем его;
            # - иначе — первое включённое устройство типа http_relay.
            dev = None
            if device_id:
                dev = self.db.get_access_device(device_id)
            if not dev:
                devices = self.db.list_access_devices(enabled_only=True)
                if devices:
                    dev = devices[0]

            if not dev:
                return self._log_and_return(
                    plate=plate_norm, camera_id=camera_id, action="error",
                    event_id=event_id, snapshot_url=snapshot_url,
                    details="Белый список, но устройств не настроено",
                    message="Устройство не настроено",
                )

            # Триггерим реле
            ok, err = self.trigger_device(dev["id"])

            if ok:
                return self._log_and_return(
                    plate=plate_norm, camera_id=camera_id, action="open",
                    device_id=dev["id"], device_name=dev["name"],
                    event_id=event_id, snapshot_url=snapshot_url,
                    details=f"Открыто устройство '{dev['name']}'",
                    message=f"Открыто: {dev['name']}",
                )
            else:
                return self._log_and_return(
                    plate=plate_norm, camera_id=camera_id, action="error",
                    device_id=dev["id"], device_name=dev["name"],
                    event_id=event_id, snapshot_url=snapshot_url,
                    details=f"Ошибка устройства '{dev['name']}': {err}",
                    message=f"Ошибка устройства: {err}",
                )

        # На всякий случай
        return self._log_and_return(
            plate=plate_norm, camera_id=camera_id, action="unknown",
            event_id=event_id, snapshot_url=snapshot_url,
            details=f"Неизвестный list_type: {list_type}",
            message="Неизвестный тип списка",
        )

    # ------------------------------------------------------------------ #
    #  Триггер устройства
    # ------------------------------------------------------------------ #
    def trigger_device(self, device_id: int,
                       duration_sec: Optional[int] = None) -> tuple:
        """
        Дёргает реле. Возвращает (ok: bool, error_message: str|None).

        Выполняет HTTP-запрос url_open. Если указан url_close,
        автоматически шлёт его через duration_sec секунд.
        """
        dev = self.db.get_access_device(device_id)
        if not dev:
            return False, "Устройство не найдено"

        if not dev.get("enabled"):
            return False, "Устройство отключено"

        # Готовим запрос
        url = dev["url_open"]
        method = (dev.get("method") or "GET").upper()
        headers = {}
        if dev.get("headers_json"):
            try:
                headers = json.loads(dev["headers_json"])
            except (ValueError, TypeError):
                pass

        auth = None
        if dev.get("username"):
            auth = (dev["username"], dev.get("password") or "")

        # Таймаут
        timeout = 5.0

        try:
            if method == "POST":
                r = requests.post(url, headers=headers, auth=auth,
                                  timeout=timeout)
            else:
                r = requests.get(url, headers=headers, auth=auth,
                                 timeout=timeout)
            ok = 200 <= r.status_code < 400
            if not ok:
                logger.warning(
                    "АСКУД: устройство '%s' вернуло HTTP %s",
                    dev["name"], r.status_code,
                )
                return False, f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            logger.warning(
                "АСКУД: ошибка запроса к устройству '%s': %s",
                dev["name"], e,
            )
            return False, str(e)
        except Exception as e:
            logger.exception("АСКУД: неожиданная ошибка: %s", e)
            return False, str(e)

        logger.info(
            "АСКУД: устройство '%s' сработало (открыто)",
            dev["name"],
        )

        # Планируем закрытие, если указан url_close
        close_url = dev.get("url_close")
        if close_url:
            delay = duration_sec or int(dev.get("open_duration_sec", 5))
            self._schedule_close(device_id, close_url, method, headers, auth,
                                 delay)

        return True, None

    def _schedule_close(self, device_id: int, url: str, method: str,
                        headers: dict, auth, delay: int):
        """Отложенное закрытие реле через delay секунд."""
        # Отменяем предыдущий таймер для этого устройства
        with self._lock:
            existing = self._close_timers.get(device_id)
            if existing and existing.is_alive():
                existing.cancel()

            def _do_close():
                try:
                    if method == "POST":
                        requests.post(url, headers=headers, auth=auth,
                                      timeout=5.0)
                    else:
                        requests.get(url, headers=headers, auth=auth,
                                     timeout=5.0)
                    logger.info(
                        "АСКУД: устройство #%s автоматически закрыто",
                        device_id,
                    )
                except Exception as e:
                    logger.warning(
                        "АСКУД: не удалось закрыть устройство #%s: %s",
                        device_id, e,
                    )

            timer = threading.Timer(delay, _do_close)
            timer.daemon = True
            timer.start()
            self._close_timers[device_id] = timer

    # ------------------------------------------------------------------ #
    #  Вспомогательные
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_plate(text: str) -> str:
        """Верхний регистр, только буквы и цифры."""
        import re
        return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())

    def _log_and_return(
        self,
        plate: str, action: str,
        camera_id: Optional[str] = None,
        device_id: Optional[int] = None,
        device_name: Optional[str] = None,
        event_id: Optional[int] = None,
        snapshot_url: Optional[str] = None,
        details: Optional[str] = None,
        message: str = "",
    ) -> Dict[str, Any]:
        """Записывает в журнал и возвращает результат."""
        try:
            log_id = self.db.add_access_log(
                plate=plate,
                camera_id=camera_id,
                action=action,
                device_id=device_id,
                device_name=device_name,
                event_id=event_id,
                snapshot_url=snapshot_url,
                details=details,
            )
        except Exception as e:
            logger.exception("АСКУД: ошибка записи в журнал: %s", e)
            log_id = None

        return {
            "action": action,
            "plate": plate,
            "device_id": device_id,
            "device_name": device_name,
            "message": message,
            "log_id": log_id,
        }

    # ------------------------------------------------------------------ #
    #  Ручной триггер (для теста из UI)
    # ------------------------------------------------------------------ #
    def manual_trigger(self, device_id: int) -> Dict[str, Any]:
        """Ручной запуск устройства (для кнопки «Тест» в UI)."""
        ok, err = self.trigger_device(device_id)
        return {
            "ok": ok,
            "error": err,
        }

    def close_all_timers(self):
        """Отменяет все активные таймеры закрытия реле."""
        with self._lock:
            for timer in self._close_timers.values():
                if timer.is_alive():
                    timer.cancel()
            self._close_timers.clear()