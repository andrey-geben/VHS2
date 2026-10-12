"""
Тест движка: запускаем аналитику, наблюдаем события в БД.

Запуск:
    python test_engine.py
"""

import logging
import time
import yaml
from pathlib import Path

from database import Database
from engine import AnalyticsEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

db = Database(db_path=str(BASE_DIR / "analytics.db"))
db.ensure_default_admin("admin", "admin")

snapshots_dir = BASE_DIR / config["server"].get("snapshots_dir", "snapshots")

engine = AnalyticsEngine(
    config=config,
    db=db,
    snapshots_dir=snapshots_dir,
)

engine.start_all()

try:
    print()
    print("Аналитика запущена. 60 секунд наблюдаем за событиями...")
    print("Для проверки: помашите рукой перед камерой.")
    print()
    for i in range(12):
        time.sleep(5)
        statuses = engine.get_status_all()
        for s in statuses:
            print(
                f"[{s['id']}] connected={s['connected']}  "
                f"fps={s['fps']:.1f}  events={s.get('event_count', 0)}"
            )
        print("-" * 60)
finally:
    engine.stop_all()

# Показываем последние события из БД
events = db.get_events(limit=10)
print()
print(f"Последние события ({len(events)}):")
for e in events:
    print(
        f"  #{e['id']}  {e['created_at']}  {e['camera_id']}  "
        f"zone={e['zone_name']}  area={e['area']}  "
        f"snap={e['snapshot_path']}"
    )