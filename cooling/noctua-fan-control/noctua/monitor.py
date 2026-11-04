"""
Отображение состояния и настроек Noctua Fan Control.

Используется для:
- отображения status.json;
- live-мониторинга;
- отображения температур CPU и NVMe;
- отображения PWM и RPM;
- отображения запросов PWM от CPU и NVMe;
- отображения температурных кривых;
- отображения health/failsafe состояния;
- просмотра доступных данных CPU/HAT fan в режиме read-only.

Данные Noctua берутся из status.json, который формирует noctua_daemon.
"""
import os, time
from pathlib import Path

from noctua.config import read_json, load_cfg
from noctua.paths import STATUS, BOOTCFG

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

    print(fan_name)
    print(f"Host: {hostname}")
    print(f"Board: {system_model()}")
    print("═" * 76)

    if not state:
        print("Нет status.json. Сначала запусти: sudo python3 noctua_daemon.py")
        print_curve("CPU levels", cfg["control"]["cpu_levels"])
        print_curve("NVMe levels", cfg["control"]["nvme_levels"])
        return

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
    fan_name = cfg.get("fan", {}).get("name", "Unknown Fan")
    baseline = health.get("baseline_rpm",{})

    print(f"Настройки {fan_name}")
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