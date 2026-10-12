"""
Модуль иконки в системном трее для VHS Core.

Показывает статус, предоставляет меню и всплывающие уведомления
(для событий Analytics: сбой, перезапуск, запуск).

Зависимости: pystray, Pillow.
Если библиотеки не установлены или нет графической сессии (Linux
без DISPLAY) — модуль тихо отключается, Core продолжает работать.
"""

import logging
import threading
import webbrowser
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    import pystray
    from pystray import MenuItem as Item, Menu
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False
    logger.warning(
        "pystray или Pillow не установлены — иконка в трее недоступна"
    )


# ---------------------------------------------------------------------- #
#  Генерация иконки по умолчанию
# ---------------------------------------------------------------------- #
def _create_default_icon(color: str = "#28a745", size: int = 64):
    """Рисует простую иконку: круг + стилизованная камера."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Фон — круг
    margin = 4
    d.ellipse((margin, margin, size - margin, size - margin), fill=color)

    # Корпус камеры — белый прямоугольник
    bw, bh = size * 0.48, size * 0.34
    bx, by = (size - bw) / 2, (size - bh) / 2
    d.rectangle((bx, by, bx + bw, by + bh), fill="white")

    # Объектив — круг в центре
    cx, cy = size / 2, size / 2
    r = size * 0.09
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)

    return img


def _load_icon(path: Optional[Path], color: str):
    """Загружает иконку из файла или генерирует стандартную."""
    if path and Path(path).exists():
        try:
            from PIL import Image
            return Image.open(path)
        except Exception as e:
            logger.warning("Не удалось загрузить иконку %s: %s", path, e)
    return _create_default_icon(color)


# ---------------------------------------------------------------------- #
#  Класс иконки в трее
# ---------------------------------------------------------------------- #
class TrayIcon:
    """
    Иконка в системном трее Windows.

    Запускается в отдельном потоке-демоне, не блокирует Flask.
    """

    def __init__(
        self,
        host: str,
        port: int,
        on_stop: Callable[[], None],
        icon_path: Optional[Path] = None,
        tooltip: str = "VHS",
    ):
        self.host = host
        self.port = port
        self.on_stop = on_stop
        self.icon_path = Path(icon_path) if icon_path else None
        self.tooltip = tooltip

        self._icon = None
        self._thread = None

        # Если сервер слушает 0.0.0.0 — открывать localhost
        display_host = "localhost" if host in ("0.0.0.0", "::") else host
        self._url = f"http://{display_host}:{port}"

    # ------------------------------------------------------------------ #
    #  Действия меню
    # ------------------------------------------------------------------ #
    def _open_browser(self, icon=None, item=None):
        logger.info("Открываем браузер: %s", self._url)
        try:
            webbrowser.open(self._url)
        except Exception as e:
            logger.error("Не удалось открыть браузер: %s", e)

    def _quit(self, icon=None, item=None):
        logger.info("Завершение работы по команде из трея")
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass
        try:
            self.on_stop()
        except Exception as e:
            logger.exception("Ошибка при завершении: %s", e)
            os._exit(1)

    # ------------------------------------------------------------------ #
    #  Меню
    # ------------------------------------------------------------------ #
    def _build_menu(self):
        return Menu(
            Item(
                "Открыть в браузере",
                self._open_browser,
                default=True,   # сработает по левому клику
            ),
            Item(f"Адрес: {self._url}", None, enabled=False),
            Menu.SEPARATOR,
            Item("Выход", self._quit),
        )

    # ------------------------------------------------------------------ #
    #  Уведомления
    # ------------------------------------------------------------------ #
    def notify(self, title: str, message: str, level: str = "info"):
        """
        Показать всплывающее уведомление из трея.

        level: "info" | "warning" | "error"
        На Windows использует balloon notification от иконки.
        Если трей недоступен — просто пишет в лог.
        """
        if not PYSTRAY_AVAILABLE or not self._icon:
            log_method = {
                "info": logger.info,
                "warning": logger.warning,
                "error": logger.error,
            }.get(level, logger.info)
            log_method("[TRAY] %s: %s", title, message)
            return

        try:
            # pystray.notify(message, title)
            self._icon.notify(message, title)
            logger.debug("Уведомление отправлено: %s — %s", title, message)
        except Exception as e:
            logger.debug("Не удалось показать уведомление: %s", e)

    # ------------------------------------------------------------------ #
    #  Жизненный цикл
    # ------------------------------------------------------------------ #
    def start(self):
        if not PYSTRAY_AVAILABLE:
            logger.warning("Иконка в трее не запущена (pystray недоступен)")
            return

        if self._thread and self._thread.is_alive():
            return

        def _run():
            try:
                image = _load_icon(self.icon_path, "#28a745")
                self._icon = pystray.Icon(
                    name="VHS",
                    icon=image,
                    title=f"{self.tooltip} — сервер запущен\n{self._url}",
                    menu=self._build_menu(),
                )
                logger.info("Иконка в трее запущена")
                self._icon.run()
            except Exception as e:
                logger.exception("Ошибка в работе иконки: %s", e)

        self._thread = threading.Thread(
            target=_run, name="tray-icon", daemon=True
        )
        self._thread.start()

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass