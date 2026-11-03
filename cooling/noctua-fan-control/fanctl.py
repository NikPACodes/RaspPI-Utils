#!/usr/bin/env python3
"""
CLI для noctua_daemon.py.

fanctl НЕ управляет GPIO напрямую.

Он:
- читает status.json;
- показывает CPU/NVMe/PWM/RPM;
- показывает обе температурные кривые;
- меняет config.json;
- переключает AUTO/MANUAL;
- задаёт manual PWM;
- редактирует CPU/NVMe LEVEL;
- выполняет baseline-калибровку;
- показывает доступные данные CPU/HAT fan.

Единственным владельцем PWM остаётся
noctua_daemon.py.
"""
from __future__ import annotations

import json, os, statistics, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent

CONFIG = BASE / "config.json"
STATUS = BASE / "status.json"
BOOTCFG = Path("/boot/firmware/config.txt")


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
# Display helpers
# ------------------------------------------------------------
def pct(pwm):
    """
    PWM 0..255 -> проценты
    """
    return float(pwm) * 100 / 255


def show_value(value, suffix=""):
    if value is None:
        return "—"
    return f"{value}{suffix}"


def print_curve(title, levels):
    """
    Температурная кривая.
    """
    print(f"\n{title}")
    print("─" * 76)
    for index, (temp, pwm) in enumerate(levels):
        pwm = int(pwm)
        print(f"LEVEL {index:02d}: {float(temp):5.1f}°C -> PWM {pwm:3d}/255 ({pct(pwm):5.1f}%)")


def level_name(temp, levels):
    """
    Положение температуры между LEVEL.
    """
    if temp is None:
        return "—"
    points = [ (float(temp), int(pwm)) for temp, pwm in levels ]

    if temp <= points[0][0]:
        return "LEVEL 0"

    for i in range(1, len(points)):
        if temp <= points[i][0]:
            return f"LEVEL {i-1}->{i}"
    return f"LEVEL {len(points)-1}"


def system_model():
    """
    Модель Raspberry Pi
    """
    path = Path("/proc/device-tree/model")
    try:
        return path.read_text().replace("\x00", "").strip()
    except OSError:
        return "Unknown Raspberry Pi"


# ------------------------------------------------------------
# Status
# ------------------------------------------------------------
def status(clear=False):
    """
    Текущее состояние Noctua
    """
    if clear:
        print("\033[2J\033[H", end="")

    cfg = load_cfg()
    state = read_json(STATUS)
    fan_name = cfg.get("fan", {}).get("name", "Unknown Fan")
    hostname = os.uname().nodename

    age = time.time() - float(state["timestamp"])
    maintenance_code = state.get("maintenance")
    maintenance = {
        "OK": "OK",
        "OBSERVE": "RPM ниже baseline — наблюдение",
        "CHECK_FAN": "ПРОВЕРИТЬ ВЕНТИЛЯТОР / ФИЛЬТР / ПИТАНИЕ",
        "NO_BASELINE": "нет baseline — выполни calibrate",
        "NO_BASELINE_FOR_PWM": "для этого PWM нет baseline",
        "LOW_PWM": "PWM слишком низкий для диагностики",
        "DISABLED": "диагностика отключена",
    }.get(
        maintenance_code,
        maintenance_code or "—",
    )

    ratio = state.get("rpm_ratio")
    if ratio is None:
        ratio_text = "—"
    else:
        ratio_text = f"{ratio * 100:.1f}% baseline"

    alarms = state.get("alarms") or []

    print(fan_name)
    print(f"Host: {hostname}")
    print(f"Board: {system_model()}")
    print("═" * 76)
    if not state:
        print("Нет status.json. Сначала запусти: sudo python3 noctua_daemon.py")
        print_curve("CPU levels", cfg["control"]["cpu_levels"])
        print_curve("NVMe levels", cfg["control"]["nvme_levels"])
        return
    print(f"Status age:          {age:.1f} s")
    print(f"Mode:                {state['mode']}")
    print(f"PWM сейчас:          {state['applied_pwm']}/255 ({pct(state['applied_pwm']):.1f}%)")
    print(f"PWM target:          {state['target_pwm']}/255 ({pct(state['target_pwm']):.1f}%)")
    print(f"Control source:      {state.get('control_source', '—')}")
    print(f"RPM:                 {state['rpm']} rpm")
    print(f"Expected RPM:        {show_value(state.get('expected_rpm'), ' rpm')}")
    print(f"RPM health:          {ratio_text}")
    print(f"Maintenance:         {maintenance}")

    # --------------------------------------------------------
    # Temperatures
    # --------------------------------------------------------
    print("\nТемпературы")
    print("─" * 76)
    print(f"CPU temp:            {show_value(state.get('cpu_temp_c'), '°C')}")
    print(f"NVMe сейчас:         {show_value(state.get('nvme_temp_current_c'), '°C')}")
    print(f"NVMe средняя:        {show_value(state.get('nvme_temp_avg_c'), '°C')}")
    print(f"NVMe samples:        {state.get('nvme_average_samples_used','—')} / {state.get('nvme_average_samples_target','—')}")

    # --------------------------------------------------------
    # Cooling requests
    # --------------------------------------------------------
    print("\nЗапросы охлаждения")
    print("─" * 76)
    print(f"CPU request:         {show_value(state.get('pwm_cpu_requested'), '/255')}")
    print(f"NVMe request:        {show_value(state.get('pwm_nvme_requested'), '/255')}")
    print(f"CPU level:           {level_name(state.get('cpu_latched_temp_c'), cfg['control']['cpu_levels'])}")
    print(f"NVMe level:          {level_name(state.get('nvme_latched_temp_c'), cfg['control']['nvme_levels'])}")

    # --------------------------------------------------------
    # Interface
    # --------------------------------------------------------
    print("\nИнтерфейс")
    print("─" * 76)
    print(f"GPIO PWM/TACH:       GPIO{state['pwm_gpio']} / GPIO{state['tach_gpio']}")
    print(f"PWM frequency:       {state['pwm_frequency_hz']} Hz")
    print(f"GPIO chip:           gpiochip{state.get('gpiochip', '—')}")
    print(f"Alarms:              {', '.join(alarms) if alarms else 'нет'}")
    print_curve("CPU levels", cfg["control"]["cpu_levels"])
    print_curve("NVMe levels", cfg["control"]["nvme_levels"])


# ------------------------------------------------------------
# Live monitor
# ------------------------------------------------------------
def watch(period=1.0):
    """
    Обновление status в реальном времени.
    """
    try:
        while True:
            status(True)
            print("\nCtrl+C — выход")
            time.sleep(period)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------
# Settings
# ------------------------------------------------------------
def settings():
    """
    Конфигурация
    """
    cfg = load_cfg()
    control = cfg["control"]
    health = cfg["health"]
    failsafe = cfg["failsafe"]

    baseline = health.get("baseline_rpm",{})

    print("Настройки Noctua")
    print("═" * 76)
    print(f"Mode:                {control['mode']}")
    print(f"Update interval:     {control['update_interval_s']} s")
    print(f"NVMe average samples:{control['nvme_average_samples']}")
    print(f"Hysteresis:          {control['hysteresis_c']}°C")
    print(f"Minimum dwell:       {control['minimum_dwell_s']} s")
    print(f"Min running PWM:     {control['min_running_pwm']}/255")
    print(f"Kickstart:           {control['kickstart_seconds']} s @ 255/255")
    print(f"Manual PWM:          {control['manual_pwm']}/255 ({pct(control['manual_pwm']):.1f}%)")
    print(f"Emergency CPU:       {control['emergency_cpu_c']}°C")
    print(f"Emergency NVMe:      {control['emergency_nvme_c']}°C")
    print_curve("CPU levels", control["cpu_levels"],)
    print_curve("NVMe levels", control["nvme_levels"])

    # --------------------------------------------------------
    # Health
    # --------------------------------------------------------
    print("\nHealth")
    print("─" * 76)
    print(f"Enabled:             {health['enabled']}")
    print(f"Min PWM for check:   {health['min_pwm_for_check']}/255")
    print(f"Min RPM ratio:       {float(health['min_rpm_ratio']) * 100:.0f}%")
    print(f"Warning delay:       {health['warn_after_seconds']} s")

    # --------------------------------------------------------
    # Failsafe
    # --------------------------------------------------------
    print("\nFailsafe")
    print("─" * 76)
    print(f"Sensor failure PWM:  {failsafe['sensor_failure_pwm']}/255")
    print(f"TACH enabled:        {failsafe['tach_fail_safe_enabled']}")
    print(f"TACH zero timeout:   {failsafe['tach_zero_seconds']} s")
    print(f"TACH check from PWM: {failsafe['tach_zero_min_pwm']}/255")
    print(f"TACH failure PWM:    {failsafe['tach_failure_pwm']}/255")

    # --------------------------------------------------------
    # Baseline
    # --------------------------------------------------------
    print("\nBaseline RPM")
    print("─" * 76)
    if not baseline:
        print("нет — запусти: sudo python3 fanctl.py calibrate")
    else:
        rows = sorted((int(pwm), int(rpm)) for pwm, rpm in baseline.items())
        print(f"{'PWM':>7}  {'Duty':>7}  {'RPM':>6}")
        print("─" * 26)
        for pwm, rpm in rows:
            print(f"{pwm:3d}/255  {pct(pwm):6.1f}%  {rpm:6d}")


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
def set_level(i, temp, pwm):
    i, temp, pwm = int(i), float(temp), int(pwm)
    cfg = load_cfg(); levels = [list(x) for x in cfg["control"]["levels"]]
    if not 0 <= i < len(levels) or not 0 <= pwm <= 255:
        raise SystemExit("Неверный LEVEL или PWM")
    levels[i] = [temp, pwm]
    ts = [float(x[0]) for x in levels]
    if ts != sorted(ts) or len(ts) != len(set(ts)):
        raise SystemExit("Температуры LEVEL должны строго возрастать")
    cfg["control"]["levels"] = levels; save_cfg(cfg)


def set_level(sensor, index, temp, pwm):
    """
    Изменить одну точку кривой.
    sensor:
        cpu
        nvme
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


# ------------------------------------------------------------
# RPM helper
# ------------------------------------------------------------
def current_rpm():
    """
    Текущий RPM из status.json.
    """
    state = read_json(STATUS)
    if not state:
        return None

    age = (time.time() - float(state.get("timestamp", 0)))
    if age > 3:
        return None

    try:
        return int(state["rpm"])
    except (KeyError, TypeError, ValueError,):
        return None


# ------------------------------------------------------------
# Baseline calibration
# ------------------------------------------------------------
def calibrate():
    """
    Калибровка RPM

    Используются рабочие PWM из CPU/NVMe curves.

    ! Из-за особенностей сборки PWM=255 автоматически исключён.
    ! При PWM=255 обороты Noctua начинают превышать максимальные на ~10-12%
    ! Это может привести к быстрому износу компонентов.
    """
    cfg = load_cfg()
    old_mode = cfg["control"]["mode"]
    old_pwm = int(cfg["control"]["manual_pwm"])
    min_pwm = int(cfg["health"]["min_pwm_for_check"])

    pwms = {
        int(pwm)
        for key in ("cpu_levels", "nvme_levels")
        for _, pwm in cfg["control"][key] if int(pwm) >= min_pwm and int(pwm) < 255
    }
    pwms = sorted(pwms)

    print("Калибровка baseline RPM.")
    print("Выполняй на чистом вентиляторе в собранном корпусе с установленной сеткой.")
    print("PWM 255 автоматически НЕ тестируется.")
    answer = input("Продолжить? [y/N]: ").strip().lower()
    if answer not in ("y", "yes", "д", "да"):
        return

    baseline = {}
    try:
        for pwm in pwms:
            cfg = load_cfg()
            cfg["control"]["mode"] = "manual"
            cfg["control"]["manual_pwm"] = pwm
            save_cfg(cfg)

            print(f"PWM {pwm}/255: стабилизация 8 секунд...")
            time.sleep(8)

            samples = []
            for _ in range(5):
                rpm = current_rpm()

                if rpm is not None and rpm > 0:
                    samples.append(rpm)
                time.sleep(1)

            if samples:
                value = int(round(statistics.median(samples)))
                baseline[str(pwm)] = value
                print(f"  baseline = {value} rpm")
            else:
                print("  RPM не получен")
        cfg = load_cfg()
        cfg["health"]["baseline_rpm"] = baseline
        save_cfg(cfg)
    finally:
        # Возвращен исходный режим
        cfg = load_cfg()
        cfg["control"]["mode"] = old_mode
        cfg["control"]["manual_pwm"] = old_pwm
        save_cfg(cfg)


# ------------------------------------------------------------
# CPU/HAT fan
# ------------------------------------------------------------
def cpu_fan_info():
    """
    Информация CPU/HAT fan (read-only)

    Показываем только реально найденные hwmon/config.txt данные.
    """
    rpm = None
    pwm_raw = None
    source = None

    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            has_rpm = (hw / "fan1_input").exists()
            has_pwm = (hw / "pwm1").exists()
            if not (has_rpm or has_pwm ):
                continue

            if has_rpm:
                rpm = int((hw / "fan1_input").read_text().strip())

            if has_pwm:
                pwm_raw = int((hw / "pwm1").read_text().strip())

            try:
                source = (hw / "name").read_text().strip()
            except OSError:
                source = hw.name
            break
        except (OSError, ValueError):
            continue

    config_lines = []
    if BOOTCFG.exists():
        try:
            for raw in BOOTCFG.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "fan_temp" in line:
                    config_lines.append(line)
        except OSError:
            pass

    if pwm_raw is None:
        pwm_text = "—"
    else:
        pwm_text = f"{pwm_raw}/255 ({pct(pwm_raw):.1f}%)"

    print("CPU/HAT fan (read-only)")
    print("═" * 76)
    print(f"Источник hwmon:       {source or '—'}")
    print(f"RPM:                  {show_value(rpm, ' rpm')}")
    print(f"PWM:                  {pwm_text}")
    print("\nfan_temp* из config.txt")
    print("─" * 76)

    if config_lines:
        for line in config_lines:
            print(line)
    else:
        print("явные настройки не найдены")


# ------------------------------------------------------------
# Menu
# ------------------------------------------------------------
def menu():
    """
    Интерактивное меню
    """
    while True:
        print()
        status()
        print("\nМеню")
        print("1 — Live monitor")
        print("2 — Настройки")
        print("3 — AUTO")
        print("4 — MANUAL PWM")
        print("5 — Изменить LEVEL CPU/NVMe")
        print("6 — Calibrate baseline")
        print("7 — CPU/HAT fan")
        print("q — выход")
        choice = input("> ").strip().lower()
        if choice == "1":
            watch()
        elif choice == "2":
            settings()
            input("\nEnter...")
        elif choice == "3":
            set_mode("auto")
        elif choice == "4":
            set_pwm(input("PWM 0..255: ").strip())
        elif choice == "5":
            cfg = load_cfg()
            sensor = input("Кривая [cpu/nvme]: ").strip().lower()
            if sensor not in ("cpu", "nvme"):
                print("Нужно cpu или nvme")
                continue
            key = f"{sensor}_levels"
            print_curve(f"{sensor.upper()} levels", cfg["control"][key])
            index = input("LEVEL: ").strip()
            current = cfg["control"][key][int(index)]
            temp = input(f"Температура [{current[0]}]: ").strip()
            if not temp:
                temp = str(current[0])
            pwm = input(f"PWM [{current[1]}]: ").strip()
            if not pwm:
                pwm = str(current[1])
            set_level(sensor, index, temp, pwm)
        elif choice == "6":
            calibrate()
        elif choice == "7":
            cpu_fan_info()
            input("\nEnter...")
        elif choice in ("q", "quit", "exit"):
            return


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def usage():
    print("""
Команды:

  python3 fanctl.py status

  python3 fanctl.py watch

  python3 fanctl.py settings

  python3 fanctl.py cpu


AUTO:

  python3 fanctl.py mode auto


MANUAL:

  python3 fanctl.py pwm 128


Изменить CPU LEVEL:

  python3 fanctl.py level cpu 3 60 115


Изменить NVMe LEVEL:

  python3 fanctl.py level nvme 3 50 153


Калибровка:

  python3 fanctl.py calibrate


Интерактивное меню:

  python3 fanctl.py
""")


def main():
    args = sys.argv[1:]
    if not args:
        menu()
        return 0
    command = (args[0].lower())
    if command == "status":
        status()
    elif command == "watch":
        period = float(args[1]) if len(args) > 1 else 1.0
        watch(period)
    elif command == "settings":
        settings()
    elif command == "cpu":
        cpu_fan_info()
    elif command == "mode" and len(args) == 2:
        set_mode(args[1])
    elif command == "pwm" and len(args) == 2:
        set_pwm(args[1])
    elif command == "level" and len(args) == 5:
        set_level(args[1], args[2], args[3], args[4])
    elif command == "calibrate":
        calibrate()
    else:
        usage()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())