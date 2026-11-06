#!/usr/bin/env python3
"""
Интерфейс управления и мониторинга Noctua.

Отвечает за:
- интерактивное меню;
- обработку аргументов командной строки;
- маршрутизацию команд в модули пакета noctua.

Через CLI доступны:
- просмотр текущего состояния;
- live-мониторинг;
- просмотр настроек;
- переключение AUTO/MANUAL;
- установка manual PWM;
- изменение температурных LEVEL CPU/NVMe;
- запуск baseline-калибровки RPM;
- просмотр доступных данных CPU/HAT fan.

fanctl не работает с GPIO напрямую и самостоятельно не управляет PWM.

Единственным владельцем PWM остается noctua_daemon.
"""
import sys

from noctua.calibration import calibrate
from noctua.config import set_mode, set_pwm, load_cfg, set_level
from noctua.monitor import status, watch, settings, print_curve, cpu_fan_info

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
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        # Ошибка настроек — понятное сообщение, без длинного traceback.
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(2) from None