#!/usr/bin/env python3
"""
Основной daemon управления Noctua.

Daemon — единственный процесс управляющий PWM.

AUTO:
- CPU -> cpu_levels -> PWM_CPU;
- последние N измерений NVMe -> среднее -> nvme_levels -> PWM_NVMe;
- итоговый запрос = max(PWM_CPU, PWM_NVMe).

Дополнительно:
- гистерезис и minimum dwell;
- кратковременный kick-start при переходе 0 -> ненулевой PWM;
- RPM по TACH;
- health относительно baseline RPM;
- failsafe при ошибках датчиков/TACH;
- PWM=255 при температурном emergency;
- атомарная запись status.json;
- hot reload программных параметров config.json.
"""
from __future__ import annotations

import json
import os
import signal
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from gpiozero import DigitalInputDevice, PWMOutputDevice
from gpiozero.pins.lgpio import LGPIOFactory

from noctua.config import load_cfg
from noctua.paths import CONFIG, STATUS

STOP = threading.Event()


# ------------------------------------------------------------
# Общие функции
# ------------------------------------------------------------
def clamp(value: float, low: float, high: float) -> float:
    """
    Ограничение диапазона low ... high.
    """
    return max(low, min(high, value))


def read_float(path: Path, scale: float = 1.0) -> float | None:
    """
    Чтение числового значения sysfs.
    """
    try:
        return float(path.read_text().strip()) / scale
    except (OSError, ValueError):
        return None


def cpu_temp_c() -> float | None:
    """
    Получение температуры CPU Raspberry Pi.
    """
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            zone_type = (zone / "type").read_text().strip().lower()
        except OSError:
            continue
        if zone_type == "cpu-thermal":
            return read_float(zone / "temp", 1000.0)
    return read_float(Path("/sys/class/thermal/thermal_zone0/temp"), 1000.0)


def nvme_current_temp_c() -> tuple[float | None, list[float]]:
    """
    Получение температуры NVMe.

    Для каждого NVMe предпочтительно используется Composite.
    Если Composite отсутствует — первый доступный temp*_input.
    Если NVMe несколько — для управления берётся самый горячий.
    """
    device_temps: list[float] = []

    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip().lower() != "nvme":
                continue
        except OSError:
            continue

        sensors: list[tuple[str, float]] = []

        for input_path in sorted(hw.glob("temp*_input")):
            temp = read_float(input_path, 1000.0)
            if temp is None or not -20 <= temp <= 120:
                continue

            stem = input_path.name.removesuffix("_input")
            try:
                label = (hw / f"{stem}_label").read_text().strip()
            except OSError:
                label = ""

            sensors.append((label, temp))

        if not sensors:
            continue

        composite = [temp for label, temp in sensors if label.lower() == "composite"]
        device_temps.append(composite[0] if composite else sensors[0][1])

    if not device_temps:
        return None, []

    return max(device_temps), device_temps


def find_rp1_gpiochip() -> int:
    """
    Поиск GPIO-контроллера RP1 по label.
    """
    for path in Path("/sys/class/gpio").glob("gpiochip*"):
        try:
            label = (path / "label").read_text().strip().lower()
        except OSError:
            continue

        if "pinctrl-rp1" in label:
            return int(path.name.removeprefix("gpiochip"))
    raise RuntimeError("Не найден GPIO-контроллер RP1 по label pinctrl-rp1")


# ------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------
def validate_levels(name: str, levels: list[list[Any]]) -> None:
    """
    Проверка температурной кривой [temperature, PWM].
    """
    if not levels:
        raise ValueError(f"{name} пуст")

    previous_temp = float("-inf")
    previous_pwm = -1

    for row in levels:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError(f"{name}: каждая точка должна быть [temperature, PWM]")

        temp = float(row[0])
        pwm = int(row[1])

        if temp <= previous_temp:
            raise ValueError(f"{name}: температуры должны строго возрастать")
        if not 0 <= pwm <= 255:
            raise ValueError(f"{name}: PWM должен быть 0..255")
        if pwm < previous_pwm:
            raise ValueError(f"{name}: PWM не должен уменьшаться с ростом температуры")

        previous_temp = temp
        previous_pwm = pwm


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Проверка параметров config.json
    """
    gpio = cfg["gpio"]
    control = cfg["control"]
    health = cfg["health"]
    failsafe = cfg["failsafe"]

    if control["mode"] not in ("auto", "manual"):
        raise ValueError("mode должен быть auto или manual")
    if int(gpio["pwm_gpio"]) == int(gpio["tach_gpio"]):
        raise ValueError("pwm_gpio и tach_gpio не должны совпадать")
    if int(gpio["pwm_frequency_hz"]) <= 0:
        raise ValueError("pwm_frequency_hz должен быть > 0")
    if int(gpio["tach_pulses_per_rev"]) <= 0:
        raise ValueError("tach_pulses_per_rev должен быть > 0")
    if float(control["update_interval_s"]) <= 0:
        raise ValueError("update_interval_s должен быть > 0")
    if int(control["nvme_average_samples"]) < 1:
        raise ValueError("nvme_average_samples должен быть >= 1")
    if float(control["hysteresis_c"]) < 0:
        raise ValueError("hysteresis_c должен быть >= 0")
    if float(control["minimum_dwell_s"]) < 0:
        raise ValueError("minimum_dwell_s должен быть >= 0")
    if float(control["kickstart_seconds"]) < 0:
        raise ValueError("kickstart_seconds должен быть >= 0")

    for key in ("min_running_pwm", "manual_pwm"):
        if not 0 <= int(control[key]) <= 255:
            raise ValueError(f"{key} должен быть 0..255")

    validate_levels("cpu_levels", control["cpu_levels"])
    validate_levels("nvme_levels", control["nvme_levels"])

    if not 0 < float(health["min_rpm_ratio"]) <= 1:
        raise ValueError("min_rpm_ratio должен быть > 0 и <= 1]")
    if float(health["warn_after_seconds"]) < 0:
        raise ValueError("warn_after_seconds должен быть >= 0")
    if not 0 <= int(health["min_pwm_for_check"]) <= 255:
        raise ValueError("min_pwm_for_check должен быть 0..255")

    for key in ("sensor_failure_pwm", "tach_zero_min_pwm", "tach_failure_pwm"):
        if not 0 <= int(failsafe[key]) <= 255:
            raise ValueError(f"{key} должен быть 0..255")

    if float(failsafe["tach_zero_seconds"]) < 0:
        raise ValueError("tach_zero_seconds должен быть >= 0")

    for pwm, rpm in health.get("baseline_rpm", {}).items():
        if not 0 <= int(pwm) <= 255 or float(rpm) <= 0:
            raise ValueError("baseline_rpm содержит некорректные PWM/RPM")

    return cfg


def load_daemon_config() -> dict[str, Any]:
    """
    Загрузка и проверка config.json"""
    return validate_config(load_cfg())


# ------------------------------------------------------------
# Кривые
# ------------------------------------------------------------
def interp_pwm(temp: float, levels: list[list[Any]]) -> int:
    """
    Расчет PWM по температуре с линейной интерполяцией.
    """
    points = [(float(t), int(p)) for t, p in levels]

    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]

    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            k = (temp - t0) / (t1 - t0)
            return int(round(p0 + k * (p1 - p0)))

    return points[-1][1]


def interp_rpm(pwm: int, baseline: dict[str, Any]) -> float | None:
    """
    Расчет ожидаемого RPM внутри откалиброванного baseline.
    """
    points = sorted((int(key), float(value)) for key, value in baseline.items() if float(value) > 0)

    if not points or pwm < points[0][0] or pwm > points[-1][0]:
        return None

    for point_pwm, point_rpm in points:
        if pwm == point_pwm:
            return point_rpm

    for (p0, r0), (p1, r1) in zip(points, points[1:]):
        if p0 < pwm < p1:
            k = (pwm - p0) / (p1 - p0)
            return r0 + k * (r1 - r0)

    return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """
    Атомарная записать JSON.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ============================================================
# Controller
# ============================================================
class Controller:
    """
    Единственный владелец PWM и TACH Noctua.
    """
    def __init__(self) -> None:
        self.cfg = load_daemon_config()
        self.cfg_mtime = CONFIG.stat().st_mtime_ns

        gpio = self.cfg["gpio"]
        initial_pwm = int(self.cfg["failsafe"]["sensor_failure_pwm"])

        self.hardware_signature = (int(gpio["pwm_gpio"]),
                                   int(gpio["tach_gpio"]),
                                   int(gpio["pwm_frequency_hz"]))

        self.gpiochip = find_rp1_gpiochip()
        self.factory = LGPIOFactory(chip=self.gpiochip)

        self.pwm = PWMOutputDevice(int(gpio["pwm_gpio"]),
                                   active_high=True,
                                   initial_value=initial_pwm / 255.0,
                                   frequency=int(gpio["pwm_frequency_hz"]),
                                   pin_factory=self.factory)
        self.tach = DigitalInputDevice(int(gpio["tach_gpio"]),
                                       pull_up=None,
                                       active_state=False,
                                       bounce_time=None,
                                       pin_factory=self.factory)

        self.lock = threading.Lock()
        self.pulses = 0
        self.tach.when_activated = self._tach_pulse
        self.last_rpm_time = time.monotonic()
        self.rpm_ema = 0.0
        self.zero_since: float | None = None
        self.applied_pwm = initial_pwm
        self.nvme_history: deque[float] = deque(maxlen=int(self.cfg["control"]["nvme_average_samples"]))
        self.cpu_latched_temp: float | None = None
        self.nvme_latched_temp: float | None = None
        self.last_policy_change_time = time.monotonic()
        self.low_rpm_since: float | None = None
        self.started = time.time()

    # --------------------------------------------------------
    # TACH / RPM
    # --------------------------------------------------------
    def _tach_pulse(self) -> None:
        """
        Подсчет одного активного импульса TACH.
        """
        with self.lock:
            self.pulses += 1


    def update_rpm(self) -> None:
        """
        Пересчет RPM.

        RPM = pulses * 60 / (measurement_seconds * pulses_per_rev)
        """
        now = time.monotonic()
        dt = now - self.last_rpm_time
        if dt < 0.8:
            return

        with self.lock:
            pulses = self.pulses
            self.pulses = 0

        ppr = int(self.cfg["gpio"]["tach_pulses_per_rev"])
        rpm_raw = pulses * 60.0 / (dt * ppr)

        if self.rpm_ema == 0.0 and rpm_raw > 0:
            self.rpm_ema = rpm_raw
        else:
            self.rpm_ema = 0.35 * rpm_raw + 0.65 * self.rpm_ema

        tach_min_pwm = int(self.cfg["failsafe"]["tach_zero_min_pwm"])
        if self.applied_pwm < tach_min_pwm:
            self.zero_since = None
        elif rpm_raw <= 0:
            self.zero_since = self.zero_since or now
        else:
            self.zero_since = None

        self.last_rpm_time = now


    # --------------------------------------------------------
    # Config reload
    # --------------------------------------------------------
    def reload_config(self) -> None:
        """
        Перечитывание config.json при изменении.
        """
        try:
            mtime = CONFIG.stat().st_mtime_ns
        except OSError:
            return

        if mtime == self.cfg_mtime:
            return

        new_cfg = load_daemon_config()
        new_gpio = new_cfg["gpio"]
        new_signature = (int(new_gpio["pwm_gpio"]), int(new_gpio["tach_gpio"]), int(new_gpio["pwm_frequency_hz"]))

        if new_signature != self.hardware_signature:
            raise RuntimeError("Изменены pwm_gpio/tach_gpio/pwm_frequency_hz. "
                               "Для применения аппаратных параметров требуется перезапуск daemon.")

        sample_count = int(new_cfg["control"]["nvme_average_samples"])
        old_samples = list(self.nvme_history)
        self.nvme_history = deque(old_samples[-sample_count:], maxlen=sample_count)

        self.cfg = new_cfg
        self.cfg_mtime = mtime
        print("[noctua] config reloaded", flush=True)


    # --------------------------------------------------------
    # Температуры / hysteresis
    # --------------------------------------------------------
    def read_temperatures(self) -> tuple[float | None, float | None, float | None, list[float]]:
        """
        Чтение температуры CPU, текущую NVMe и среднюю NVMe по последним N измерениям.
        """
        cpu = cpu_temp_c()
        nvme_current, nvme_devices = nvme_current_temp_c()
        if nvme_current is not None:
            self.nvme_history.append(nvme_current)
        nvme_average = statistics.fmean(self.nvme_history) if self.nvme_history else None
        return cpu, nvme_current, nvme_average, nvme_devices


    def hysteresis_temp(self, current: float | None,
                              latched: float | None) -> tuple[float | None, bool]:
        """
        Применение гистерезис к управляющей температуре.

        Изменения меньше hysteresis_c игнорируются в обе стороны.
        Рост >= hysteresis_c принимается сразу.
        Снижение >= hysteresis_c принимается только после minimum_dwell_s.
        Emergency проверяется отдельно по текущей температуре.
        """
        if current is None:
            return latched, False
        if latched is None:
            return current, False

        control = self.cfg["control"]
        hysteresis = float(control["hysteresis_c"])
        dwell = float(control["minimum_dwell_s"])

        if current >= latched + hysteresis:
            self.last_policy_change_time = time.monotonic()
            return current, False

        enough_time = time.monotonic() - self.last_policy_change_time >= dwell
        if current <= latched - hysteresis and enough_time:
            return current, True

        return latched, False


    # --------------------------------------------------------
    # PWM policy
    # --------------------------------------------------------
    def apply_min_running_pwm(self, target: int) -> int:
        """
        Разрешить 0, но поднять слишком малый ненулевой PWM до min_running_pwm.
        """
        minimum = int(self.cfg["control"]["min_running_pwm"])
        return minimum if 0 < target < minimum else target


    def apply_tach_failsafe(self, target: int) -> tuple[int, str | None]:
        """
        Применение TACH failsafe без снижения уже более высокого target.
        """
        failsafe = self.cfg["failsafe"]

        if not failsafe["tach_fail_safe_enabled"]:
            return target, None
        if target < int(failsafe["tach_zero_min_pwm"]):
            return target, None
        if self.zero_since is None:
            return target, None
        if time.monotonic() - self.zero_since < float(failsafe["tach_zero_seconds"]):
            return target, None

        target = max(target, int(failsafe["tach_failure_pwm"]))
        return target, "TACH=0 при ненулевом PWM"


    def choose_pwm(self, cpu: float | None,
                         nvme_current: float | None,
                         nvme_average: float | None) -> tuple[int, int | None, int | None, str, list[str]]:
        """
        Расчет итогового PWM и источника запроса.
        """
        control = self.cfg["control"]
        failsafe = self.cfg["failsafe"]
        alarms: list[str] = []

        # Emergency использует текущие температуры, чтобы averaging/hysteresis
        # не задерживали защитное повышение охлаждения.
        if cpu is not None and cpu >= float(control["emergency_cpu_c"]):
            target, tach_alarm = self.apply_tach_failsafe(255)
            alarms.append("CPU emergency")
            source = "CPU_EMERGENCY"
            if tach_alarm:
                alarms.append(tach_alarm)
                source += "+TACH_FAILSAFE"
            return target, 255, None, source, alarms

        if nvme_current is not None and nvme_current >= float(control["emergency_nvme_c"]):
            target, tach_alarm = self.apply_tach_failsafe(255)
            alarms.append("NVMe emergency")
            source = "NVME_EMERGENCY"
            if tach_alarm:
                alarms.append(tach_alarm)
                source += "+TACH_FAILSAFE"
            return target, None, 255, source, alarms

        missing: list[str] = []
        if cpu is None:
            missing.append("CPU")
        if nvme_current is None:
            missing.append("NVMe")

        # MANUAL: ручной PWM, но sensor/TACH failsafe остаются активны.
        if control["mode"] == "manual":
            target = self.apply_min_running_pwm(int(control["manual_pwm"]))

            if missing:
                target = max(target, int(failsafe["sensor_failure_pwm"]))
                alarms.append("Нет датчика: " + ", ".join(missing))

            target, tach_alarm = self.apply_tach_failsafe(target)
            if tach_alarm:
                alarms.append(tach_alarm)

            source = "MANUAL"
            if missing:
                source += "+SENSOR_FAILSAFE"
            if tach_alarm:
                source += "+TACH_FAILSAFE"

            return int(clamp(target, 0, 255)), None, None, source, alarms

        # AUTO: независимые CPU/NVMe запросы.
        self.cpu_latched_temp, cpu_down = self.hysteresis_temp(cpu, self.cpu_latched_temp)
        self.nvme_latched_temp, nvme_down = self.hysteresis_temp(nvme_average, self.nvme_latched_temp)

        if cpu_down or nvme_down:
            self.last_policy_change_time = time.monotonic()

        pwm_cpu = (
            interp_pwm(self.cpu_latched_temp, control["cpu_levels"])
            if cpu is not None and self.cpu_latched_temp is not None
            else None
        )
        pwm_nvme = (
            interp_pwm(self.nvme_latched_temp, control["nvme_levels"])
            if nvme_current is not None and self.nvme_latched_temp is not None
            else None
        )

        requests = [pwm for pwm in (pwm_cpu, pwm_nvme) if pwm is not None]
        target = max(requests) if requests else 0

        # Failsafe — минимальный безопасный запрос, а не безусловная замена.
        if missing:
            target = max(target, int(failsafe["sensor_failure_pwm"]))
            alarms.append("Нет датчика: " + ", ".join(missing))

        target = self.apply_min_running_pwm(target)

        if pwm_cpu is not None and pwm_nvme is not None:
            if pwm_cpu > pwm_nvme:
                source = "CPU"
            elif pwm_nvme > pwm_cpu:
                source = "NVME"
            else:
                source = "CPU+NVME"
        elif pwm_cpu is not None:
            source = "CPU"
        elif pwm_nvme is not None:
            source = "NVME"
        else:
            source = "SENSOR_FAILSAFE"

        target, tach_alarm = self.apply_tach_failsafe(target)
        if tach_alarm:
            alarms.append(tach_alarm)
            source += "+TACH_FAILSAFE"

        return int(clamp(target, 0, 255)), pwm_cpu, pwm_nvme, source, alarms


    # --------------------------------------------------------
    # PWM output
    # --------------------------------------------------------

    def set_pwm(self, value: int) -> None:
        """
        Применение PWM.

        При 0 -> ненулевой PWM:
        255/255 на kickstart_seconds, затем рассчитанный target.
        """
        value = int(clamp(value, 0, 255))

        if self.applied_pwm == 0 and value > 0:
            self.pwm.value = 1.0
            time.sleep(float(self.cfg["control"]["kickstart_seconds"]))
            self.zero_since = time.monotonic()

        self.pwm.value = value / 255.0
        self.applied_pwm = value

        if value < int(self.cfg["failsafe"]["tach_zero_min_pwm"]):
            self.zero_since = None


    # --------------------------------------------------------
    # Health
    # --------------------------------------------------------
    def health(self) -> tuple[str, float | None, float | None]:
        """
        Сравнение текущего RPM с baseline для текущего PWM.
        """
        health = self.cfg["health"]

        if not health["enabled"]:
            self.low_rpm_since = None
            return "DISABLED", None, None

        baseline = health.get("baseline_rpm", {})
        if not baseline:
            self.low_rpm_since = None
            return "NO_BASELINE", None, None

        if self.applied_pwm < int(health["min_pwm_for_check"]):
            self.low_rpm_since = None
            return "LOW_PWM", None, None

        expected = interp_rpm(self.applied_pwm, baseline)
        if expected is None or expected <= 0:
            self.low_rpm_since = None
            return "NO_BASELINE_FOR_PWM", None, None

        ratio = self.rpm_ema / expected
        if ratio < float(health["min_rpm_ratio"]):
            self.low_rpm_since = self.low_rpm_since or time.monotonic()
            if (time.monotonic() - self.low_rpm_since >= float(health["warn_after_seconds"])):
                return "CHECK_FAN", expected, ratio
            return "OBSERVE", expected, ratio

        self.low_rpm_since = None
        return "OK", expected, ratio


    # --------------------------------------------------------
    # Status / loop
    # --------------------------------------------------------
    def write_status(self, *, cpu: float | None,
                              nvme_current: float | None,
                              nvme_average: float | None,
                              nvme_devices: list[float],
                              target: int,
                              pwm_cpu: int | None,
                              pwm_nvme: int | None,
                              source: str,
                              alarms: list[str],
                              maintenance: str,
                              expected_rpm: float | None,
                              rpm_ratio: float | None) -> None:
        """
        Запись runtime status для fanctl.
        """
        write_json_atomic(
            STATUS,
            {
                "timestamp": time.time(),
                "uptime_s": round(time.time() - self.started, 1),
                "mode": self.cfg["control"]["mode"],
                "gpiochip": self.gpiochip,
                "pwm_gpio": int(self.cfg["gpio"]["pwm_gpio"]),
                "tach_gpio": int(self.cfg["gpio"]["tach_gpio"]),
                "pwm_frequency_hz": int(self.cfg["gpio"]["pwm_frequency_hz"]),
                "target_pwm": target,
                "applied_pwm": self.applied_pwm,
                "rpm": int(round(self.rpm_ema)),
                "cpu_temp_c": None if cpu is None else round(cpu, 1),
                "nvme_temp_current_c": None if nvme_current is None else round(nvme_current, 1),
                "nvme_temp_avg_c": None if nvme_average is None else round(nvme_average, 1),
                "nvme_average_samples_used": len(self.nvme_history),
                "nvme_average_samples_target": int(self.cfg["control"]["nvme_average_samples"]),
                "nvme_device_temps_c": [round(value, 1) for value in nvme_devices],
                "cpu_latched_temp_c": None if self.cpu_latched_temp is None else round(self.cpu_latched_temp, 1),
                "nvme_latched_temp_c": None if self.nvme_latched_temp is None else round(self.nvme_latched_temp, 1),
                "pwm_cpu_requested": pwm_cpu,
                "pwm_nvme_requested": pwm_nvme,
                "control_source": source,
                "expected_rpm": None if expected_rpm is None else int(round(expected_rpm)),
                "rpm_ratio": None if rpm_ratio is None else round(rpm_ratio, 3),
                "maintenance": maintenance,
                "alarms": alarms,
            },
        )


    def loop(self) -> None:
        """
        Основной цикл daemon.
        """
        while not STOP.is_set():
            cycle_started = time.monotonic()

            self.reload_config()
            self.update_rpm()

            cpu, nvme_current, nvme_average, nvme_devices = self.read_temperatures()
            target, pwm_cpu, pwm_nvme, source, alarms = self.choose_pwm(cpu, nvme_current, nvme_average)

            if target != self.applied_pwm:
                self.set_pwm(target)

            maintenance, expected_rpm, rpm_ratio = self.health()
            self.write_status(
                cpu=cpu,
                nvme_current=nvme_current,
                nvme_average=nvme_average,
                nvme_devices=nvme_devices,
                target=target,
                pwm_cpu=pwm_cpu,
                pwm_nvme=pwm_nvme,
                source=source,
                alarms=alarms,
                maintenance=maintenance,
                expected_rpm=expected_rpm,
                rpm_ratio=rpm_ratio,
            )

            interval = float(self.cfg["control"]["update_interval_s"])
            elapsed = time.monotonic() - cycle_started
            STOP.wait(max(0.05, interval - elapsed))


    def close(self) -> None:
        """
        Завершение работы с GPIO.

        Перед закрытием выполняется best-effort переход на sensor_failure_pwm.
        Состояние линии после освобождения GPIO зависит от аппаратной схемы.
        """
        try:
            fallback = int(self.cfg["failsafe"]["sensor_failure_pwm"])
            self.pwm.value = fallback / 255.0
            time.sleep(0.2)
        except Exception:
            pass

        for obj in (self.tach, self.pwm, self.factory):
            try:
                obj.close()
            except Exception:
                pass


# ============================================================
# Main
# ============================================================
def stop_handler(*_: Any) -> None:
    """
    Обработка SIGINT/SIGTERM.
    """
    STOP.set()


def main() -> int:
    """
    Запуск daemon.
    """
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    controller: Controller | None = None
    try:
        controller = Controller()
        controller.loop()
        return 0
    except Exception as exc:
        print(f"[noctua] FATAL: {exc}", flush=True)
        return 1
    finally:
        if controller is not None:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())