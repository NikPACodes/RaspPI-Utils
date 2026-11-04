"""
Общие пути файлов проекта Noctua Fan Control.
"""
from pathlib import Path

# Корневой каталог noctua-fan-control.
BASE = Path(__file__).resolve().parent.parent

# Файл настроек config.json
CONFIG = BASE / "config.json"
# Runtime-файл status.json (создается noctua_daemon)
STATUS = BASE / "status.json"

# Системный config.txt Raspberry Pi
BOOTCFG = Path("/boot/firmware/config.txt")
