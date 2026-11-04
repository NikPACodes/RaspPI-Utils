"""
Работа с конфигурацией Noctua Fan Control.

Модуль отвечает за:
- чтение JSON-файлов;
- загрузку config.json;
- атомарное сохранение config.json;
- переключение режима AUTO/MANUAL;
- изменение manual PWM;
- изменение температурных LEVEL CPU и NVMe.

Изменения сохраняются в config.json.
Применение новых настроек выполняет noctua_daemon.
"""
import json, os, time

from noctua.paths import CONFIG

# ------------------------------------------------------------
# JSON
# ------------------------------------------------------------
def read_json(path):
    """
    Прочение JSON
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_cfg():
    """
    Загрузка config.json
    """
    cfg = read_json(CONFIG)
    if cfg is None:
        raise SystemExit(f"Не могу прочитать {CONFIG}")
    return cfg


def save_cfg(cfg):
    """
    Атомарное сохранение config.json.
    """
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG)
    time.sleep(1.2)


# ------------------------------------------------------------
# Mode
# ------------------------------------------------------------
def set_mode(mode):
    """
    Режим: AUTO / MANUAL
    """
    if mode not in ("auto", "manual"):
        raise SystemExit("mode: auto или manual")
    cfg = load_cfg()
    cfg["control"]["mode"] = mode
    save_cfg(cfg)


# ------------------------------------------------------------
# Manual PWM
# ------------------------------------------------------------
def set_pwm(value):
    """
    Ручная установка PWM

    Команда автоматически переводит controller в manual.
    """
    pwm = int(value)
    if not 0 <= pwm <= 255:
        raise SystemExit("PWM должен быть 0..255")
    cfg = load_cfg();
    cfg["control"]["manual_pwm"] = pwm
    cfg["control"]["mode"] = "manual"
    save_cfg(cfg)


# ------------------------------------------------------------
# Edit LEVEL
# ------------------------------------------------------------
def set_level(sensor, index, temp, pwm):
    """
    Изменить один LEVEL температурной кривой CPU или NVMe.

    sensor:
        "cpu"  — изменить cpu_levels
        "nvme" — изменить nvme_levels
    index:
        Номер LEVEL, начиная с 0
    temp:
        Температура срабатывания LEVEL, °C
    pwm:
        PWM для данного LEVEL, от 0 до 255
    """
    sensor = sensor.lower()
    if sensor not in ("cpu", "nvme"):
        raise SystemExit("sensor должен быть cpu или nvme")

    key = f"{sensor}_levels"
    index = int(index)
    temp = float(temp)
    pwm = int(pwm)

    if not 0 <= pwm <= 255:
        raise SystemExit("PWM должен быть 0..255")

    cfg = load_cfg()
    levels = [list(item) for item in cfg["control"][key]]
    if not 0 <= index < len(levels):
        raise SystemExit("Неверный LEVEL")

    levels[index] = [temp, pwm]
    temperatures = [float(item[0]) for item in levels]
    if temperatures != sorted(temperatures) or len(temperatures) != len(set(temperatures)):
        raise SystemExit("Температуры LEVEL должны строго возрастать")

    cfg["control"][key] = levels
    save_cfg(cfg)