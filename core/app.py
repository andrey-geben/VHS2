"""
Точка входа сервера видеонаблюдения VHS Core.

Собирает вместе:
- Database (SQLite): пользователи, записи, настройки, маркеры, АСКУД
- Flask-Login (аутентификация + роли)
- CameraManager (реестр камер)
- ArchiveManager (архив записей)
- AnalyticsManager (управление подпроцессом VHS-Analytics)
- AccessManager (АСКУД: номера + реле + журнал)

Аналитика на камеру настраивается независимыми флагами:
- detect_objects — детекция объектов (YOLO)
- detect_plates  — распознавание номеров (ALPR)
- detect_faces   — распознавание лиц (задел на будущее)
- detect_motion  — детекция движения (MOG2)
"""

import sys
import os
import shutil
import logging
import atexit
from pathlib import Path
from datetime import datetime, timedelta

import yaml
from flask import (
    Flask, render_template, Response, request, jsonify,
    redirect, url_for, send_file, abort, flash,
)
from flask_login import login_required, current_user

from database import Database, ROLE_ADMIN, ROLE_VIEWER, LIST_WHITE, LIST_BLACK
from auth import (
    init_auth, auth_bp, admin_required, api_token_required,
    _is_valid_token,
)
from camera_manager import CameraManager
from recorder import ArchiveManager
from analytics_manager import AnalyticsManager
from access_manager import AccessManager


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
LOG_PATH = BASE_DIR / "vhs.log"
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
    "ui.theme":                "dark",
    "ui.accent":               "#0d6efd",
    "ui.tile_columns":         "4",
    "ui.show_fps":             "1",
    "ui.default_page":         "index",
    "system.max_archive_days": "0",
    "system.mjpeg_quality":    "80",
}


# ---------------------------------------------------------------------- #
#  Валидатор конфигурации камеры
# ---------------------------------------------------------------------- #
def _convert_legacy_mode(mode: str) -> dict:
    """
    Конвертирует устаревший `mode` в набор флагов.

    Для обратной совместимости со старыми конфигами.
    """
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


def _validate_camera_cfg(data: dict) -> dict:
    cam_id = (data.get("id") or "").strip()
    if not cam_id:
        raise ValueError("ID камеры обязателен")
    if not all(ch.isalnum() or ch in "_-" for ch in cam_id):
        raise ValueError("ID может содержать только буквы, цифры, _ и -")

    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Имя камеры обязательно")

    cam_type = data.get("type", "ip")
    if cam_type not in ("analog", "ip"):
        raise ValueError("Тип камеры должен быть 'analog' или 'ip'")

    source_raw = data.get("source")
    if source_raw is None or source_raw == "":
        raise ValueError("Источник обязателен")

    if cam_type == "analog":
        try:
            source = int(source_raw)
        except (TypeError, ValueError):
            raise ValueError("Для аналоговой камеры источник — целое число")
        if source < 0:
            raise ValueError("Индекс не может быть отрицательным")
    else:
        source = str(source_raw).strip()
        if not source.startswith(("rtsp://", "http://", "https://")):
            raise ValueError("Для IP-камеры источник должен начинаться с "
                             "rtsp://, http:// или https://")

    try:
        fps = max(1, min(60, int(data.get("fps", 25))))
        width = max(160, min(3840, int(data.get("width", 640))))
        height = max(120, min(2160, int(data.get("height", 480))))
    except (TypeError, ValueError):
        raise ValueError("FPS, ширина и высота должны быть числами")

    cfg = {
        "id": cam_id,
        "name": name,
        "type": cam_type,
        "source": source,
        "enabled": bool(data.get("enabled", True)),
        "fps": fps,
        "width": width,
        "height": height,
        "auto_record": bool(data.get("auto_record", False)),
    }

    # --- Аналитика камеры ---
    ana_in = data.get("analytics") or {}

    # Обратная совместимость: если пришёл старый `mode` — конвертируем.
    if "mode" in ana_in and "enabled" not in ana_in:
        ana = _convert_legacy_mode(ana_in.get("mode"))
        # Сохраняем зоны
        ana["zones"] = ana_in.get("zones") or []
    else:
        ana = {
            "enabled":         bool(ana_in.get("enabled", False)),
            "detect_objects":  bool(ana_in.get("detect_objects", False)),
            "detect_plates":   bool(ana_in.get("detect_plates", False)),
            "detect_faces":    bool(ana_in.get("detect_faces", False)),
            "detect_motion":   bool(ana_in.get("detect_motion", False)),
            "zones":           ana_in.get("zones") or [],
        }

    # Если включены флаги, но `enabled` стоит False — включаем автоматически
    any_flag = (
        ana.get("detect_objects") or ana.get("detect_plates") or
        ana.get("detect_faces") or ana.get("detect_motion")
    )
    if any_flag and not ana.get("enabled"):
        ana["enabled"] = True

    cfg["analytics"] = ana

    ptz = data.get("ptz") or {}
    if ptz.get("enabled"):
        cfg["ptz"] = {
            "enabled": True,
            "ip": (ptz.get("ip") or "").strip(),
            "port": int(ptz.get("port", 80)),
            "user": ptz.get("user") or "",
            "password": ptz.get("password") or "",
        }
    return cfg


def _normalize_plate(text: str) -> str:
    import re
    return re.sub(r"[^А-ЯA-Z0-9]", "", (text or "").upper())


# ---------------------------------------------------------------------- #
#  Фабрика приложения
# ---------------------------------------------------------------------- #
def create_app() -> Flask:
    config = load_config()
    server_cfg = config.get("server", {})
    auth_cfg = config.get("auth", {})
    analytics_cfg = config.get("analytics", {}) or {}

    app = Flask(
        __name__,
        template_folder=str(RESOURCE_DIR / "templates"),
        static_folder=str(RESOURCE_DIR / "static"),
    )
    app.secret_key = server_cfg.get("secret_key", "dev-secret-change-me")

    recordings_dir_cfg = server_cfg.get("recordings_dir", "recordings")
    recordings_dir = Path(recordings_dir_cfg)
    if not recordings_dir.is_absolute():
        recordings_dir = (BASE_DIR / recordings_dir).resolve()
    else:
        recordings_dir = recordings_dir.resolve()
    recordings_dir.mkdir(parents=True, exist_ok=True)

    history_cfg = server_cfg.get("recordings_dirs_history") or []
    history_dirs = []
    for h in history_cfg:
        p = Path(h)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        history_dirs.append(p)

    db = Database(db_path=str(BASE_DIR / "surveillance.db"))
    db.ensure_default_admin(
        auth_cfg.get("default_user", "admin"),
        auth_cfg.get("default_password", "admin"),
    )

    init_auth(app, db)
    app.register_blueprint(auth_bp)

    camera_manager = CameraManager(
        config_path=str(CONFIG_PATH),
        db=db,
        recordings_dir=recordings_dir,
        base_dir=BASE_DIR,
    )
    archive_manager = ArchiveManager(
        db=db,
        recordings_dir=recordings_dir,
        extra_dirs=history_dirs,
    )

    analytics_mgr = AnalyticsManager(
        exe_path=analytics_cfg.get(
            "exe_path", "..\\analytics\\VHS-Analytics.exe"
        ),
        base_dir=BASE_DIR,
        enabled=bool(analytics_cfg.get("managed", True)),
        auto_start=bool(analytics_cfg.get("auto_start", False)),
        auto_restart=bool(analytics_cfg.get("auto_restart", True)),
        max_restarts_per_hour=int(
            analytics_cfg.get("max_restarts_per_hour", 3)
        ),
        url=analytics_cfg.get("url", "http://localhost:8100"),
    )

    access_manager = AccessManager(db=db)

    app.camera_manager = camera_manager
    app.archive_manager = archive_manager
    app.analytics_manager = analytics_mgr
    app.access_manager = access_manager
    app.db = db
    app.config_data = config

    # ------------------------------------------------------------------ #
    #  Контекст-процессоры
    # ------------------------------------------------------------------ #
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
                "product_name": branding.get("product_name", "VHS"),
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
        cameras = camera_manager.all()
        return render_template(
            "index.html",
            cameras=cameras,
            statuses=camera_manager.get_status_all(),
            disk=archive_manager.get_disk_usage(),
        )

    @app.route("/camera/<camera_id>")
    @login_required
    def camera_page(camera_id: str):
        cam = camera_manager.get(camera_id)
        if not cam:
            abort(404)
        return render_template(
            "camera.html",
            camera=cam,
            status=cam.get_status(),
            has_ptz=camera_manager.has_ptz(camera_id),
        )

    @app.route("/archive")
    @login_required
    def archive_page():
        cameras = camera_manager.all()
        return render_template("archive.html", cameras=cameras)

    @app.route("/access")
    @login_required
    @admin_required
    def access_page():
        return render_template("access.html")

    # ================================================================== #
    #  MJPEG
    # ================================================================== #
    @app.route("/video_feed/<camera_id>")
    def video_feed(camera_id: str):
        if not _is_valid_token() and not current_user.is_authenticated:
            if (request.args.get("token") is not None
                    or "application/json" in request.headers.get("Accept", "")):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("auth.login", next=request.path))

        cam = camera_manager.get(camera_id)
        if not cam:
            abort(404)

        return Response(
            cam.mjpeg_generator(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    # ================================================================== #
    #  Архивные файлы
    # ================================================================== #
    @app.route("/archive/play/<int:recording_id>")
    @login_required
    def archive_play(recording_id: int):
        path = archive_manager.resolve_file(recording_id)
        if not path:
            abort(404)
        return send_file(path, mimetype="video/x-msvideo",
                         as_attachment=False, conditional=True)

    @app.route("/archive/download/<int:recording_id>")
    @login_required
    def archive_download(recording_id: int):
        path = archive_manager.resolve_file(recording_id)
        if not path:
            abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    # ================================================================== #
    #  API: чтение
    # ================================================================== #
    @app.route("/api/cameras")
    @login_required
    def api_cameras():
        return jsonify(camera_manager.get_status_all())

    @app.route("/api/archive")
    @login_required
    def api_archive():
        camera_id = request.args.get("camera_id") or None
        date_str = request.args.get("date")
        limit = int(request.args.get("limit", 500))

        date_from = date_to = None
        if date_str:
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").date()
                date_from = datetime.combine(day, datetime.min.time())
                date_to = date_from + timedelta(days=1)
            except ValueError:
                return jsonify({"error": "invalid date"}), 400

        rows = archive_manager.list_recordings(
            camera_id=camera_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        return jsonify(rows)

    @app.route("/api/archive/dates")
    @login_required
    def api_archive_dates():
        camera_id = request.args.get("camera_id") or None
        days_back = int(request.args.get("days", 30))
        return jsonify(archive_manager.get_dates_summary(camera_id, days_back))

    @app.route("/api/disk")
    @login_required
    def api_disk():
        return jsonify(archive_manager.get_disk_usage())

    # ================================================================== #
    #  API: ping
    # ================================================================== #
    @app.route("/api/ping")
    @api_token_required
    def api_ping():
        return jsonify({
            "ok": True,
            "product": config.get("branding", {}).get("product_name", "VHS"),
            "version": config.get("branding", {}).get("version", ""),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        })

    # ================================================================== #
    #  API: маркеры
    # ================================================================== #
    @app.route("/api/marker/create", methods=["POST"])
    @api_token_required
    def api_marker_create():
        data = request.get_json(silent=True) or {}
        camera_id = (data.get("camera_id") or "").strip()
        if not camera_id:
            return jsonify({"error": "camera_id обязателен"}), 400

        if not camera_manager.get(camera_id):
            return jsonify({"error": f"камера {camera_id} не найдена"}), 404

        marker_id = db.add_marker(
            camera_id=camera_id,
            marker_type=(data.get("marker_type") or "event")[:64],
            description=(data.get("description") or "")[:512] or None,
            source=(data.get("source") or "external")[:64],
            external_id=(str(data["external_id"])[:64]
                         if data.get("external_id") is not None else None),
            snapshot_url=(data.get("snapshot_url") or "")[:1024] or None,
        )
        logger.info(
            "Маркер #%s создан: %s/%s (%s)",
            marker_id, camera_id, data.get("marker_type", "event"),
            data.get("source", "external"),
        )
        return jsonify({"ok": True, "id": marker_id})

    @app.route("/api/markers")
    def api_markers_list():
        if not _is_valid_token() and not current_user.is_authenticated:
            if request.args.get("token") is not None or \
               "application/json" in request.headers.get("Accept", ""):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("auth.login", next=request.path))

        camera_id = request.args.get("camera_id") or None
        date_str = request.args.get("date")
        limit = int(request.args.get("limit", 500))

        date_from = date_to = None
        if date_str:
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").date()
                date_from = datetime.combine(day, datetime.min.time())
                date_to = date_from + timedelta(days=1)
            except ValueError:
                return jsonify({"error": "invalid date"}), 400

        rows = db.get_markers(
            camera_id=camera_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        return jsonify(rows)

    @app.route("/api/marker/<int:marker_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_marker_delete(marker_id: int):
        ok = db.delete_marker(marker_id)
        return jsonify({"ok": ok})

    @app.route("/api/markers/purge", methods=["POST"])
    @login_required
    @admin_required
    def api_markers_purge():
        data = request.get_json(silent=True) or {}
        days = int(data.get("days", 90))
        removed = db.purge_markers_older_than(days)
        return jsonify({"removed": removed})

    # ================================================================== #
    #  API: АСКУД — проверка номера
    # ================================================================== #
    @app.route("/api/access/check_plate", methods=["POST"])
    @api_token_required
    def api_access_check_plate():
        data = request.get_json(silent=True) or {}
        plate = (data.get("plate") or "").strip()
        if not plate:
            return jsonify({"error": "plate обязателен"}), 400

        camera_id = (data.get("camera_id") or "").strip() or None
        event_id = data.get("event_id")
        snapshot_url = (data.get("snapshot_url") or "").strip() or None

        result = access_manager.check_plate(
            plate=plate,
            camera_id=camera_id,
            event_id=int(event_id) if event_id else None,
            snapshot_url=snapshot_url,
        )

        logger.info(
            "АСКУД: %s → %s (%s)",
            plate, result.get("action"), result.get("message"),
        )
        return jsonify(result)

    # ================================================================== #
    #  API: АСКУД — номера
    # ================================================================== #
    @app.route("/api/access/plates", methods=["GET"])
    @login_required
    @admin_required
    def api_access_plates_list():
        list_type = request.args.get("list_type") or None
        search = request.args.get("search") or None
        enabled_only = request.args.get("enabled_only") == "1"
        limit = int(request.args.get("limit", 500))

        rows = db.list_access_plates(
            list_type=list_type,
            enabled_only=enabled_only,
            search=search,
            limit=limit,
        )
        return jsonify(rows)

    @app.route("/api/access/plates", methods=["POST"])
    @login_required
    @admin_required
    def api_access_plates_create():
        data = request.get_json(silent=True) or {}
        plate = (data.get("plate") or "").strip()
        if not plate:
            return jsonify({"error": "Номер обязателен"}), 400

        plate_norm = _normalize_plate(plate)
        if not plate_norm or len(plate_norm) < 4:
            return jsonify({"error": "Некорректный номер"}), 400

        if db.find_access_plate_by_number(plate_norm):
            return jsonify({"error": "Такой номер уже есть в базе"}), 409

        list_type = data.get("list_type") or LIST_WHITE
        if list_type not in ("white", "black", "regular"):
            return jsonify({"error": "Недопустимый тип списка"}), 400

        try:
            plate_id = db.add_access_plate(
                plate=plate_norm,
                owner=(data.get("owner") or "")[:128] or None,
                description=(data.get("description") or "")[:512] or None,
                list_type=list_type,
                enabled=bool(data.get("enabled", True)),
            )
        except Exception as e:
            logger.exception("Ошибка добавления номера")
            return jsonify({"error": str(e)}), 500

        return jsonify({"ok": True, "id": plate_id, "plate": plate_norm})

    @app.route("/api/access/plates/<int:plate_id>", methods=["PUT"])
    @login_required
    @admin_required
    def api_access_plates_update(plate_id: int):
        data = request.get_json(silent=True) or {}
        if not db.get_access_plate(plate_id):
            return jsonify({"error": "Не найдено"}), 404

        kwargs = {}
        if "plate" in data:
            kwargs["plate"] = _normalize_plate(data["plate"])
        if "owner" in data:
            kwargs["owner"] = data["owner"]
        if "description" in data:
            kwargs["description"] = data["description"]
        if "list_type" in data:
            kwargs["list_type"] = data["list_type"]
        if "enabled" in data:
            kwargs["enabled"] = bool(data["enabled"])

        try:
            db.update_access_plate(plate_id, **kwargs)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

        return jsonify({"ok": True})

    @app.route("/api/access/plates/<int:plate_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_access_plates_delete(plate_id: int):
        ok = db.delete_access_plate(plate_id)
        return jsonify({"ok": ok})

    @app.route("/api/access/plates/import", methods=["POST"])
    @login_required
    @admin_required
    def api_access_plates_import():
        data = request.get_json(silent=True) or {}
        csv_text = data.get("csv") or ""
        if not csv_text.strip():
            return jsonify({"error": "Пустой CSV"}), 400

        added = 0
        skipped = 0
        errors = []

        for i, raw_line in enumerate(csv_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if not parts or not parts[0]:
                continue
            if parts[0].lower() in ("plate", "номер"):
                continue

            plate = _normalize_plate(parts[0])
            if not plate or len(plate) < 4:
                errors.append(f"Строка {i}: некорректный номер '{parts[0]}'")
                continue

            if db.find_access_plate_by_number(plate):
                skipped += 1
                continue

            list_type = (parts[1] if len(parts) > 1 else "white").lower()
            if list_type not in ("white", "black", "regular"):
                list_type = "white"
            owner = parts[2] if len(parts) > 2 else None
            description = parts[3] if len(parts) > 3 else None

            try:
                db.add_access_plate(plate, owner, description, list_type, True)
                added += 1
            except Exception as e:
                errors.append(f"Строка {i}: {e}")

        return jsonify({
            "ok": True, "added": added,
            "skipped": skipped, "errors": errors,
        })

    @app.route("/api/access/plates/export", methods=["GET"])
    @login_required
    @admin_required
    def api_access_plates_export():
        rows = db.list_access_plates(limit=100000)
        lines = ["plate,list_type,owner,description"]
        for r in rows:
            owner = (r.get("owner") or "").replace(",", " ")
            desc = (r.get("description") or "").replace(",", " ")
            lines.append(f"{r['plate']},{r['list_type']},{owner},{desc}")
        csv_text = "\n".join(lines)

        from flask import Response as _Resp
        return _Resp(
            csv_text,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=access_plates.csv"
            },
        )

    # ================================================================== #
    #  API: АСКУД — устройства
    # ================================================================== #
    @app.route("/api/access/devices", methods=["GET"])
    @login_required
    @admin_required
    def api_access_devices_list():
        rows = db.list_access_devices()
        return jsonify(rows)

    @app.route("/api/access/devices", methods=["POST"])
    @login_required
    @admin_required
    def api_access_devices_create():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        url_open = (data.get("url_open") or "").strip()
        if not name:
            return jsonify({"error": "Имя обязательно"}), 400
        if not url_open:
            return jsonify({"error": "URL открытия обязателен"}), 400

        try:
            dev_id = db.add_access_device(
                name=name, url_open=url_open,
                type=(data.get("type") or "http_relay"),
                url_close=(data.get("url_close") or "").strip() or None,
                method=(data.get("method") or "GET").upper(),
                open_duration_sec=int(data.get("open_duration_sec") or 5),
                username=(data.get("username") or "").strip() or None,
                password=data.get("password") or None,
                headers_json=(data.get("headers_json") or "").strip() or None,
                enabled=bool(data.get("enabled", True)),
            )
        except Exception as e:
            logger.exception("Ошибка добавления устройства")
            return jsonify({"error": str(e)}), 500

        return jsonify({"ok": True, "id": dev_id})

    @app.route("/api/access/devices/<int:device_id>", methods=["PUT"])
    @login_required
    @admin_required
    def api_access_devices_update(device_id: int):
        data = request.get_json(silent=True) or {}
        if not db.get_access_device(device_id):
            return jsonify({"error": "Не найдено"}), 404

        kwargs = {}
        for field in ("name", "type", "url_open", "url_close", "method",
                      "username", "password", "headers_json"):
            if field in data:
                v = data[field]
                if isinstance(v, str):
                    v = v.strip() or None
                kwargs[field] = v
        if "open_duration_sec" in data:
            kwargs["open_duration_sec"] = int(data["open_duration_sec"] or 5)
        if "enabled" in data:
            kwargs["enabled"] = bool(data["enabled"])
        if "method" in kwargs and kwargs["method"]:
            kwargs["method"] = kwargs["method"].upper()

        try:
            db.update_access_device(device_id, **kwargs)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

        return jsonify({"ok": True})

    @app.route("/api/access/devices/<int:device_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_access_devices_delete(device_id: int):
        ok = db.delete_access_device(device_id)
        return jsonify({"ok": ok})

    @app.route("/api/access/devices/<int:device_id>/test", methods=["POST"])
    @login_required
    @admin_required
    def api_access_devices_test(device_id: int):
        result = access_manager.manual_trigger(device_id)
        if result.get("ok"):
            return jsonify({"ok": True, "message": "Устройство сработало"})
        return jsonify({
            "ok": False,
            "error": result.get("error") or "Ошибка устройства",
        }), 500

    # ================================================================== #
    #  API: АСКУД — журнал
    # ================================================================== #
    @app.route("/api/access/log")
    @login_required
    @admin_required
    def api_access_log():
        plate = request.args.get("plate") or None
        camera_id = request.args.get("camera_id") or None
        action = request.args.get("action") or None
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))

        date_from = date_to = None
        date_str = request.args.get("date")
        if date_str:
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").date()
                date_from = datetime.combine(day, datetime.min.time())
                date_to = date_from + timedelta(days=1)
            except ValueError:
                return jsonify({"error": "invalid date"}), 400

        rows = db.get_access_log(
            plate=plate, camera_id=camera_id, action=action,
            date_from=date_from, date_to=date_to,
            limit=limit, offset=offset,
        )
        total = db.count_access_log(
            plate=plate, camera_id=camera_id, action=action,
        )
        return jsonify({"events": rows, "total": total})

    @app.route("/api/access/log/purge", methods=["POST"])
    @login_required
    @admin_required
    def api_access_log_purge():
        data = request.get_json(silent=True) or {}
        days = int(data.get("days", 90))
        removed = db.purge_access_log_older_than(days)
        return jsonify({"removed": removed})

    # ================================================================== #
    #  API: Analytics
    # ================================================================== #
    @app.route("/api/analytics/status")
    @login_required
    def api_analytics_status():
        return jsonify(analytics_mgr.status())

    @app.route("/api/analytics/check_path", methods=["POST"])
    @login_required
    @admin_required
    def api_analytics_check_path():
        from pathlib import Path as _P

        data = request.get_json(silent=True) or {}
        raw_path = (data.get("exe_path") or "").strip()

        if not raw_path:
            return jsonify({"ok": False, "error": "Путь не задан"})

        p = _P(raw_path)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()

        if p.exists():
            return jsonify({
                "ok": True, "resolved": str(p), "found": True,
                "message": "Файл найден",
            })

        found_candidates = []
        for rel in AnalyticsManager.SEARCH_PATHS:
            cand = (BASE_DIR / rel).resolve()
            if cand.exists():
                found_candidates.append(str(cand))

        if found_candidates:
            return jsonify({
                "ok": True, "resolved": str(p), "found": False,
                "message": "Файл не найден, но есть кандидаты",
                "candidates": found_candidates,
            })
        return jsonify({
            "ok": True, "resolved": str(p), "found": False,
            "message": "Файл не найден. Укажите правильный путь.",
            "candidates": [],
        })

    @app.route("/api/analytics/start", methods=["POST"])
    @login_required
    @admin_required
    def api_analytics_start():
        analytics_mgr.update_settings(
            enabled=bool(analytics_cfg.get("managed", True)),
            auto_start=bool(analytics_cfg.get("auto_start", False)),
            auto_restart=bool(analytics_cfg.get("auto_restart", True)),
            max_restarts_per_hour=int(
                analytics_cfg.get("max_restarts_per_hour", 3)
            ),
            url=analytics_cfg.get("url", "http://localhost:8100"),
            exe_path=analytics_cfg.get(
                "exe_path", "..\\analytics\\VHS-Analytics.exe"
            ),
        )
        ok = analytics_mgr.start()
        return jsonify({"ok": ok, "status": analytics_mgr.status()})

    @app.route("/api/analytics/stop", methods=["POST"])
    @login_required
    @admin_required
    def api_analytics_stop():
        ok = analytics_mgr.stop()
        return jsonify({"ok": ok, "status": analytics_mgr.status()})

    @app.route("/api/analytics/restart", methods=["POST"])
    @login_required
    @admin_required
    def api_analytics_restart():
        ok = analytics_mgr.restart()
        return jsonify({"ok": ok, "status": analytics_mgr.status()})

    @app.route("/api/analytics/settings", methods=["GET"])
    @login_required
    @admin_required
    def api_analytics_settings_get():
        return jsonify({
            "managed": bool(analytics_cfg.get("managed", True)),
            "auto_start": bool(analytics_cfg.get("auto_start", False)),
            "auto_restart": bool(analytics_cfg.get("auto_restart", True)),
            "max_restarts_per_hour": int(
                analytics_cfg.get("max_restarts_per_hour", 3)
            ),
            "exe_path": analytics_cfg.get(
                "exe_path", "..\\analytics\\VHS-Analytics.exe"
            ),
            "url": analytics_cfg.get("url", "http://localhost:8100"),
            "tray_notifications": bool(
                analytics_cfg.get("tray_notifications", True)
            ),
        })

    @app.route("/api/analytics/settings", methods=["POST"])
    @login_required
    @admin_required
    def api_analytics_settings_update():
        data = request.get_json(silent=True) or {}

        new_cfg = {
            "api_token": analytics_cfg.get(
                "api_token", "change-this-analytics-token"
            ),
            "managed": bool(data.get("managed", True)),
            "auto_start": bool(data.get("auto_start", False)),
            "auto_restart": bool(data.get("auto_restart", True)),
            "max_restarts_per_hour": int(
                data.get("max_restarts_per_hour", 3)
            ),
            "exe_path": (data.get("exe_path") or "").strip()
                        or analytics_cfg.get("exe_path", ""),
            "url": (data.get("url") or "").strip()
                   or analytics_cfg.get("url", "http://localhost:8100"),
            "tray_notifications": bool(
                data.get("tray_notifications", True)
            ),
        }

        config["analytics"] = new_cfg
        app.config_data = config

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    config, f,
                    allow_unicode=True, sort_keys=False,
                    default_flow_style=False,
                )
        except Exception as e:
            logger.exception("Не удалось сохранить config.yaml: %s", e)
            return jsonify({"error": str(e)}), 500

        analytics_mgr.update_settings(
            enabled=new_cfg["managed"],
            auto_start=new_cfg["auto_start"],
            auto_restart=new_cfg["auto_restart"],
            max_restarts_per_hour=new_cfg["max_restarts_per_hour"],
            url=new_cfg["url"],
            exe_path=new_cfg["exe_path"],
        )

        logger.info("Настройки Analytics обновлены")
        return jsonify({"ok": True})

    @app.route("/api/analytics/config")
    @api_token_required
    def api_analytics_config():
        """
        Возвращает Analytics конфиг камер с флагами аналитики.
        """
        cameras_out = []
        for cam_cfg in config.get("cameras", []) or []:
            ana = cam_cfg.get("analytics") or {}

            # Обратная совместимость: если старый формат — конвертируем
            if "mode" in ana and "enabled" not in ana:
                flags = _convert_legacy_mode(ana.get("mode"))
                flags["zones"] = ana.get("zones") or []
                ana = flags

            cameras_out.append({
                "id": cam_cfg.get("id"),
                "name": cam_cfg.get("name"),
                "enabled": bool(cam_cfg.get("enabled", True)),
                # Новый формат: флаги
                "analytics": {
                    "enabled":        bool(ana.get("enabled", False)),
                    "detect_objects": bool(ana.get("detect_objects", False)),
                    "detect_plates":  bool(ana.get("detect_plates", False)),
                    "detect_faces":   bool(ana.get("detect_faces", False)),
                    "detect_motion":  bool(ana.get("detect_motion", False)),
                    "zones":          ana.get("zones") or [],
                },
                # Для обратной совместимости со старым Analytics
                "analytics_mode": _flags_to_legacy_mode(ana),
            })

        core_url = f"http://localhost:{server_cfg.get('port', 8000)}"

        return jsonify({
            "vhs_core": {
                "base_url": core_url,
                "api_token": analytics_cfg.get(
                    "api_token", "change-this-analytics-token"
                ),
            },
            "cameras": cameras_out,
        })

    @app.route("/api/analytics/notify", methods=["POST"])
    @api_token_required
    def api_analytics_notify():
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "Analytics")[:128]
        message = (data.get("message") or "")[:512]
        level = data.get("level", "info")

        tray_enabled = bool(analytics_cfg.get("tray_notifications", True))
        if tray_enabled and getattr(app, "tray_icon", None):
            try:
                app.tray_icon.notify(title, message, level)
            except Exception as e:
                logger.debug("Не удалось показать уведомление: %s", e)

        logger.info("[Analytics→Core] %s: %s", title, message)
        return jsonify({"ok": True})

    # ================================================================== #
    #  API: Хранилище
    # ================================================================== #
    @app.route("/api/storage/info")
    @login_required
    def api_storage_info():
        info = archive_manager.get_disk_usage()
        info["dirs_history"] = [
            str(d) for d in camera_manager.get_recordings_dirs()[1:]
        ]
        info["base_dir"] = str(BASE_DIR)
        return jsonify(info)

    @app.route("/api/storage/check_path", methods=["POST"])
    @login_required
    @admin_required
    def api_storage_check_path():
        data = request.get_json(silent=True) or {}
        raw = (data.get("path") or "").strip()
        if not raw:
            return jsonify({"ok": False, "error": "Путь не задан"})

        p = Path(raw)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        else:
            p = p.resolve()

        result = {
            "ok": True, "path": str(p), "exists": p.exists(),
            "created": False, "writable": False, "error": None,
            "disk_total_gb": 0, "disk_free_gb": 0, "disk_used_percent": 0,
        }

        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
                result["created"] = True
                result["exists"] = True
            except Exception as e:
                result["ok"] = False
                result["error"] = f"Не удалось создать папку: {e}"
                return jsonify(result)

        if not p.is_dir():
            result["ok"] = False
            result["error"] = "Путь не является папкой"
            return jsonify(result)

        test_file = p / ".vhs_write_test"
        try:
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
            result["writable"] = True
        except Exception as e:
            result["ok"] = False
            result["error"] = f"Нет прав на запись: {e}"
            return jsonify(result)

        try:
            usage = shutil.disk_usage(p)
            result["disk_total_gb"] = round(usage.total / 1024 / 1024 / 1024, 2)
            result["disk_free_gb"] = round(usage.free / 1024 / 1024 / 1024, 2)
            result["disk_used_percent"] = round(usage.used / usage.total * 100, 1)
        except Exception:
            pass

        return jsonify(result)

    @app.route("/api/storage/set_path", methods=["POST"])
    @login_required
    @admin_required
    def api_storage_set_path():
        data = request.get_json(silent=True) or {}
        raw = (data.get("path") or "").strip()
        if not raw:
            return jsonify({"error": "Путь не задан"}), 400

        p = Path(raw)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        else:
            p = p.resolve()

        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return jsonify({"error": f"Не удалось создать папку: {e}"}), 400

        test_file = p / ".vhs_write_test"
        try:
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
        except Exception as e:
            return jsonify({"error": f"Нет прав на запись: {e}"}), 400

        try:
            ok = camera_manager.change_recordings_dir(p)
        except Exception as e:
            logger.exception("Ошибка смены папки записей")
            return jsonify({"error": str(e)}), 500

        if not ok:
            return jsonify({"error": "Не удалось сменить папку"}), 500

        archive_manager.update_dir(
            camera_manager.recordings_dir,
            extra_dirs=camera_manager.get_recordings_dirs()[1:],
        )
        app.config_data = camera_manager.get_config()

        logger.info("Папка записей изменена на %s", p)
        return jsonify({
            "ok": True, "path": str(p),
            "dirs_history": [
                str(d) for d in camera_manager.get_recordings_dirs()[1:]
            ],
        })

    @app.route("/api/storage/forget_history", methods=["POST"])
    @login_required
    @admin_required
    def api_storage_forget_history():
        server_cfg_mut = camera_manager.get_config().get("server", {})
        server_cfg_mut["recordings_dirs_history"] = []
        camera_manager._save_config()
        archive_manager.update_dir(
            camera_manager.recordings_dir, extra_dirs=[]
        )
        logger.info("История папок записей очищена")
        return jsonify({"ok": True})

    # ================================================================== #
    #  API: управление камерами
    # ================================================================== #
    @app.route("/api/camera/<camera_id>/start", methods=["POST"])
    @admin_required
    def api_camera_start(camera_id: str):
        cam = camera_manager.get(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        cam.start()
        return jsonify({"status": "started", "camera": cam.get_status()})

    @app.route("/api/camera/<camera_id>/stop", methods=["POST"])
    @admin_required
    def api_camera_stop(camera_id: str):
        cam = camera_manager.get(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        cam.stop()
        return jsonify({"status": "stopped", "camera": cam.get_status()})

    @app.route("/api/camera/<camera_id>/record/start", methods=["POST"])
    @admin_required
    def api_record_start(camera_id: str):
        cam = camera_manager.get(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        ok = cam.start_recording()
        return jsonify({"ok": ok, "recording": cam.is_recording})

    @app.route("/api/camera/<camera_id>/record/stop", methods=["POST"])
    @admin_required
    def api_record_stop(camera_id: str):
        cam = camera_manager.get(camera_id)
        if not cam:
            return jsonify({"error": "not found"}), 404
        ok = cam.stop_recording()
        return jsonify({"ok": ok, "recording": cam.is_recording})

    @app.route("/api/config/reload", methods=["POST"])
    @admin_required
    def api_reload_config():
        camera_manager.reload_config()
        return jsonify({"status": "reloaded"})

    @app.route("/api/camera/<camera_id>/ptz", methods=["POST"])
    @admin_required
    def api_ptz(camera_id: str):
        data = request.get_json(silent=True) or {}
        direction = data.get("direction")
        speed = float(data.get("speed", 0.5))
        if direction not in ("up", "down", "left", "right",
                             "zoom_in", "zoom_out"):
            return jsonify({"error": "invalid direction"}), 400
        try:
            camera_manager.ptz_move(camera_id, direction, speed)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.exception("PTZ ошибка")
            return jsonify({"error": str(e)}), 500
        return jsonify({"status": "ok"})

    @app.route("/api/camera/<camera_id>/ptz/stop", methods=["POST"])
    @admin_required
    def api_ptz_stop(camera_id: str):
        try:
            camera_manager.ptz_stop(camera_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.exception("PTZ stop ошибка")
            return jsonify({"error": str(e)}), 500
        return jsonify({"status": "ok"})

    # ================================================================== #
    #  API: архив
    # ================================================================== #
    @app.route("/api/archive/<int:recording_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_archive_delete(recording_id: int):
        ok = db.delete_recording(recording_id)
        return jsonify({"ok": ok})

    @app.route("/api/archive/purge", methods=["POST"])
    @login_required
    @admin_required
    def api_archive_purge():
        data = request.get_json(silent=True) or {}
        days = int(data.get("days", 30))
        removed = archive_manager.purge_older_than(days)
        return jsonify({"removed": removed})

    # ================================================================== #
    #  UI: пользователи
    # ================================================================== #
    @app.route("/users")
    @login_required
    @admin_required
    def users_page():
        return render_template("users.html", users=db.list_users())

    @app.route("/api/users", methods=["GET"])
    @login_required
    @admin_required
    def api_users_list():
        return jsonify(db.list_users())

    @app.route("/api/users", methods=["POST"])
    @login_required
    @admin_required
    def api_users_create():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        role = data.get("role") or ROLE_VIEWER

        if not username or len(username) < 3:
            return jsonify({"error": "Логин должен быть не короче 3 символов"}), 400
        if len(password) < 4:
            return jsonify({"error": "Пароль должен быть не короче 4 символов"}), 400
        if role not in (ROLE_ADMIN, ROLE_VIEWER):
            return jsonify({"error": "Недопустимая роль"}), 400
        if db.user_exists(username):
            return jsonify({"error": "Пользователь уже существует"}), 409

        user_id = db.create_user(username, password, role)
        return jsonify({"ok": True, "id": user_id,
                        "username": username, "role": role})

    @app.route("/api/users/<int:user_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_users_delete(user_id: int):
        if str(user_id) == current_user.id:
            return jsonify({"error": "Нельзя удалить самого себя"}), 400
        try:
            ok = db.delete_user(user_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": ok})

    @app.route("/api/users/<int:user_id>/password", methods=["POST"])
    @login_required
    @admin_required
    def api_users_set_password(user_id: int):
        data = request.get_json(silent=True) or {}
        new_password = data.get("password") or ""
        if len(new_password) < 4:
            return jsonify({"error": "Пароль слишком короткий"}), 400
        if not db.get_user_by_id(user_id):
            return jsonify({"error": "Пользователь не найден"}), 404
        db.set_password(user_id, new_password)
        return jsonify({"ok": True})

    @app.route("/api/users/<int:user_id>/role", methods=["POST"])
    @login_required
    @admin_required
    def api_users_set_role(user_id: int):
        data = request.get_json(silent=True) or {}
        role = data.get("role")
        if role not in (ROLE_ADMIN, ROLE_VIEWER):
            return jsonify({"error": "Недопустимая роль"}), 400
        if not db.get_user_by_id(user_id):
            return jsonify({"error": "Пользователь не найден"}), 404

        if str(user_id) == current_user.id and role != ROLE_ADMIN:
            admins = [u for u in db.list_users() if u["role"] == ROLE_ADMIN]
            if len(admins) <= 1:
                return jsonify({
                    "error": "Нельзя понизить себя — вы последний администратор"
                }), 400

        db.set_role(user_id, role)
        return jsonify({"ok": True})

    # ================================================================== #
    #  UI: настройки
    # ================================================================== #
    @app.route("/settings")
    @login_required
    @admin_required
    def settings_page():
        return render_template(
            "settings.html",
            cameras=camera_manager.list_camera_configs(),
        )

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
    #  API: CRUD камер
    # ================================================================== #
    @app.route("/api/config/cameras", methods=["GET"])
    @login_required
    @admin_required
    def api_cameras_config_list():
        return jsonify(camera_manager.list_camera_configs())

    @app.route("/api/config/cameras", methods=["POST"])
    @login_required
    @admin_required
    def api_cameras_config_create():
        data = request.get_json(silent=True) or {}
        try:
            cfg = _validate_camera_cfg(data)
            camera_manager.add_camera(cfg)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.exception("Ошибка добавления камеры")
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "id": cfg["id"]})

    @app.route("/api/config/cameras/<camera_id>", methods=["PUT"])
    @login_required
    @admin_required
    def api_cameras_config_update(camera_id: str):
        data = request.get_json(silent=True) or {}
        try:
            cfg = _validate_camera_cfg({**data, "id": camera_id})
            camera_manager.update_camera(camera_id, cfg)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.exception("Ошибка обновления камеры")
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    @app.route("/api/config/cameras/<camera_id>", methods=["DELETE"])
    @login_required
    @admin_required
    def api_cameras_config_delete(camera_id: str):
        try:
            ok = camera_manager.delete_camera(camera_id)
        except Exception as e:
            logger.exception("Ошибка удаления камеры")
            return jsonify({"error": str(e)}), 500
        if not ok:
            return jsonify({"error": "Камера не найдена"}), 404
        return jsonify({"ok": True})

    # ================================================================== #
    #  Обработчики ошибок
    # ================================================================== #
    @app.errorhandler(403)
    def forbidden(_e):
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "forbidden",
                "message": "Недостаточно прав",
            }), 403
        return render_template("base.html", error_403=True), 403

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return render_template("base.html", error_404=True), 404

    return app


# ---------------------------------------------------------------------- #
#  Вспомогательная функция (обратное преобразование)
# ---------------------------------------------------------------------- #
def _flags_to_legacy_mode(ana: dict) -> str:
    """Для обратной совместимости со старым Analytics (mode)."""
    if not ana.get("enabled"):
        return "off"
    obj = ana.get("detect_objects", False)
    pl = ana.get("detect_plates", False)
    if obj and pl:
        return "both"
    if obj:
        return "person"
    if pl:
        return "plate"
    if ana.get("detect_faces"):
        return "face"
    if ana.get("detect_motion"):
        return "motion"
    return "off"


# ---------------------------------------------------------------------- #
#  Запуск
# ---------------------------------------------------------------------- #
app = create_app()


def _shutdown():
    try:
        logger.info("Остановка Analytics...")
        app.analytics_manager.shutdown()
        logger.info("Analytics остановлен")
    except Exception as e:
        logger.exception("Ошибка остановки Analytics: %s", e)

    try:
        logger.info("Остановка камер...")
        app.camera_manager.stop_all()
        logger.info("Камеры остановлены")
    except Exception as e:
        logger.exception("Ошибка остановки камер: %s", e)

    try:
        app.access_manager.close_all_timers()
    except Exception:
        pass


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
    port = int(server_cfg.get("port", 8000))

    app.camera_manager.start_all()

    app.tray_icon = None
    if _has_gui():
        try:
            from tray import TrayIcon

            def _tray_notify(title, message, level="info"):
                if app.tray_icon:
                    app.tray_icon.notify(title, message, level)

            app.tray_icon = TrayIcon(
                host=host, port=port,
                on_stop=_full_shutdown,
                icon_path=ICON_PATH if ICON_PATH.exists() else None,
            )
            app.tray_icon.start()

            app.analytics_manager.notify = _tray_notify
        except Exception as e:
            logger.exception("Не удалось запустить трей: %s", e)
            app.tray_icon = None
    else:
        logger.info("Графическая сессия не обнаружена — трей не запускается.")

    if app.analytics_manager.auto_start:
        logger.info("Автозапуск Analytics включён — запускаю...")
        app.analytics_manager.start()

    logger.info("Сервер запускается на http://%s:%s", host, port)
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        if app.tray_icon:
            app.tray_icon.stop()
        _shutdown()