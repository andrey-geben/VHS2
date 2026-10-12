"""
Модуль аутентификации и авторизации VHS Core.

Роли:
- admin  — полный доступ
- viewer — только просмотр и архив

Помимо сессионной авторизации через Flask-Login есть поддержка
API-токена для внешних модулей (VHS-Analytics и др.):
- токен задаётся в config.yaml → analytics.api_token;
- передаётся либо в заголовке X-Api-Token, либо Authorization: Bearer,
  либо в query (?token=XXX) — для совместимости с /video_feed;
- даёт полный доступ к управляющим эндпоинтам, помеченным
  @admin_required (start/stop камеры, запись, PTZ и т.п.).

Декораторы:
- @admin_required      — сессия админа ИЛИ валидный API-токен;
- @api_token_required  — только валидный API-токен (для внешних модулей).
"""

from functools import wraps
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, abort, jsonify, current_app,
)
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from werkzeug.security import check_password_hash
import hmac

from database import ROLE_ADMIN, ROLE_VIEWER


# ---------------------------------------------------------------------- #
#  Модель пользователя для Flask-Login
# ---------------------------------------------------------------------- #
class User(UserMixin):
    """Обёртка над записью из БД, совместимая с Flask-Login."""

    def __init__(self, user_id: int, username: str, role: str):
        self.id = str(user_id)
        self.username = username
        self.role = role or ROLE_VIEWER

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_viewer(self) -> bool:
        return self.role == ROLE_VIEWER

    @staticmethod
    def from_db_row(row: dict) -> "User":
        return User(row["id"], row["username"], row.get("role", ROLE_VIEWER))


# ---------------------------------------------------------------------- #
#  Проверка API-токена
# ---------------------------------------------------------------------- #
def _extract_token() -> str:
    """
    Извлекает токен из запроса.

    Поддерживаются три варианта:
    - заголовок X-Api-Token (рекомендуется);
    - заголовок Authorization: Bearer XXX (стандарт OAuth2);
    - query-параметр ?token=XXX (для /video_feed — там заголовки неудобны).
    """
    token = request.headers.get("X-Api-Token", "")
    if token:
        return token.strip()

    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    return (request.args.get("token") or "").strip()


def _get_expected_token() -> str:
    """Возвращает ожидаемый токен из конфига."""
    config = getattr(current_app, "config_data", None) or {}
    return ((config.get("analytics", {}) or {}).get("api_token") or "").strip()


def _is_valid_token() -> bool:
    """Проверяет, что токен в запросе совпадает с ожидаемым."""
    expected = _get_expected_token()
    if not expected:
        return False
    provided = _extract_token()
    if not provided:
        return False
    # Сравнение с постоянным временем — защита от timing-атак
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------- #
#  Декораторы
# ---------------------------------------------------------------------- #
def admin_required(f):
    """
    Доступ только администраторам (по сессии Flask-Login)
    ИЛИ клиентам с валидным API-токеном.

    Это позволяет VHS-Analytics вызывать управляющие эндпоинты
    (start/stop записи, PTZ) без логина.

    Для /api/* возвращает JSON 403, для HTML — страницу 403.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 1. Валидный API-токен — пропускаем
        if _is_valid_token():
            return f(*args, **kwargs)

        # 2. Сессионный пользователь
        if not current_user.is_authenticated:
            return current_app.login_manager.unauthorized()

        if not current_user.is_admin:
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "forbidden",
                    "message": "Требуются права администратора",
                }), 403
            abort(403)

        return f(*args, **kwargs)
    return wrapper


def api_token_required(f):
    """
    Доступ только с валидным API-токеном.

    Используется для эндпоинтов, которые должны быть недоступны
    из браузера даже администратору (например, служебные вебхуки,
    создание маркеров от Analytics).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _is_valid_token():
            return jsonify({
                "error": "unauthorized",
                "message": "Требуется валидный API-токен",
            }), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------- #
#  Blueprint аутентификации
# ---------------------------------------------------------------------- #
auth_bp = Blueprint("auth", __name__)


def init_auth(app, db):
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Пожалуйста, войдите в систему."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        row = db.get_user_by_id(int(user_id))
        return User.from_db_row(row) if row else None

    @login_manager.unauthorized_handler
    def unauthorized():
        """Неавторизованных перенаправляем на страницу логина."""
        return redirect(url_for("auth.login", next=request.path))

    # ------------------------------------------------------------------ #
    #  Маршруты
    # ------------------------------------------------------------------ #
    @auth_bp.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            remember = request.form.get("remember") == "on"

            user_row = db.get_user(username)
            if user_row and check_password_hash(
                user_row["password_hash"], password
            ):
                user = User.from_db_row(user_row)
                login_user(user, remember=remember)

                next_url = request.args.get("next") or url_for("index")
                # Защита от open redirect: разрешаем только относительные пути
                if not next_url.startswith("/"):
                    next_url = url_for("index")
                return redirect(next_url)

            flash("Неверное имя пользователя или пароль.", "danger")

        return render_template("login.html")

    @auth_bp.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Вы вышли из системы.", "info")
        return redirect(url_for("auth.login"))

    @auth_bp.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        """Смена собственного пароля."""
        if request.method == "POST":
            current_pwd = request.form.get("current_password", "")
            new_pwd = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")

            user_row = db.get_user(current_user.username)
            if not user_row or not check_password_hash(
                user_row["password_hash"], current_pwd
            ):
                flash("Неверный текущий пароль.", "danger")
            elif len(new_pwd) < 4:
                flash("Новый пароль слишком короткий (минимум 4 символа).",
                      "warning")
            elif new_pwd != confirm:
                flash("Пароли не совпадают.", "warning")
            else:
                db.set_password(user_row["id"], new_pwd)
                flash("Пароль успешно изменён.", "success")
                return redirect(url_for("index"))

        return render_template("profile.html")

    return login_manager