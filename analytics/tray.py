"""
Иконка в системном трее для VHS-Analytics.
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
    logger.warning("pystray или Pillow не установлены — иконка в трее недоступна")


def _create_default_icon(color: str = "#0dcaf0", size: int = 64):
    """Иконка аналитики: голубой круг + символ «глаз»."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = 4
    d.ellipse((margin, margin, size - margin, size - margin), fill=color)

    # Стилизованный глаз: эллипс + зрачок
    cx, cy = size / 2, size / 2
    d.ellipse((cx - size * 0.3, cy - size * 0.18,
               cx + size * 0.3, cy + size * 0.18), fill="white")
    r = size * 0.12
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)

    return img


def _load_icon(path: Optional[Path], color: str):
    if path and Path(path).exists():
        try:
            from PIL import Image
            return Image.open(path)
        except Exception as e:
            logger.warning("Не удалось загрузить иконку %s: %s", path, e)
    return _create_default_icon(color)


class TrayIcon:
    def __init__(
        self,
        host: str,
        port: int,
        on_stop: Callable[[], None],
        icon_path: Optional[Path] = None,
        tooltip: str = "VHS Analytics",
    ):
        self.host = host
        self.port = port
        self.on_stop = on_stop
        self.icon_path = Path(icon_path) if icon_path else None
        self.tooltip = tooltip

        self._icon = None
        self._thread = None

        display_host = "localhost" if host in ("0.0.0.0", "::") else host
        self._url = f"http://{display_host}:{port}"

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

    def _build_menu(self):
        return Menu(
            Item("Открыть в браузере", self._open_browser, default=True),
            Item(f"Адрес: {self._url}", None, enabled=False),
            Menu.SEPARATOR,
            Item("Выход", self._quit),
        )

    def start(self):
        if not PYSTRAY_AVAILABLE:
            logger.warning("Иконка в трее не запущена (pystray недоступен)")
            return

        if self._thread and self._thread.is_alive():
            return

        def _run():
            try:
                image = _load_icon(self.icon_path, "#0dcaf0")
                self._icon = pystray.Icon(
                    name="VHS-Analytics",
                    icon=image,
                    title=f"{self.tooltip}\n{self._url}",
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