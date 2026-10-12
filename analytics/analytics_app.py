"""
Точка входа VHS-Analytics.

Собирает вместе:
- Database (SQLite): пользователи, события, настройки
- Flask-Login (аутентификация + роли)
- AnalyticsEngine (движок детекции: MOG2, YOLO, ALPR, Face)

Поддерживает новую схему аналитики через флаги:
- detect_objects — YOLOv8
- detect_plates  — ALPR
- detect_faces   — InsightFace (задел)
- detect_motion  — MOG2

Совместим со старым форматом analytics_mode (off/person/plate/both).
"""

import sys
import os
import json
import time
import logging
import atexit
import threading
from pathlib import Path
from datetime import datetime, timedelta

import requests
import yaml
import cv2
from flask import (
    Flask, render_template, Response, request, jsonify,
    redirect, url_for, send_file, abort, flash,
)
from flask_login import login_required, current_user

from database import Database, ROLE_ADMIN, ROLE_VIEWER
from auth import (
    init_auth, auth_bp, admin_required,
    api_token_required, _is_valid_token,
)
from engine import AnalyticsEngine


# ---------------------------------------------------------------------- #
#  Пути
# ---------------------------------------------------------------------- #
def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


def get_resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).parent.resolve()


BASE_DIR = get_base_dir()
RESOURCE_DIR = get_resource_dir()
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "analytics.log"
ICON_PATH = BASE_DIR / "icon.ico"


# ---------------------------------------------------------------------- #
#  Логирование
# ---------------------------------------------------------------------- #
def _setup_logging():
    handlers = []
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(fh)
    except Exception as e:
        print(f"Не удалось открыть лог-файл {LOG_PATH}: {e}")

    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(sh)

    logging.basicConfig(level=logging.INFO, handlers=handlers)


_setup_logging()
logger = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


SETTINGS_DEFAULTS = {
    "ui.theme":        "dark",
    "ui.accent":       "#0d6efd",
    "ui.page_size":    "50",
}


# ---------------------------------------------------------------------- #
#  Конвертация legacy mode → флаги
# ---------------------------------------------------------------------- #
def _legacy_mode_to_flags(mode: str) -> dict:
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
    """Приводит блок analytics камеры к новому формату с флагами."""
    ana = ana or {}
    if "mode" in ana and "enabled" not in ana:
        flags = _legacy_mode_to_flags(ana.get("mode"))
        flags["zones"] = ana.get("zones") or []
        return flags

    flags = {
        "enabled":         bool(ana.get("enabled", False)),
        "detect_objects":  bool(ana.get("detect_objects", False)),
        "detect_plates":   bool(ana.get("detect_plates", False)),
        "detect_faces":    bool(ana.get("detect_faces", False)),
        "detect_motion":   bool(ana.get("detect_motion", False)),
        "zones":           ana.get("zones") or [],
    }
    any_flag = (
        flags["detect_objects"] or flags["detect_plates"] or
        flags["detect_faces"] or flags["detect_motion"]
    )
    if any_flag:
        flags["enabled"] = True
    return flags


# ---------------------------------------------------------------------- #
#  Получение конфига из VHS Core
# ---------------------------------------------------------------------- #
def fetch_config_from_core(local_config: dict) -> dict:
    """
    Пытается получить конфиг камер и режимов аналитики из VHS Core.

    Core отдаёт камеры с полем `analytics` (флагами).
    Если пришёл старый формат `analytics_mode` — конвертируем.

    Если Core недоступен — работаем на локальном config.yaml.
    """
    vhs_cfg = local_config.get("vhs_core", {}) or {}
    base_url = vhs_cfg.get("base_url", "http://localhost:8000").rstrip("/")
    api_token = vhs_cfg.get("api_token", "")

    if not api_token:
        logger.warning("Нет vhs_core.api_token — пропускаю запрос конфига у Core")
        return local_config

    url = f"{base_url}/api/analytics/config"
    try:
        r = requests.get(
            url,
            headers={"X-Api-Token": api_token},
            timeout=5.0,
        )
        if r.status_code != 200:
            logger.warning(
                "Core вернул %s на /api/analytics/config — "
                "использую локальный конфиг",
                r.status_code,
            )
            return local_config

        data = r.json()
    except Exception as e:
        logger.warning("Не удалось получить конфиг у Core: %s", e)
        logger.info("Работаю на локальном config.yaml")
        return local_config

    core_cameras = data.get("cameras") or []

    local_cams_by_id = {
        c.get("id"): c for c in (local_config.get("cameras") or [])
    }

    new_cameras = []
    for cc in core_cameras:
        cam_id = cc.get("id")
        if not cam_id:
            continue

        local_cam = local_cams_by_id.get(cam_id, {})
        merged = dict(local_cam)

        merged["id"] = cam_id
        merged["name"] = cc.get("name") or local_cam.get("name") or cam_id
        merged["enabled"] = bool(cc.get("enabled", True))

        # --- Аналитика: новый формат (флаги) или legacy (mode) ---
        core_ana = cc.get("analytics")
        if core_ana:
            merged["analytics"] = _normalize_analytics(core_ana)
        else:
            # Старый Core отдал только analytics_mode
            legacy_mode = cc.get("analytics_mode") or "off"
            merged["analytics"] = _legacy_mode_to_flags(legacy_mode)
            # Зоны — из локального конфига
            local_ana = local_cam.get("analytics") or {}
            merged["analytics"]["zones"] = local_ana.get("zones") or []

        # Зоны — из Core, если там есть
        if core_ana and core_ana.get("zones"):
            merged["analytics"]["zones"] = core_ana["zones"]

        new_cameras.append(merged)

    core_ids = {c["id"] for c in new_cameras}
    for cam_id in local_cams_by_id:
        if cam_id not in core_ids:
            logger.info(
                "Камера '%s' есть локально, но отсутствует в Core — пропускаю",
                cam_id,
            )

    local_config["cameras"] = new_cameras

    # Лог со сводкой по режимам
    summary = []
    for c in new_cameras:
        ana = c.get("analytics") or {}
        if not ana.get("enabled"):
            continue
        kinds = []
        if ana.get("detect_objects"):
            kinds.append("obj")
        if ana.get("detect_plates"):
            kinds.append("plate")
        if ana.get("detect_faces"):
            kinds.append("face")
        if ana.get("detect_motion"):
            kinds.append("motion")
        summary.append(f"{c['id']}:[{'+'.join(kinds) or 'off'}]")

    logger.info(
        "Получен конфиг от Core: %d камер, %s",
        len(new_cameras),
        " ".join(summary) if summary else "аналитика нигде не включена",
    )
    return local_config


# ---------------------------------------------------------------------- #
#  Уведомления в Core
# ---------------------------------------------------------------------- #
def notify_core(local_config: dict, title: str, message: str,
                level: str = "info"):
    vhs_cfg = local_config.get("vhs_core", {}) or {}
    base_url = vhs_cfg.get("base_url", "http://localhost:8000").rstrip("/")
    api_token = vhs_cfg.get("api_token", "")
    if not api_token:
        return
    try:
        requests.post(
            f"{base_url}/api/analytics/notify",
            headers={"X-Api-Token": api_token},
            json={"title": title, "message": message, "level": level},
            timeout=3.0,
        )
    except Exception as e:
        logger.debug("Не удалось уведомить Core: %s", e)


# ---------------------------------------------------------------------- #
#  Фабрика приложения
# ---------------------------------------------------------------------- #
def create_app() -> Flask:
    local_config = load_config()
    config = fetch_config_from_core(local_config)

    server_cfg = config.get("server", {})
    auth_cfg = config.get("auth", {})

    app = Flask(
        __name__,
        template_folder=str(RESOURCE_DIR / "templates"),
        static_folder=str(RESOURCE_DIR / "static"),
    )
    app.secret_key = server_cfg.get("secret_key", "dev-analytics-secret")

    snapshots_dir = BASE_DIR / server_cfg.get("snapshots_dir", "snapshots")
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    db = Database(db_path=str(BASE_DIR / "analytics.db"))
    db.ensure_default_admin(
        auth_cfg.get("default_user", "admin"),
        auth_cfg.get("default_password", "admin"),
    )

    init_auth(app, db)
    app.register_blueprint(auth_bp)

    engine = AnalyticsEngine(
        config=config,
        db=db,
        snapshots_dir=snapshots_dir,
        base_dir=BASE_DIR,
    )

    app.db = db
    app.engine = engine
    app.config_data = config
    app.snapshots_dir = snapshots_dir
    app.vhs_config = config.get("vhs_core", {}) or {}

    @app.template_filter("from_json")
    def from_json_filter(value):
        if not value:
            return {}
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}

    @app.context_processor
    def inject_settings():
        s = dict(SETTINGS_DEFAULTS)
        s.update(db.get_all_settings())
        return {"app_settings": s}

    @app.context_processor
    def inject_branding():
        branding = config.get("branding", {}) or {}
        year = branding.get("copyright_year") or datetime.now().year
        return {
            "branding": {
                "product_name": branding.get("product_name", "VHS Analytics"),
                "company": branding.get("company", ""),
                "author": branding.get("author", ""),
                "copyright_holder": branding.get("copyright_holder", ""),
                "year": year,
                "version": branding.get("version", ""),
                "show_version": bool(branding.get("show_version", True)),
            }
        }

    # ================================================================== #
    #  UI
    # ================================================================== #
    @app.route("/")
    @login_required
    def index():
        statuses = engine.get_status_all()
        recent_events = db.get_events(limit=12)
        total_events = db.count_events()
        events_today = db.count_events(
            date_from=datetime.now().replace(hour=0, minute=0, second=0,
                                             microsecond=0)
        )
        return render_template(
            "index.html",
            statuses=statuses,
            recent_events=recent_events,
            total_events=total_events,
            events_today=events_today,
        )

    @app.route("/events")
    @login_required
    def events_page():
        return render_template("events.html")

    @app.route("/event/<int:event_id>")
    @login_required
    def event_page(event_id: int):
        event = db.get_event(event_id)
        if not event:
            abort(404)
        return render_template("event.html", event=event)

    @app.route("/cameras")
    @login_required
    def cameras_page():
        statuses = engine.get_status_all()
        cameras_cfg = config.get("cameras", []) or []
        return render_template(
            "cameras.html",
            statuses=statuses,
            cameras_cfg=cameras_cfg,
        )

    # ================================================================== #
    #  Превью (MJPEG)
    # ================================================================== #
    @app.route("/preview/<camera_id>")
    @login_required
    def preview(camera_id: str):
        import time as _time

        def generate():
            last_sent = None
            boundary = b"--frame\r\n"
            while True:
                frame = engine.get_last_frame(camera_id)
                if frame is None:
                    _time.sleep(0.2)
                    continue

                fid = id(frame)
                if fid == last_sent:
                    _time.sleep(0.04)
                    continue
                last_sent = fid

                ok, jpg = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70]
                )
                if not ok:
                    continue
                data = jpg.tobytes()
                yield (
                    boundary
                    + b"Content-Type: image/jpeg\r\n"
                    + b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                    + data
                    + b"\r\n"
                )
                _time.sleep(0.04)

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    # ================================================================== #
    #  Снапшоты
    # ================================================================== #
    @app.route("/snapshot/<int:event_id>")
    @login_required
    def snapshot(event_id: int):
        ev = db.get_event(event_id)
        if not ev or not ev.get("snapshot_path"):
            abort(404)

        path = Path(ev["snapshot_path"]).resolve()
        base = snapshots_dir.resolve()
        try:
            path.relative_to(base)
        except ValueError:
            logger.warning("Попытка отдать снапшот вне snapshots/: %s", path)
            abort(404)

        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/jpeg")

    # ================================================================== #
    #  API: события
    # ================================================================== #
    @app.route("/api/events")
    @login_required
    def api_events():
        camera_id = request.args.get("camera_id") or None
        event_type = request.args.get("event_type") or None
        date_str = request.args.get("date")
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))

        date_from = date_to = None
        if date_str:
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").date()
                date_from = datetime.combine(day, datetime.min.time())
                date_to = date_from + timedelta(days=1)
            except ValueError:
                return jsonify({"error": "invalid date"}), 400

        events = db.get_events(
            camera_id=camera_id,
            event_type=event_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        total = db.count_events(
            camera_id=camera_id,
            event_type=event_type,
            date_from=date_from,
            date_to=date_to,
        )
        return jsonify({"events": events, "total": total})

    @app.route("/api/event_types")
    @login_required
    def api_event_types():
        rows = db._execute(
            "SELECT DISTINCT event_type FROM events ORDER BY event_type",
            fetch="all",
        )
        types = [r["event_type"] for r in rows if r["event_type"]]
        known = ["person", "car", "truck", "bus", "motorcycle", "bicycle",
                 "plate", "face", "motion"]
        for k in known:
            if k not in types:
                types.append(k)
        return jsonify(types)

    @app.route("/api/events/<int:event_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_event_delete(event_id: int):
        ok = db.delete_event(event_id)
        return jsonify({"ok": ok})

    @app.route("/api/events/purge", methods=["POST"])
    @login_required
    @admin_required
    def api_events_purge():
        data = request.get_json(silent=True) or {}
        days = int(data.get("days", 30))
        removed = db.purge_older_than(days)
        return jsonify({"removed": removed})

    # ================================================================== #
    #  API: статистика
    # ================================================================== #
    @app.route("/api/stats")
    @login_required
    def api_stats():
        camera_id = request.args.get("camera_id") or None
        days = int(request.args.get("days", 30))
        summary = db.get_events_summary(camera_id=camera_id, days_back=days)
        hourly = db.get_hourly_stats(days_back=7)
        return jsonify({
            "summary": summary,
            "hourly": hourly["hourly"],
            "total": db.count_events(camera_id=camera_id),
        })

    # ================================================================== #
    #  API: статус движка
    # ================================================================== #
    @app.route("/api/cameras")
    @login_required
    def api_cameras():
        return jsonify(engine.get_status_all())

    @app.route("/api/camera/<camera_id>/start", methods=["POST"])
    @login_required
    @admin_required
    def api_camera_start(camera_id: str):
        ok = engine.start_camera(camera_id)
        if not ok:
            return jsonify({"error": "не удалось запустить"}), 400
        return jsonify({"ok": True})

    @app.route("/api/camera/<camera_id>/stop", methods=["POST"])
    @login_required
    @admin_required
    def api_camera_stop(camera_id: str):
        ok = engine.stop_camera(camera_id)
        if not ok:
            return jsonify({"error": "камера не запущена"}), 404
        return jsonify({"ok": True})

    @app.route("/api/config/reload", methods=["POST"])
    @login_required
    @admin_required
    def api_reload_config():
        engine.reload_config()
        return jsonify({"ok": True})

    # ================================================================== #
    #  API: настройки UI
    # ================================================================== #
    @app.route("/api/settings", methods=["GET"])
    @login_required
    def api_settings_get():
        s = dict(SETTINGS_DEFAULTS)
        s.update(db.get_all_settings())
        return jsonify(s)

    @app.route("/api/settings", methods=["POST"])
    @login_required
    @admin_required
    def api_settings_update():
        data = request.get_json(silent=True) or {}
        for key, value in data.items():
            if key in SETTINGS_DEFAULTS:
                db.set_setting(key, str(value))
        return jsonify({"ok": True})

    # ================================================================== #
    #  API: управление процессом (для Core)
    # ================================================================== #
    @app.route("/api/ping")
    def api_ping():
        return jsonify({
            "ok": True,
            "product": "VHS Analytics",
            "version": config.get("branding", {}).get("version", ""),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        })

    @app.route("/api/shutdown", methods=["POST"])
    @api_token_required
    def api_shutdown():
        logger.info("Получена команда shutdown от Core")

        def _delayed_exit():
            time.sleep(1.0)
            logger.info("Analytics завершает работу")
            os._exit(0)

        threading.Thread(target=_delayed_exit, daemon=True).start()
        return jsonify({"ok": True, "message": "shutting down"})

    # ================================================================== #
    #  Обработчики ошибок
    # ================================================================== #
    @app.errorhandler(403)
    def forbidden(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "forbidden"}), 403
        return render_template("base.html", error_403=True), 403

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return render_template("base.html", error_404=True), 404

    return app


# ---------------------------------------------------------------------- #
#  Запуск
# ---------------------------------------------------------------------- #
app = create_app()


def _shutdown():
    try:
        logger.info("Остановка движка...")
        app.engine.stop_all()
        logger.info("Движок остановлен")
    except Exception as e:
        logger.exception("Ошибка остановки: %s", e)


def _full_shutdown():
    _shutdown()
    logger.info("Завершение процесса")
    os._exit(0)


atexit.register(_shutdown)


def _has_gui() -> bool:
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


if __name__ == "__main__":
    server_cfg = app.config_data.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = int(server_cfg.get("port", 8100))

    logger.info("Запускаю движок аналитики...")
    app.engine.start_all()

    tray_icon = None
    if _has_gui():
        try:
            from tray import TrayIcon
            tray_icon = TrayIcon(
                host=host, port=port,
                on_stop=_full_shutdown,
                icon_path=ICON_PATH if ICON_PATH.exists() else None,
                tooltip="VHS Analytics",
            )
            tray_icon.start()
        except Exception as e:
            logger.exception("Не удалось запустить иконку в трее: %s", e)
            tray_icon = None

    logger.info("VHS Analytics запускается на http://%s:%s", host, port)
    notify_core(app.config_data, "Analytics", "Модуль аналитики запущен",
                "info")

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        if tray_icon:
            tray_icon.stop()
        _shutdown()