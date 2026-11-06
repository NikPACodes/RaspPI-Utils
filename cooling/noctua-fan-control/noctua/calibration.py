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
import os, statistics, time

from noctua.config import read_json, load_cfg, edit_session, validate_config
from noctua.paths import STATUS

# ------------------------------------------------------------
# RPM helper
# ------------------------------------------------------------
def current_state():
    """
    Текущий статус от daemon.
    Старые значения не используем для baseline.
    """
    state = read_json(STATUS)
    if not state:
        return None

    try:
        age = time.time() - float(state["timestamp"])
        pid = int(state["daemon_pid"])
        if not 0 <= age <= 3 or pid < 1:
            return None
        try:
            os.kill(pid, 0)  # проверка существования процесса, без отправки сигнала
        except PermissionError:
            pass
    except (KeyError, TypeError, ValueError, OSError):
        return None

    return state


def current_rpm():
    """
    Текущий RPM из status.json (только из свежего статуса).
    """
    state = current_state()
    if not state:
        return None

    try:
        return int(state["rpm"])
    except (KeyError, TypeError, ValueError):
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
    # Проверки перед калибровкой
    cfg = validate_config(load_cfg())
    min_pwm = int(cfg["health"]["min_pwm_for_check"])

    pwms = {
        int(pwm)
        for key in ("cpu_levels", "nvme_levels")
        for _, pwm in cfg["control"][key] if min_pwm <= int(pwm) < 255
    }
    pwms = sorted(pwms)

    if not pwms:
        raise ValueError("Нет допустимых PWM для калибровки")
    if current_state() is None:
        raise RuntimeError("Daemon не отвечает: требуется свежий status.json с daemon_pid")

    print("Калибровка baseline RPM.")
    print("Выполняй на чистом вентиляторе в собранном корпусе с установленной сеткой.")
    print("PWM 255 автоматически НЕ тестируется.")
    answer = input("Продолжить? [y/N]: ").strip().lower()
    if answer not in ("y", "yes", "д", "да"):
        return

    # Калибровка (одна транзакция под файловой блокировкой)
    with edit_session() as session:
        cfg = session.load()
        old_mode = cfg["control"]["mode"]
        old_pwm = int(cfg["control"]["manual_pwm"])
        baseline = {}
        changed_mode = False
        finished = False
        try:
            for pwm in pwms:
                if current_state() is None:
                    raise RuntimeError("Daemon перестал отвечать во время калибровки")

                # Просим daemon выставить PWM через config
                cfg = session.load()
                cfg["control"]["mode"] = "manual"
                cfg["control"]["manual_pwm"] = pwm
                session.save(cfg)
                changed_mode = True

                print(f"PWM {pwm}/255: стабилизация 8 секунд...")
                time.sleep(8)

                # Проверка соответствия RPM выбранному PWM
                samples = []
                last_timestamp = None
                for _ in range(5):
                    state = current_state()
                    if state is None:
                        raise RuntimeError("Статус daemon потерян — baseline не сохранён")
                    if state.get("mode") != "manual" or state.get("applied_pwm") != pwm:
                        raise RuntimeError(f"PWM {pwm}: daemon применил другое значение — baseline не сохранён")

                    timestamp = float(state["timestamp"])
                    if timestamp != last_timestamp:
                        try:
                            rpm = int(state["rpm"])
                        except (KeyError, TypeError, ValueError):
                            rpm = 0
                        if rpm > 0:
                            samples.append(rpm)
                        last_timestamp = timestamp
                    time.sleep(1)

                if len(samples) < 4:
                    raise RuntimeError(f"PWM {pwm}: получено меньше четырёх достоверных RPM")
                value = int(round(statistics.median(samples)))
                baseline[str(pwm)] = value
                print(f"  baseline = {value} rpm")

            # Сохраняем baseline только после успешной проверки ВСЕХ уровней.
            cfg = session.load()
            cfg["health"]["baseline_rpm"] = baseline
            session.save(cfg)
            finished = True
            print("Baseline сохранён.")

        finally:
            if changed_mode:
                # Возвращен исходный режим.
                cfg = session.load()
                cfg["control"]["mode"] = old_mode
                cfg["control"]["manual_pwm"] = old_pwm
                session.save(cfg)
                print(f"Восстановлен режим: {old_mode}, manual PWM: {old_pwm}.")
            if not finished:
                print("Калибровка отменена; прежний baseline сохранён.")
