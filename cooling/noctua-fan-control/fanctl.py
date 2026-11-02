#!/usr/bin/env python3
from __future__ import annotations

import json, os, statistics, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
STATUS = BASE / "status.json"
BOOTCFG = Path("/boot/firmware/config.txt")
CPU_DEFAULTS = [(50.0, 5.0, 75), (60.0, 5.0, 125), (67.5, 5.0, 175), (75.0, 5.0, 250)]


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_cfg():
    cfg = read_json(CONFIG)
    if cfg is None:
        raise SystemExit(f"Не могу прочитать {CONFIG}")
    return cfg


def save_cfg(cfg):
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG)
    time.sleep(1.2)


def pct(pwm):
    return float(pwm) * 100 / 255


def print_levels(cfg):
    print("\nУровни Noctua")
    print("─" * 72)
    for i, (temp, pwm) in enumerate(cfg["control"]["levels"]):
        print(f"LEVEL {i:02d}: {float(temp):5.1f}°C -> PWM {int(pwm):3d}/255 ({pct(pwm):5.1f}%)")


def level_name(temp, levels):
    if temp is None:
        return "—"
    pts = [(float(t), int(p)) for t, p in levels]
    if temp <= pts[0][0]:
        return "LEVEL 0"
    for i in range(1, len(pts)):
        if temp <= pts[i][0]:
            return f"LEVEL {i-1}->{i}"
    return f"LEVEL {len(pts)-1}"


def status(clear=False):
    if clear:
        print("\033[2J\033[H", end="")
    cfg = load_cfg()
    s = read_json(STATUS)
    print("Noctua NF-A4x20 5V PWM")
    print("═" * 72)
    if not s:
        print("Нет status.json. Сначала запусти: sudo python3 noctua_daemon.py")
        print_levels(cfg)
        return
    age = time.time() - float(s["timestamp"])
    maint = {
        "OK": "OK",
        "OBSERVE": "RPM ниже baseline — наблюдение",
        "CHECK_CLEANING": "ПРОВЕРИТЬ/ПОЧИСТИТЬ КУЛЕР",
        "NO_BASELINE": "нет baseline — выполни calibrate",
        "LOW_PWM": "PWM слишком низкий для диагностики",
        "DISABLED": "диагностика отключена",
    }.get(s.get("maintenance"), s.get("maintenance"))
    ratio = s.get("rpm_ratio")
    print(f"Status age:          {age:.1f} s")
    print(f"Mode:                {s['mode']}")
    print(f"PWM сейчас:          {s['applied_pwm']}/255 ({pct(s['applied_pwm']):.1f}%)")
    print(f"PWM target:          {s['target_pwm']}/255 ({pct(s['target_pwm']):.1f}%)")
    print(f"RPM:                 {s['rpm']} rpm")
    print(f"Expected RPM:        {s.get('expected_rpm') if s.get('expected_rpm') is not None else '—'}")
    print(f"RPM health:          {f'{ratio*100:.1f}% baseline' if ratio is not None else '—'}")
    print(f"Maintenance:         {maint}")
    print(f"CPU temp:            {s.get('cpu_temp_c', '—')}°C")
    print(f"NVMe temp:           {s.get('nvme_temp_c', '—')}°C")
    print(f"Control temp:        {s.get('control_temp_c', '—')}°C")
    print(f"Current level:       {level_name(s.get('control_temp_c'), cfg['control']['levels'])}")
    print(f"GPIO PWM/TACH:       GPIO{s['pwm_gpio']} / GPIO{s['tach_gpio']}")
    print(f"PWM frequency:       {s['pwm_frequency_hz']} Hz")
    print(f"Alarms:              {', '.join(s.get('alarms') or []) or 'нет'}")
    print_levels(cfg)


def watch(period=1.0):
    try:
        while True:
            status(True)
            print("\nCtrl+C — выход")
            time.sleep(period)
    except KeyboardInterrupt:
        pass


def settings():
    cfg = load_cfg()
    c, h, f = cfg["control"], cfg["health"], cfg["failsafe"]
    print("Настройки Noctua")
    print("═" * 72)
    print(f"Mode:                {c['mode']}")
    print(f"Manual PWM:          {c['manual_pwm']}/255")
    print(f"Min running PWM:     {c['min_running_pwm']}/255")
    print(f"CPU offset:          {c['cpu_offset_c']}°C")
    print(f"Hysteresis:          {c['hysteresis_c']}°C")
    print(f"Kickstart:           {c['kickstart_seconds']} s @ 255/255")
    print(f"Emergency CPU/NVMe:  {c['emergency_cpu_c']}°C / {c['emergency_nvme_c']}°C")
    print(f"Health threshold:    {float(h['min_rpm_ratio'])*100:.0f}% baseline")
    print(f"Health min PWM:      {h['min_pwm_for_check']}/255")
    print(f"TACH fail-safe:      {f['tach_fail_safe_enabled']}")
    print_levels(cfg)
    print("\nBaseline RPM")
    print("─" * 72)
    if not h.get("baseline_rpm"):
        print("нет — запусти: sudo python3 fanctl.py calibrate")
    else:
        for p, r in sorted((int(k), int(v)) for k, v in h["baseline_rpm"].items()):
            print(f"PWM {p:3d}/255 -> {r:4d} rpm")


def set_mode(mode):
    if mode not in ("auto", "manual"):
        raise SystemExit("mode: auto или manual")
    cfg = load_cfg(); cfg["control"]["mode"] = mode; save_cfg(cfg)


def set_pwm(value):
    value = int(value)
    if not 0 <= value <= 255:
        raise SystemExit("PWM должен быть 0..255")
    cfg = load_cfg(); cfg["control"]["manual_pwm"] = value; cfg["control"]["mode"] = "manual"; save_cfg(cfg)


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


def current_rpm():
    s = read_json(STATUS)
    if not s or time.time() - float(s.get("timestamp", 0)) > 3:
        return None
    try:
        return int(s["rpm"])
    except (KeyError, TypeError, ValueError):
        return None


def calibrate():
    cfg = load_cfg()
    old_mode, old_pwm = cfg["control"]["mode"], int(cfg["control"]["manual_pwm"])
    pwms = sorted({int(p) for _, p in cfg["control"]["levels"] if int(p) >= int(cfg["health"]["min_pwm_for_check"])})
    if 255 not in pwms:
        pwms.append(255)
    print("Калибровка baseline RPM. Выполняй на чистом вентиляторе в собранном корпусе.")
    if input("Продолжить? [y/N]: ").strip().lower() not in ("y", "yes", "д", "да"):
        return
    baseline = {}
    try:
        for p in pwms:
            cfg = load_cfg(); cfg["control"]["mode"] = "manual"; cfg["control"]["manual_pwm"] = p; save_cfg(cfg)
            print(f"PWM {p}/255: стабилизация 8 секунд...")
            time.sleep(8)
            samples = []
            for _ in range(5):
                r = current_rpm()
                if r and r > 0:
                    samples.append(r)
                time.sleep(1)
            if samples:
                baseline[str(p)] = int(round(statistics.median(samples)))
                print(f"  baseline = {baseline[str(p)]} rpm")
            else:
                print("  RPM не получен")
        cfg = load_cfg(); cfg["health"]["baseline_rpm"] = baseline; save_cfg(cfg)
    finally:
        cfg = load_cfg(); cfg["control"]["mode"] = old_mode; cfg["control"]["manual_pwm"] = old_pwm; save_cfg(cfg)


def cpu_info():
    rpm = pwm_raw = None
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "fan1_input").exists(): rpm = int((hw / "fan1_input").read_text().strip())
            if (hw / "pwm1").exists(): pwm_raw = int((hw / "pwm1").read_text().strip())
            if rpm is not None or pwm_raw is not None: break
        except (OSError, ValueError): pass
    custom = {}
    if BOOTCFG.exists():
        try:
            for raw in BOOTCFG.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line.startswith("dtparam="): continue
                for part in line[len("dtparam="):].split(","):
                    if "=" not in part: continue
                    k, v = part.split("=", 1)
                    for n in range(4):
                        if k == f"fan_temp{n}": custom.setdefault(n, {})["temp"] = int(v)/1000
                        elif k == f"fan_temp{n}_hyst": custom.setdefault(n, {})["hyst"] = int(v)/1000
                        elif k == f"fan_temp{n}_speed": custom.setdefault(n, {})["speed"] = int(v)
        except OSError: pass
    print("CPU-кулер Raspberry Pi 5 (read-only)")
    print("═" * 72)
    print(f"RPM:                 {rpm if rpm is not None else '—'}")
    print(f"PWM:                 {pwm_raw if pwm_raw is not None else '—'}/255")
    print("\nУровни CPU-кулера:")
    for n, (dt, dh, ds) in enumerate(CPU_DEFAULTS):
        x = custom.get(n, {})
        t, h, p = float(x.get("temp", dt)), float(x.get("hyst", dh)), int(x.get("speed", ds))
        print(f"LEVEL {n}: {t:5.1f}°C, hyst {h:3.1f}°C -> PWM {p:3d}/255 [{'custom' if x else 'default'}]")


def menu():
    while True:
        print(); status(); print("\nМеню")
        print("1 — Live monitor\n2 — Настройки\n3 — AUTO\n4 — MANUAL PWM\n5 — Изменить LEVEL\n6 — Calibrate baseline\n7 — CPU-кулер\nq — выход")
        c = input("> ").strip().lower()
        if c == "1": watch()
        elif c == "2": settings(); input("\nEnter...")
        elif c == "3": set_mode("auto")
        elif c == "4": set_pwm(input("PWM 0..255: ").strip())
        elif c == "5":
            cfg = load_cfg(); print_levels(cfg); i = input("LEVEL: ").strip(); cur = cfg["control"]["levels"][int(i)]
            t = input(f"Температура [{cur[0]}]: ").strip() or str(cur[0])
            p = input(f"PWM [{cur[1]}]: ").strip() or str(cur[1]); set_level(i, t, p)
        elif c == "6": calibrate()
        elif c == "7": cpu_info(); input("\nEnter...")
        elif c in ("q", "quit", "exit"): return


def main():
    a = sys.argv[1:]
    if not a: menu(); return 0
    cmd = a[0].lower()
    if cmd == "status": status()
    elif cmd == "watch": watch(float(a[1]) if len(a) > 1 else 1.0)
    elif cmd == "settings": settings()
    elif cmd == "cpu": cpu_info()
    elif cmd == "mode" and len(a) == 2: set_mode(a[1])
    elif cmd == "pwm" and len(a) == 2: set_pwm(a[1])
    elif cmd == "level" and len(a) == 4: set_level(a[1], a[2], a[3])
    elif cmd == "calibrate": calibrate()
    else:
        print("python3 fanctl.py [status|watch|settings|cpu|mode auto|pwm N|level N TEMP PWM|calibrate]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
