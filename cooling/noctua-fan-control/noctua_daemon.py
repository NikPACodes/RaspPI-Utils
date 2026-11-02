#!/usr/bin/env python3
from __future__ import annotations

import json, os, signal, threading, time
from pathlib import Path
from typing import Any

from gpiozero import PWMOutputDevice, DigitalInputDevice
from gpiozero.pins.lgpio import LGPIOFactory

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
STATUS = BASE / "status.json"
STOP = threading.Event()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def read_float(path: Path, scale=1.0):
    try:
        return float(path.read_text().strip()) / scale
    except (OSError, ValueError):
        return None


def cpu_temp_c():
    return read_float(Path("/sys/class/thermal/thermal_zone0/temp"), 1000.0)


def nvme_temps_c():
    out = []
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip().lower() != "nvme":
                continue
        except OSError:
            continue
        for f in hw.glob("temp*_input"):
            t = read_float(f, 1000.0)
            if t is not None and -20 <= t <= 120:
                out.append(t)
    return out


def find_rp1_gpiochip():
    for p in Path("/sys/class/gpio").glob("gpiochip*"):
        try:
            label = (p / "label").read_text().strip()
        except OSError:
            continue
        if "pinctrl-rp1" in label:
            return int(p.name.replace("gpiochip", ""))
    if Path("/dev/gpiochip0").exists():
        return 0
    if Path("/dev/gpiochip4").exists():
        return 4
    raise RuntimeError("Не найден GPIO-контроллер RP1")


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    levels = cfg["control"]["levels"]
    if cfg["control"]["mode"] not in ("auto", "manual"):
        raise ValueError("mode должен быть auto или manual")
    if not levels:
        raise ValueError("levels пуст")
    last = -999.0
    for temp, pwm in levels:
        if float(temp) <= last or not 0 <= int(pwm) <= 255:
            raise ValueError("levels: температуры должны возрастать, PWM 0..255")
        last = float(temp)
    return cfg


def interp_pwm(temp, levels):
    pts = [(float(t), int(p)) for t, p in levels]
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            k = (temp - t0) / (t1 - t0)
            return int(round(p0 + k * (p1 - p0)))
    return pts[-1][1]


def interp_rpm(pwm, baseline):
    pts = sorted((int(k), float(v)) for k, v in baseline.items() if float(v) > 0)
    if not pts:
        return None
    if pwm <= pts[0][0]:
        return pts[0][1]
    if pwm >= pts[-1][0]:
        return pts[-1][1]
    for (p0, r0), (p1, r1) in zip(pts, pts[1:]):
        if p0 <= pwm <= p1:
            k = (pwm - p0) / (p1 - p0)
            return r0 + k * (r1 - r0)
    return None


def write_json_atomic(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Controller:
    def __init__(self):
        self.cfg = load_config()
        self.cfg_mtime = CONFIG.stat().st_mtime
        self.factory = LGPIOFactory(chip=find_rp1_gpiochip())
        g = self.cfg["gpio"]
        self.pwm = PWMOutputDevice(
            int(g["pwm_gpio"]), active_high=True, initial_value=1.0,
            frequency=int(g["pwm_frequency_hz"]), pin_factory=self.factory
        )
        self.tach = DigitalInputDevice(
            int(g["tach_gpio"]), pull_up=None, active_state=False,
            bounce_time=None, pin_factory=self.factory
        )
        self.lock = threading.Lock()
        self.pulses = 0
        self.tach.when_activated = self._pulse
        self.last_rpm_t = time.monotonic()
        self.rpm_ema = 0.0
        self.zero_since = None
        self.applied_pwm = 255
        self.last_control_temp = None
        self.low_rpm_since = None
        self.started = time.time()

    def _pulse(self):
        with self.lock:
            self.pulses += 1

    def reload(self):
        try:
            m = CONFIG.stat().st_mtime
        except OSError:
            return
        if m != self.cfg_mtime:
            self.cfg = load_config()
            self.cfg_mtime = m
            print("[noctua] config reloaded", flush=True)

    def update_rpm(self):
        now = time.monotonic()
        dt = now - self.last_rpm_t
        if dt < 0.8:
            return
        with self.lock:
            pulses, self.pulses = self.pulses, 0
        ppr = int(self.cfg["gpio"]["tach_pulses_per_rev"])
        rpm = (pulses / ppr) * (60.0 / dt)
        self.rpm_ema = rpm if self.rpm_ema == 0 and rpm > 0 else 0.35 * rpm + 0.65 * self.rpm_ema
        if rpm == 0:
            self.zero_since = self.zero_since or now
        else:
            self.zero_since = None
        self.last_rpm_t = now

    def temperatures(self):
        cpu = cpu_temp_c()
        nvmes = nvme_temps_c()
        nvme = max(nvmes) if nvmes else None
        vals = []
        if nvme is not None:
            vals.append(nvme)
        if cpu is not None:
            vals.append(cpu - float(self.cfg["control"]["cpu_offset_c"]))
        return cpu, nvme, nvmes, (max(vals) if vals else None)

    def choose_pwm(self, cpu, nvme, control_temp):
        c, f = self.cfg["control"], self.cfg["failsafe"]
        alarms = []
        if cpu is not None and cpu >= float(c["emergency_cpu_c"]):
            return 255, ["CPU emergency"]
        if nvme is not None and nvme >= float(c["emergency_nvme_c"]):
            return 255, ["NVMe emergency"]

        if c["mode"] == "manual":
            target = int(c["manual_pwm"])
        else:
            if control_temp is None:
                return int(f["sensor_failure_pwm"]), ["Нет температурных датчиков"]
            h = float(c["hysteresis_c"])
            if self.last_control_temp is None:
                self.last_control_temp = control_temp
            elif abs(control_temp - self.last_control_temp) >= h:
                self.last_control_temp = control_temp
            target = interp_pwm(self.last_control_temp, c["levels"])

        minimum = int(c["min_running_pwm"])
        if 0 < target < minimum:
            target = minimum

        if f["tach_fail_safe_enabled"] and target >= int(f["tach_zero_min_pwm"]) and self.zero_since is not None:
            if time.monotonic() - self.zero_since >= float(f["tach_zero_seconds"]):
                alarms.append("TACH=0 при ненулевом PWM")
                target = int(f["tach_failure_pwm"])

        return int(clamp(target, 0, 255)), alarms

    def set_pwm(self, value):
        value = int(clamp(value, 0, 255))
        if self.applied_pwm == 0 and value > 0:
            self.pwm.value = 1.0
            time.sleep(float(self.cfg["control"]["kickstart_seconds"]))
        self.pwm.value = value / 255.0
        self.applied_pwm = value

    def health(self):
        h = self.cfg["health"]
        if not h["enabled"]:
            return "DISABLED", None, None
        baseline = h.get("baseline_rpm", {})
        if not baseline:
            return "NO_BASELINE", None, None
        if self.applied_pwm < int(h["min_pwm_for_check"]):
            return "LOW_PWM", None, None
        expected = interp_rpm(self.applied_pwm, baseline)
        if not expected:
            return "NO_BASELINE", None, None
        ratio = self.rpm_ema / expected
        if ratio < float(h["min_rpm_ratio"]):
            self.low_rpm_since = self.low_rpm_since or time.monotonic()
            if time.monotonic() - self.low_rpm_since >= float(h["warn_after_seconds"]):
                return "CHECK_CLEANING", expected, ratio
            return "OBSERVE", expected, ratio
        self.low_rpm_since = None
        return "OK", expected, ratio

    def loop(self):
        while not STOP.is_set():
            started = time.monotonic()
            self.reload()
            self.update_rpm()
            cpu, nvme, nvmes, control_temp = self.temperatures()
            target, alarms = self.choose_pwm(cpu, nvme, control_temp)
            if target != self.applied_pwm:
                self.set_pwm(target)
            maintenance, expected, ratio = self.health()
            write_json_atomic(STATUS, {
                "timestamp": time.time(),
                "uptime_s": time.time() - self.started,
                "mode": self.cfg["control"]["mode"],
                "pwm_gpio": int(self.cfg["gpio"]["pwm_gpio"]),
                "tach_gpio": int(self.cfg["gpio"]["tach_gpio"]),
                "pwm_frequency_hz": int(self.cfg["gpio"]["pwm_frequency_hz"]),
                "target_pwm": target,
                "applied_pwm": self.applied_pwm,
                "rpm": int(round(self.rpm_ema)),
                "cpu_temp_c": None if cpu is None else round(cpu, 1),
                "nvme_temp_c": None if nvme is None else round(nvme, 1),
                "nvme_temps_c": [round(x, 1) for x in nvmes],
                "control_temp_c": None if control_temp is None else round(control_temp, 1),
                "expected_rpm": None if expected is None else int(round(expected)),
                "rpm_ratio": None if ratio is None else round(ratio, 3),
                "maintenance": maintenance,
                "alarms": alarms,
            })
            interval = float(self.cfg["control"]["update_interval_s"])
            STOP.wait(max(0.05, interval - (time.monotonic() - started)))

    def close(self):
        try:
            self.pwm.value = 1.0
            time.sleep(0.2)
        except Exception:
            pass
        for obj in (self.tach, self.pwm, self.factory):
            try:
                obj.close()
            except Exception:
                pass


def stop_handler(*_):
    STOP.set()


def main():
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    c = None
    try:
        c = Controller()
        c.loop()
        return 0
    except Exception as e:
        print(f"[noctua] FATAL: {e}", flush=True)
        return 1
    finally:
        if c:
            c.close()


if __name__ == "__main__":
    raise SystemExit(main())
