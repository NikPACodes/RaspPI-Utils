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
import fcntl
import json
import math
import os
import shutil
import tempfile
import time
from contextlib import contextmanager

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
        raise ValueError(f"Не могу прочитать {CONFIG}")
    return cfg


def save_cfg(cfg):
    """
    Атомарное сохранение config.json
    """
    with edit_session() as session:
        session.save(cfg)
    time.sleep(1.2)


# ------------------------------------------------------------
# Валидация настроек (общая для fanctl и daemon)
# ------------------------------------------------------------
def _number(value, title, *, minimum=None, maximum=None, integer=False):
    """
    Проверяем не только диапазон, но и NaN/Infinity.
    """
    if isinstance(value, bool):
        raise ValueError(f"{title}: ожидается число")

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{title}: ожидается число") from exc

    if not math.isfinite(result):
        raise ValueError(f"{title}: число должно быть конечным")
    if integer and not result.is_integer():
        raise ValueError(f"{title}: нужно целое число")
    if minimum is not None and result < minimum:
        raise ValueError(f"{title}: меньше допустимого")
    if maximum is not None and result > maximum:
        raise ValueError(f"{title}: больше допустимого")

    return int(result) if integer else result


def validate_levels(name, levels):
    """
    Проверка одной температурной кривой CPU/NVMe.

    ! Температуры строго растут; PWM может оставаться прежним, но не уменьшаться.
    """
    if not isinstance(levels, list) or not levels:
        raise ValueError(f"{name}: кривая пуста")
    previous_temp = float("-inf")
    previous_pwm = -1

    for index, row in enumerate(levels):
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError(f"{name}[{index}]: нужно [температура, PWM]")

        temp = _number(row[0], f"{name}[{index}].temp", minimum=-20, maximum=150)
        pwm = _number(row[1], f"{name}[{index}].pwm", minimum=0, maximum=255, integer=True)

        if temp <= previous_temp:
            raise ValueError(f"{name}: температуры должны строго возрастать")
        if pwm < previous_pwm:
            raise ValueError(f"{name}: PWM не должен уменьшаться с ростом температуры")
        previous_temp, previous_pwm = temp, pwm


def validate_config(cfg):
    """
    Единая валидация ВСЕГО config.json до записи или применения.
    """
    try:
        if not isinstance(cfg, dict):
            raise ValueError("Ожидается объект JSON")

        gpio = cfg["gpio"]
        control = cfg["control"]
        health = cfg["health"]
        failsafe = cfg["failsafe"]

        if control["mode"] not in ("auto", "manual"):
            raise ValueError("mode: auto или manual")

        pwm_gpio = _number(gpio["pwm_gpio"], "pwm_gpio", minimum=0, maximum=27, integer=True)
        tach_gpio = _number(gpio["tach_gpio"], "tach_gpio", minimum=0, maximum=27, integer=True)

        if pwm_gpio == tach_gpio:
            raise ValueError("pwm_gpio и tach_gpio не должны совпадать")

        # Noctua NF-A4x20 5V PWM допускает 21–28 кГц (ограничил в соответствии со своей моделью)
        _number(gpio["pwm_frequency_hz"], "pwm_frequency_hz", minimum=21000, maximum=28000, integer=True)
        _number(gpio["tach_pulses_per_rev"], "tach_pulses_per_rev", minimum=1, maximum=4, integer=True)
        _number(control["update_interval_s"], "update_interval_s", minimum=1, maximum=5)
        _number(control["nvme_average_samples"], "nvme_average_samples", minimum=2, maximum=10, integer=True)
        _number(control["hysteresis_c"], "hysteresis_c", minimum=0, maximum=10)
        _number(control["minimum_dwell_s"], "minimum_dwell_s", minimum=0, maximum=300)
        _number(control["kickstart_seconds"], "kickstart_seconds", minimum=0, maximum=5)

        for field in ("min_running_pwm", "manual_pwm"):
            _number(control[field], field, minimum=0, maximum=255, integer=True)

        for field in ("emergency_cpu_c", "emergency_nvme_c"):
            _number(control[field], field, minimum=0, maximum=120)

        validate_levels("cpu_levels", control["cpu_levels"])
        validate_levels("nvme_levels", control["nvme_levels"])

        if not isinstance(health["enabled"], bool):
            raise ValueError("health.enabled должен быть true/false")

        _number(health["min_pwm_for_check"], "min_pwm_for_check", minimum=0, maximum=255, integer=True)
        _number(health["min_rpm_ratio"], "min_rpm_ratio", minimum=.001, maximum=1)
        _number(health["warn_after_seconds"], "warn_after_seconds", minimum=10, maximum=300)

        if not isinstance(health["baseline_rpm"], dict):
            raise ValueError("baseline_rpm должен быть объектом JSON")

        for pwm, rpm in health["baseline_rpm"].items():
            _number(pwm, "baseline PWM", minimum=0, maximum=255, integer=True)
            _number(rpm, "baseline RPM", minimum=1, maximum=30000)

        if not isinstance(failsafe["tach_fail_safe_enabled"], bool):
            raise ValueError("tach_fail_safe_enabled должен быть true/false")

        for field in ("sensor_failure_pwm", "tach_zero_min_pwm", "tach_failure_pwm"):
            _number(failsafe[field], field, minimum=0, maximum=255, integer=True)
        _number(failsafe["tach_zero_seconds"], "tach_zero_seconds", minimum=0, maximum=600)

    except (KeyError, TypeError) as exc:
        raise ValueError(f"Отсутствует или неверно задано поле: {exc}") from exc
    return cfg


# ------------------------------------------------------------
# Блокировка и резервная копия
# ------------------------------------------------------------
class ConfigSession:
    """
    Одна транзакция: загрузка -> изменение -> проверка -> запись.

    ! При калибровке сессия удерживает lock до восстановления режима.
    """
    def __init__(self):
        self.backed_up = False

    def load(self):
        return validate_config(load_cfg())

    def save(self, cfg):
        """
        Проверка и атомарная запись с сохранением предыдущего config.json.
        """
        validate_config(cfg)
        if not self.backed_up:
            shutil.copy2(CONFIG, CONFIG.with_suffix(".json.bak"))
            self.backed_up = True

        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=CONFIG.parent)
        try:
            os.fchmod(fd, CONFIG.stat().st_mode & 0o777)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(cfg, file, ensure_ascii=False, indent=2, allow_nan=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, CONFIG)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


@contextmanager
def edit_session():
    """
    Писатель конфигурации.

    ! Конкурирующие fanctl-команды не допускаются и получают ошибку.
    """
    lock_path = CONFIG.with_name(".config.lock")
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Настройки заняты другим изменением/калибровкой") from None
        yield ConfigSession()
    finally:
        os.close(fd)


def edit_config(change):
    """
    Общая процедура изменения настройки; чтение тоже под блокировкой.
    """
    with edit_session() as session:
        cfg = session.load()
        change(cfg)
        session.save(cfg)
    time.sleep(1.2)  # daemon перечитывает config.json в следующем цикле


# ------------------------------------------------------------
# Mode
# ------------------------------------------------------------
def set_mode(mode):
    """
    Режим: AUTO / MANUAL
    """
    if mode not in ("auto", "manual"):
        raise SystemExit("mode: auto или manual")
    def change(cfg):
        cfg["control"]["mode"] = mode
    edit_config(change)


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
    def change(cfg):
        cfg["control"]["manual_pwm"] = pwm
        cfg["control"]["mode"] = "manual"
    edit_config(change)


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

    ! Температуры и PWM, перед записью, проверяются валидатором validate_config.
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

    def change(cfg):
        levels = [list(item) for item in cfg["control"][key]]
        if not 0 <= index < len(levels):
            raise ValueError("Неверный LEVEL")
        levels[index] = [temp, pwm]
        cfg["control"][key] = levels

    edit_config(change)
