"""
Модуль управления камерами VHS Core.

CameraManager:
- читает config.yaml;
- конвертирует старый формат analytics.mode в новые флаги
  (обратная совместимость);
- создаёт и хранит объекты Camera;
- запускает/останавливает потоки камер;
- PTZ-контроллер для IP-камер с ONVIF;
- CRUD камер через UI;
- смена папки записей на лету.
"""

import logging
import yaml
from pathlib import Path
from typing import Dict, List, Optional

from camera import Camera

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
#  Конвертация старого формата analytics.mode в флаги
# ---------------------------------------------------------------------- #
def _legacy_mode_to_flags(mode: str) -> dict:
    """Старый `mode` → набор флагов."""
    mode = (mode or "off").lower()
    flags = {
        "enabled": False,
        "detect_objects": False,
        "detect_plates": False,
        "detect_faces": False,
        "detect_motion": False,
    }
    if mode == "off":
        return flags
    if mode in ("person", "yolo"):
        flags["enabled"] = True
        flags["detect_objects"] = True
    elif mode in ("plate", "alpr"):
        flags["enabled"] = True
        flags["detect_plates"] = True
    elif mode == "both":
        flags["enabled"] = True
        flags["detect_objects"] = True
        flags["detect_plates"] = True
    elif mode == "face":
        flags["enabled"] = True
        flags["detect_faces"] = True
    elif mode == "motion":
        flags["enabled"] = True
        flags["detect_motion"] = True
    return flags


def _normalize_analytics(ana: dict) -> dict:
    """
    Приводит блок analytics камеры к новому формату с флагами.
    Если в конфиге старый `mode` — конвертирует.
    """
    ana = ana or {}

    # Старый формат: {"mode": "plate", "zones": [...]}
    if "mode" in ana and "enabled" not in ana:
        flags = _legacy_mode_to_flags(ana.get("mode"))
        flags["zones"] = ana.get("zones") or []
        return flags

    # Новый формат: флаги
    flags = {
        "enabled":         bool(ana.get("enabled", False)),
        "detect_objects":  bool(ana.get("detect_objects", False)),
        "detect_plates":   bool(ana.get("detect_plates", False)),
        "detect_faces":    bool(ana.get("detect_faces", False)),
        "detect_motion":   bool(ana.get("detect_motion", False)),
        "zones":           ana.get("zones") or [],
    }
    # Автовключение enabled при наличии любого флага
    any_flag = (
        flags["detect_objects"] or flags["detect_plates"] or
        flags["detect_faces"] or flags["detect_motion"]
    )
    if any_flag:
        flags["enabled"] = True
    return flags


# ---------------------------------------------------------------------- #
#  PTZ-контроллер
# ---------------------------------------------------------------------- #
class PTZController:
    """Управление поворотной камерой по ONVIF."""

    def __init__(self, ip: str, port: int, user: str, password: str):
        self.ip = ip
        self.port = port
        self.user = user
        self.password = password
        self._cam = None
        self._ptz = None
        self._profile_token = None

    def _ensure_connected(self):
        if self._ptz is not None:
            return
        from onvif import ONVIFCamera

        self._cam = ONVIFCamera(self.ip, self.port, self.user, self.password)
        self._ptz = self._cam.create_ptz_service()
        media = self._cam.create_media_service()
        profiles = media.GetProfiles()
        if not profiles:
            raise RuntimeError("ONVIF: не найдено ни одного профиля")
        self._profile_token = profiles[0].token

    def move(self, direction: str, speed: float = 0.5, duration: float = 0.5):
        import threading
        import time

        self._ensure_connected()
        speed = max(0.1, min(1.0, speed))

        velocity = {"PanTilt": {"x": 0.0, "y": 0.0}, "Zoom": {"x": 0.0}}
        if direction == "left":
            velocity["PanTilt"]["x"] = -speed
        elif direction == "right":
            velocity["PanTilt"]["x"] = speed
        elif direction == "up":
            velocity["PanTilt"]["y"] = speed
        elif direction == "down":
            velocity["PanTilt"]["y"] = -speed
        elif direction == "zoom_in":
            velocity["Zoom"]["x"] = speed
        elif direction == "zoom_out":
            velocity["Zoom"]["x"] = -speed
        else:
            raise ValueError(f"Неизвестное направление: {direction}")

        request = self._ptz.create_type("ContinuousMove")
        request.ProfileToken = self._profile_token
        request.Velocity = velocity
        self._ptz.ContinuousMove(request)

        def _auto_stop():
            time.sleep(duration)
            try:
                self.stop()
            except Exception:
                pass

        threading.Thread(target=_auto_stop, daemon=True).start()

    def stop(self):
        self._ensure_connected()
        self._ptz.Stop({"ProfileToken": self._profile_token})

    def goto_preset(self, preset_token: str):
        self._ensure_connected()
        self._ptz.GotoPreset({
            "ProfileToken": self._profile_token,
            "PresetToken": preset_token,
        })


# ---------------------------------------------------------------------- #
#  Менеджер камер
# ---------------------------------------------------------------------- #
class CameraManager:
    """Центральный реестр камер."""

    def __init__(self, config_path: str, db, recordings_dir: Path,
                 base_dir: Optional[Path] = None):
        self.config_path = Path(config_path)
        self.db = db
        self.base_dir = Path(base_dir) if base_dir else self.config_path.parent
        self.recordings_dir = Path(recordings_dir).resolve()

        self._cameras: Dict[str, Camera] = {}
        self._ptz: Dict[str, PTZController] = {}
        self._config: dict = {}

        self._load_config()
        # Конвертируем старые конфиги в новый формат и сохраняем
        if self._migrate_analytics_format():
            self._save_config()

    # ------------------------------------------------------------------ #
    #  Конфиг
    # ------------------------------------------------------------------ #
    def _load_config(self):
        if not self.config_path.exists():
            raise FileNotFoundError(f"Не найден конфиг: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f) or {}
        logger.info(
            "Конфиг загружен: %s камер",
            len(self._config.get("cameras", []) or [])
        )

    def _migrate_analytics_format(self) -> bool:
        """
        Если в конфиге старый формат analytics.mode —
        конвертирует в флаги. Возвращает True, если что-то изменилось.
        """
        changed = False
        for cam_cfg in self._config.get("cameras", []) or []:
            ana = cam_cfg.get("analytics") or {}
            if "mode" in ana and "enabled" not in ana:
                new_ana = _normalize_analytics(ana)
                cam_cfg["analytics"] = new_ana
                changed = True
                logger.info(
                    "[%s] Миграция analytics.mode='%s' → флаги: %s",
                    cam_cfg.get("id"), ana.get("mode"),
                    {k: v for k, v in new_ana.items() if k != "zones"},
                )
        return changed

    def _save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                self._config, f,
                allow_unicode=True, sort_keys=False,
                default_flow_style=False,
            )

    def reload_config(self):
        logger.info("Перезагрузка конфигурации...")
        old_ids = set(self._cameras.keys())
        self._load_config()
        if self._migrate_analytics_format():
            self._save_config()

        new_ids = set()

        for cam_cfg in self._config.get("cameras", []) or []:
            cam_id = cam_cfg["id"]
            new_ids.add(cam_id)

            if cam_id in self._cameras:
                existing = self._cameras[cam_id]
                existing.name = cam_cfg.get("name", existing.name)
                if str(existing.source) != str(cam_cfg.get("source")):
                    existing.stop()
                    self._cameras[cam_id] = self._build_camera(cam_cfg)
                    self._cameras[cam_id].start()
            else:
                cam = self._build_camera(cam_cfg)
                self._cameras[cam_id] = cam
                if cam_cfg.get("enabled", True):
                    cam.start()

        for cam_id in old_ids - new_ids:
            self._cameras[cam_id].stop()
            del self._cameras[cam_id]
            self._ptz.pop(cam_id, None)
            logger.info("Камера %s удалена из конфига и остановлена", cam_id)

    def _build_camera(self, cfg: dict) -> Camera:
        cfg = dict(cfg)
        cfg.setdefault(
            "segment_duration",
            self._config.get("server", {}).get("segment_duration", 3600),
        )
        cam = Camera(cfg, db=self.db, recordings_dir=self.recordings_dir)

        ptz_cfg = cfg.get("ptz") or {}
        if ptz_cfg.get("enabled"):
            self._ptz[cam.id] = PTZController(
                ip=ptz_cfg["ip"],
                port=int(ptz_cfg.get("port", 80)),
                user=ptz_cfg.get("user", ""),
                password=ptz_cfg.get("password", ""),
            )
        return cam

    # ------------------------------------------------------------------ #
    #  Смена папки записей
    # ------------------------------------------------------------------ #
    def change_recordings_dir(self, new_dir: Path) -> bool:
        new_dir = Path(new_dir).resolve()
        old_dir = self.recordings_dir

        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error("Не удалось создать папку %s: %s", new_dir, e)
            return False

        logger.info("Смена папки записей: %s → %s", old_dir, new_dir)
        self.stop_all()

        server_cfg = self._config.setdefault("server", {})
        history = list(server_cfg.get("recordings_dirs_history") or [])
        old_dir_str = str(old_dir)
        if old_dir_str and old_dir_str not in history and old_dir != new_dir:
            history.append(old_dir_str)
        if len(history) > 10:
            history = history[-10:]
        server_cfg["recordings_dirs_history"] = history

        try:
            rel = new_dir.relative_to(self.base_dir)
            server_cfg["recordings_dir"] = str(rel).replace("\\", "/")
        except ValueError:
            server_cfg["recordings_dir"] = str(new_dir)

        self._save_config()
        self.recordings_dir = new_dir

        self._cameras.clear()
        self._ptz.clear()

        for cam_cfg in self._config.get("cameras", []) or []:
            cam = self._build_camera(cam_cfg)
            self._cameras[cam.id] = cam

        for cam_cfg in self._config.get("cameras", []) or []:
            if cam_cfg.get("enabled", True):
                cam = self._cameras.get(cam_cfg["id"])
                if cam:
                    cam.start()

        logger.info("Папка записей переключена на %s", new_dir)
        return True

    def get_recordings_dirs(self) -> List[Path]:
        dirs = [self.recordings_dir]
        history = (
            self._config.get("server", {}).get("recordings_dirs_history") or []
        )
        for h in history:
            p = Path(h)
            if not p.is_absolute():
                p = (self.base_dir / p).resolve()
            if p not in dirs:
                dirs.append(p)
        return dirs

    # ------------------------------------------------------------------ #
    #  CRUD камер
    # ------------------------------------------------------------------ #
    def _find_camera_cfg(self, camera_id: str):
        for i, c in enumerate(self._config.get("cameras", []) or []):
            if c.get("id") == camera_id:
                return i, c
        return -1, None

    def add_camera(self, cam_cfg: dict):
        if self._find_camera_cfg(cam_cfg["id"])[1] is not None:
            raise ValueError(f"Камера с id={cam_cfg['id']} уже существует")
        # Нормализуем analytics
        cam_cfg["analytics"] = _normalize_analytics(cam_cfg.get("analytics"))
        self._config.setdefault("cameras", [])
        self._config["cameras"].append(cam_cfg)
        self._save_config()
        self.reload_config()

    def update_camera(self, camera_id: str, new_cfg: dict):
        idx, _ = self._find_camera_cfg(camera_id)
        if idx < 0:
            raise ValueError(f"Камера {camera_id} не найдена")
        new_cfg = dict(new_cfg)
        new_cfg["id"] = camera_id
        new_cfg["analytics"] = _normalize_analytics(new_cfg.get("analytics"))
        self._config["cameras"][idx] = new_cfg
        self._save_config()
        self.reload_config()

    def delete_camera(self, camera_id: str) -> bool:
        idx, _ = self._find_camera_cfg(camera_id)
        if idx < 0:
            return False
        self._config["cameras"].pop(idx)
        self._save_config()
        self.reload_config()
        return True

    def get_camera_config(self, camera_id: str) -> Optional[dict]:
        _, c = self._find_camera_cfg(camera_id)
        if not c:
            return None
        out = dict(c)
        # Нормализуем analytics для UI
        out["analytics"] = _normalize_analytics(out.get("analytics"))
        return out

    def list_camera_configs(self) -> List[dict]:
        out = []
        for c in (self._config.get("cameras") or []):
            item = dict(c)
            item["analytics"] = _normalize_analytics(item.get("analytics"))
            out.append(item)
        return out

    # ------------------------------------------------------------------ #
    #  Жизненный цикл
    # ------------------------------------------------------------------ #
    def start_all(self):
        for cam_cfg in self._config.get("cameras", []) or []:
            cam_id = cam_cfg["id"]
            if cam_id in self._cameras:
                continue
            cam = self._build_camera(cam_cfg)
            self._cameras[cam_id] = cam
            if cam_cfg.get("enabled", True):
                cam.start()
        logger.info("Запущено камер: %s", len(self._cameras))

    def stop_all(self):
        for cam in self._cameras.values():
            try:
                cam.stop()
            except Exception as e:
                logger.error("Ошибка остановки камеры %s: %s", cam.id, e)
        logger.info("Все камеры остановлены")

    # ------------------------------------------------------------------ #
    #  Доступ
    # ------------------------------------------------------------------ #
    def get(self, camera_id: str) -> Optional[Camera]:
        return self._cameras.get(camera_id)

    def all(self) -> List[Camera]:
        return list(self._cameras.values())

    def get_status_all(self) -> List[dict]:
        return [cam.get_status() for cam in self._cameras.values()]

    def get_config(self) -> dict:
        return self._config

    # ------------------------------------------------------------------ #
    #  PTZ
    # ------------------------------------------------------------------ #
    def ptz_move(self, camera_id: str, direction: str, speed: float = 0.5):
        if camera_id not in self._ptz:
            raise ValueError(f"PTZ не настроен для камеры {camera_id}")
        self._ptz[camera_id].move(direction, speed)

    def ptz_stop(self, camera_id: str):
        if camera_id not in self._ptz:
            raise ValueError(f"PTZ не настроен для камеры {camera_id}")
        self._ptz[camera_id].stop()

    def has_ptz(self, camera_id: str) -> bool:
        return camera_id in self._ptz