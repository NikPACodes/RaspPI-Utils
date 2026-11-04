"""
Baseline-калибровка RPM Noctua Fan.

Модуль отвечает за:
- чтение актуального RPM из status.json;
- последовательное переключение рабочих PWM;
- ожидание стабилизации вентилятора;
- сбор нескольких измерений RPM;
- вычисление медианного RPM;
- сохранение baseline_rpm в config.json;
- восстановление исходного режима после калибровки.

PWM=255 не используется при длительной baseline-калибровке
из-за наблюдавшихся в данной сборке повышенных RPM.
"""
import statistics, time

from noctua.config import read_json, load_cfg, save_cfg
from noctua.paths import STATUS

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
    Калибровка baseline RPM.

    Используются рабочие значения PWM из температурных кривых CPU и NVMe.

    PWM=255 автоматически исключён из длительной калибровки
    из-за наблюдавшихся в данной сборке повышенных RPM.
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