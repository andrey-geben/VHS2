"""
HTTP-клиент к API VHS Core.

Назначение:
- создавать маркеры событий (POST /api/marker/create);
- проверять номер по базе АСКУД (POST /api/access/check_plate);
- управлять записью камеры (start/stop record);
- запускать/останавливать поток камеры;
- проверять доступность VHS Core (ping).

Аутентификация — заголовок X-Api-Token.
"""

import logging
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)


class VhsClient:
    """Тонкая обёртка над REST API VHS Core."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout: float = 5.0,
        enabled: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token or ""
        self.timeout = float(timeout)
        self.enabled = bool(enabled) and bool(self.api_token)

        if not self.enabled:
            logger.warning(
                "VhsClient отключён: %s",
                "нет api_token" if not self.api_token else "enabled=False",
            )
        else:
            logger.info("VhsClient инициализирован: %s", self.base_url)

    # ------------------------------------------------------------------ #
    #  Низкоуровневый запрос
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None

        url = f"{self.base_url}{path}"
        headers = {
            "X-Api-Token": self.api_token,
            "Accept": "application/json",
        }
        if json_data is not None:
            headers["Content-Type"] = "application/json"

        try:
            resp = requests.request(
                method=method, url=url, headers=headers,
                json=json_data, timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as e:
            logger.warning("VHS Core недоступен (%s %s): %s", method, path, e)
            return None

        if 300 <= resp.status_code < 400:
            location = resp.headers.get("Location", "?")
            logger.warning(
                "VHS Core %s %s → %s (redirect to %s). Проверьте api_token.",
                method, path, resp.status_code, location,
            )
            return None

        if resp.status_code >= 400:
            err = ""
            try:
                err = resp.json().get("error", "")
            except Exception:
                err = resp.text[:200]
            logger.warning(
                "VHS Core %s %s → %s: %s",
                method, path, resp.status_code, err,
            )
            return None

        try:
            return resp.json()
        except Exception:
            return {"ok": True}

    # ------------------------------------------------------------------ #
    #  Healthcheck
    # ------------------------------------------------------------------ #
    def ping(self) -> bool:
        result = self._request("GET", "/api/ping")
        return bool(result and result.get("ok"))

    # ------------------------------------------------------------------ #
    #  Маркеры
    # ------------------------------------------------------------------ #
    def create_marker(
        self,
        camera_id: str,
        marker_type: str = "event",
        description: Optional[str] = None,
        source: str = "analytics",
        external_id: Optional[str] = None,
        snapshot_url: Optional[str] = None,
        plate: Optional[str] = None,          # ← добавлено для совместимости
    ) -> Optional[int]:
        """
        Создаёт маркер в Core.

        Параметр `plate` — опциональный: если передан и не указан
        в description, он будет добавлен к описанию.
        """
        # Если plate передан и не упомянут в описании — добавляем
        if plate and plate not in (description or ""):
            if description:
                description = f"{description}: {plate}"
            else:
                description = f"Номер: {plate}"

        payload = {
            "camera_id": camera_id,
            "marker_type": marker_type,
            "source": source,
        }
        if description:
            payload["description"] = description
        if external_id is not None:
            payload["external_id"] = str(external_id)
        if snapshot_url:
            payload["snapshot_url"] = snapshot_url

        result = self._request("POST", "/api/marker/create", json_data=payload)
        if result and result.get("ok"):
            return result.get("id")
        return None

    # ------------------------------------------------------------------ #
    #  АСКУД
    # ------------------------------------------------------------------ #
    def check_access_plate(
        self,
        plate: str,
        camera_id: Optional[str] = None,
        event_id: Optional[int] = None,
        snapshot_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Проверяет номер по базе АСКУД в Core.

        Возвращает:
        {
            "action":  "open" | "deny" | "alarm" | "unknown" | "error",
            "plate":   "А123ВС77",
            "device_id":   1,
            "device_name": "Шлагбаум",
            "message": "Открыто: Шлагбаум",
            "log_id": 123,
        }
        """
        payload = {"plate": plate}
        if camera_id:
            payload["camera_id"] = camera_id
        if event_id is not None:
            payload["event_id"] = int(event_id)
        if snapshot_url:
            payload["snapshot_url"] = snapshot_url

        return self._request(
            "POST", "/api/access/check_plate", json_data=payload,
        )

    # ------------------------------------------------------------------ #
    #  Управление записью
    # ------------------------------------------------------------------ #
    def start_recording(self, camera_id: str) -> bool:
        result = self._request(
            "POST", f"/api/camera/{camera_id}/record/start"
        )
        return bool(result and result.get("ok"))

    def stop_recording(self, camera_id: str) -> bool:
        result = self._request(
            "POST", f"/api/camera/{camera_id}/record/stop"
        )
        return bool(result and result.get("ok"))

    def recording_status(self, camera_id: str) -> Optional[bool]:
        result = self._request("GET", "/api/cameras")
        if not result:
            return None
        for cam in result:
            if cam.get("id") == camera_id:
                return bool(cam.get("recording"))
        return None

    # ------------------------------------------------------------------ #
    #  Управление потоком камеры
    # ------------------------------------------------------------------ #
    def camera_start(self, camera_id: str) -> bool:
        result = self._request("POST", f"/api/camera/{camera_id}/start")
        return bool(result)

    def camera_stop(self, camera_id: str) -> bool:
        result = self._request("POST", f"/api/camera/{camera_id}/stop")
        return bool(result)