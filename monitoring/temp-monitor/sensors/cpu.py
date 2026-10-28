"""
Чтение температуры процессора через sysfs.
"""
from pathlib import Path


# Типы датчиков
SUPPORTED_TYPES = {"cpu-thermal", "soc_thermal", "bcm2835_thermal"}


def _read_temp(path: Path) -> float | None:
    """
    Чтение температуры из указанного файла sysfs и приведение к °C.

    Linux хранит температуру в милли °C (тысячных долях градуса).
    Прим: 42500 = 42.5 °C
    """
    try:
        return int(path.read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


def read_temperature() -> float | None:
    """
    Поиск температурного датчика CPU и чтение значений.
    """
    # Путь к каталогу температурных датчиков и получение всех элементы
    zones = sorted(Path('/sys/class/thermal').glob('thermal_zone*'))
    for zone in zones:
        try:
            # Чтение названий датчиков
            kind = (zone / 'type').read_text().strip()
        except OSError:
            continue

        if kind in SUPPORTED_TYPES:
            # Чтение температуры
            value = _read_temp(zone / 'temp')
            if value is not None:
                return value

    # Если подходящий тип не найден или показания недоступны, пробуем первую температурную зону Linux.
    # ВАЖНО: thermal_zone0 не гарантированно является CPU.
    # На Raspberry Pi такой fallback часто полезен, но на Intel/AMD требует дополнительной проверки.
    return _read_temp(Path('/sys/class/thermal/thermal_zone0/temp'))
