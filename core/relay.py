"""
Управление сетевым реле для шлагбаума.

Схема работы:
  Analytics распознал номер
    → Core POST /api/marker/create (marker_type='plate', поле plate)
      → RelayService.handle_plate(camera_id, plate)
        → проверка чёрного/белого списка в БД
        → проверка кулдауна (не открывать чаще, чем раз в N секунд)
        → RelayDriver.pulse() — HTTP GET открыть → пауза → закрыть
        → запись в relay_log
        → уведомление в трей при ошибке

Настройки реле хранятся в таблице relay_settings (key-value)
и читаются из БД при каждом вызове, чтобы изменения из UI
применялись без перезапуска Core.

Dummy-режим (dummy_mode=1) — не дёргает HTTP, только пишет в лог.
Удобно для отладки без физического реле.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Низкоуровневый драйвер — HTTP GET по шаблону URL
# ---------------------------------------------------------------------- #
class RelayDriver:
    """
    Дёргает реле по шаблону URL.

    Шаблон содержит плейсхолдеры:
      {host}    — IP реле
      {port}    — порт
      {channel} — номер канала под шлагбаум
      {state}   — '1' (вкл) / '0' (выкл)

    Пример для CVGAUDIO:
      http://{host}:{port}/cgi-bin/v_logic.cgi?cmd=relay,{channel},{state}

    Пример для DRM88ERv3:
      http://{host}:{port}/setcoil.json?A={channel}&B={state}
    """

    def __init__(self, settings: Dict[str, str]):
        self.settings = settings

    def pulse(self) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Импульс: открыть → пауза pulse_ms → закрыть.

        Возвращает (ok, http_code, error).
        При dummy_mode — не дёргает HTTP, но возвращает (True, 200, None).
        """
        ok, code, err = self._do_request(state="1")
        if not ok:
            return ok, code, err

        try:
            pulse_ms = int(self.settings.get("pulse_ms", "0"))
        except (TypeError, ValueError):
            pulse_ms = 0

        if pulse_ms > 0:
            time.sleep(pulse_ms / 1000.0)
            # Закрываем. Если не получилось — не считаем это ошибкой
            # открытия: шлагбаум уже открылся.
            close_ok, close_code, close_err = self._do_request(state="0")
            if not close_ok:
                logger.warning(
                    "Реле: не удалось закрыть канал (%s): %s",
                    close_code, close_err,
                )

        return True, code, None

    def test(self) -> Tuple[bool, Optional[int], Optional[str]]:
        """
        Тестовый одиночный импульс — используется кнопкой «Проверить»
        в UI. Отличие от pulse(): ничего не логирует в relay_log,
        всегда реально шлёт HTTP (даже в dummy_mode — пишет в лог).
        """
        return self.pulse()

    # ------------------------------------------------------------------ #
    #  Внутреннее
    # ------------------------------------------------------------------ #
    def _build_url(self, state: str) -> str:
        template = self.settings.get("url_template", "")
        if not template:
            raise ValueError("url_template не задан в настройках реле")

        url = template.format(
            host=self.settings.get("host", ""),
            port=self.settings.get("port", "80"),
            channel=self.settings.get("channel", "1"),
            state=state,
        )
        return url

    def _do_request(
        self, state: str
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        try:
            url = self._build_url(state)
        except Exception as e:
            return False, None, f"Ошибка шаблона URL: {e}"

        dummy = str(self.settings.get("dummy_mode", "0")) == "1"
        if dummy:
            logger.info("[DUMMY] Реле: GET %s", url)
            return True, 200, None

        try:
            timeout_ms = int(self.settings.get("timeout_ms", "2000"))
        except (TypeError, ValueError):
            timeout_ms = 2000
        timeout = max(0.1, timeout_ms / 1000.0)

        try:
            retries = int(self.settings.get("retries", "3"))
        except (TypeError, ValueError):
            retries = 3
        retries = max(1, min(retries, 10))

        try:
            delay_ms = int(self.settings.get("retry_delay_ms", "500"))
        except (TypeError, ValueError):
            delay_ms = 500
        delay = max(0.0, delay_ms / 1000.0)

        last_error: Optional[str] = None
        last_code: Optional[int] = None

        for attempt in range(1, retries + 1):
            try:
                r = requests.get(url, timeout=timeout, allow_redirects=False)
                last_code = r.status_code
                if 200 <= r.status_code < 300:
                    logger.info(
                        "Реле: GET %s → %s (попытка %d)",
                        url, r.status_code, attempt,
                    )
                    return True, r.status_code, None
                last_error = f"HTTP {r.status_code}"
                logger.warning(
                    "Реле: GET %s → %s (попытка %d/%d)",
                    url, r.status_code, attempt, retries,
                )
            except requests.exceptions.Timeout:
                last_error = "таймаут"
                logger.warning(
                    "Реле: таймаут GET %s (попытка %d/%d)",
                    url, attempt, retries,
                )
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                logger.warning(
                    "Реле: ошибка GET %s — %s (попытка %d/%d)",
                    url, e, attempt, retries,
                )

            if attempt < retries and delay > 0:
                time.sleep(delay)

        return False, last_code, last_error or "неизвестная ошибка"


# ---------------------------------------------------------------------- #
#  Высокоуровневый сервис — решения по номеру, лог, кулдаун
# ---------------------------------------------------------------------- #
class RelayService:
    """
    Обрабатывает распознанный номер: решает, открывать ли шлагбаум,
    дёргает драйвер, пишет журнал, уведомляет при ошибке.
    """

    def __init__(
        self,
        db,
        notify: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.db = db
        self.notify = notify

        # Кулдаун — per-plate. Хранит {plate_norm: last_open_ts}
        self._cooldown: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Публичное API
    # ------------------------------------------------------------------ #
    def handle_plate(self, camera_id: str, plate: str) -> Dict[str, Any]:
        """
        Основная точка входа. Возвращает dict:
          {
            "result": "opened" | "denied" | "error" | "skipped",
            "reason": "...",
            "error":  "..." (опционально),
          }
        """
        from database import _normalize_plate  # локальный импорт — избежать циклов

        settings = self.db.get_relay_settings()

        # 1. Реле включено?
        if str(settings.get("enabled", "0")) != "1":
            reason = "relay_disabled"
            self._log(camera_id, plate, settings, "skipped", reason)
            return {"result": "skipped", "reason": reason}

        # 2. Номер валиден?
        plate = (plate or "").strip()
        plate_norm = _normalize_plate(plate)
        if not plate_norm:
            reason = "empty_plate"
            self._log(camera_id, plate, settings, "skipped", reason)
            return {"result": "skipped", "reason": reason}

        # 3. Чёрный список — блокирует всегда
        if self.db.find_plate_rule(plate_norm, "black"):
            reason = "blacklist"
            self._log(camera_id, plate, settings, "denied", reason)
            logger.info(
                "Реле: отказ по чёрному списку — %s (камера %s)",
                plate, camera_id,
            )
            return {"result": "denied", "reason": reason}

        # 4. Решение по режиму
        open_mode = str(settings.get("open_mode", "whitelist"))
        white_rule = self.db.find_plate_rule(plate_norm, "white")

        if open_mode == "any":
            should_open, reason = True, "any_mode"
        elif open_mode == "whitelist":
            if white_rule:
                should_open, reason = True, "whitelist"
            else:
                should_open, reason = False, "not_in_whitelist"
        elif open_mode == "blacklist_blocks":
            should_open, reason = True, "no_blacklist_match"
        else:
            should_open, reason = False, "unknown_open_mode"

        if not should_open:
            self._log(camera_id, plate, settings, "denied", reason)
            logger.info(
                "Реле: отказ — %s (%s, камера %s)",
                plate, reason, camera_id,
            )
            return {"result": "denied", "reason": reason}

        # 5. Кулдаун (per-plate)
        cooldown_sec = self._int(settings.get("cooldown_sec", "10"), 10)
        if self._in_cooldown(plate_norm, cooldown_sec):
            self._log(camera_id, plate, settings, "skipped", "cooldown")
            logger.info(
                "Реле: пропуск — %s (кулдаун %d с, камера %s)",
                plate, cooldown_sec, camera_id,
            )
            return {"result": "skipped", "reason": "cooldown"}

        # 6. Дёргаем реле
        driver = RelayDriver(settings)
        ok, http_code, err = driver.pulse()

        if ok:
            with self._lock:
                self._cooldown[plate_norm] = time.time()
            self._log(
                camera_id, plate, settings, "opened", reason,
                http_code=http_code,
            )
            logger.info(
                "Реле: ОТКРЫТО по номеру %s (%s, камера %s)",
                plate, reason, camera_id,
            )
            return {"result": "opened", "reason": reason}

        # Ошибка
        self._log(
            camera_id, plate, settings, "error", reason,
            http_code=http_code, error=err,
        )
        logger.error(
            "Реле: ОШИБКА по номеру %s (%s, камера %s): %s",
            plate, reason, camera_id, err,
        )
        if str(settings.get("notify_on_error", "1")) == "1":
            self._notify_error(plate, camera_id, err)
        return {"result": "error", "reason": reason, "error": err}

    def test(self) -> Dict[str, Any]:
        """
        Тестовый импульс — кнопка «Проверить реле» в UI.
        Не пишет в relay_log, не проверяет списки и кулдаун.
        """
        settings = self.db.get_relay_settings()
        driver = RelayDriver(settings)
        ok, http_code, err = driver.test()
        return {
            "ok": ok,
            "http_code": http_code,
            "error": err,
            "url_template": settings.get("url_template", ""),
            "dummy_mode": str(settings.get("dummy_mode", "0")) == "1",
        }

    def clear_cooldown(self):
        """Сбросить кулдаун (пригодится в отладке)."""
        with self._lock:
            self._cooldown.clear()

    # ------------------------------------------------------------------ #
    #  Внутреннее
    # ------------------------------------------------------------------ #
    def _int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _in_cooldown(self, plate_norm: str, cooldown_sec: int) -> bool:
        if cooldown_sec <= 0:
            return False
        now = time.time()
        with self._lock:
            last = self._cooldown.get(plate_norm)
            if last is not None and (now - last) < cooldown_sec:
                return True
        return False

    def _log(
        self,
        camera_id: str,
        plate: str,
        settings: Dict[str, str],
        result: str,
        reason: str,
        http_code: Optional[int] = None,
        error: Optional[str] = None,
    ):
        try:
            channel = self._int(settings.get("channel", "0"), 0) or None
        except Exception:
            channel = None
        try:
            self.db.add_relay_log(
                result=result,
                camera_id=camera_id,
                plate=plate or None,
                channel=channel,
                reason=reason,
                http_code=http_code,
                error=error,
            )
        except Exception as e:
            logger.exception("Не удалось записать relay_log: %s", e)

    def _notify_error(self, plate: str, camera_id: str, error: Optional[str]):
        if not self.notify:
            return
        try:
            self.notify(
                "Шлагбаум",
                f"Реле недоступно. Номер {plate}, камера {camera_id}: "
                f"{error or 'неизвестная ошибка'}",
                "error",
            )
        except Exception as e:
            logger.debug("Ошибка notify callback: %s", e)