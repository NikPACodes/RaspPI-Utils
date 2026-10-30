#!/usr/bin/env python3
"""
Терминальный монитор температур Raspberry Pi.
"""
import argparse
import math
import re
import signal
from pathlib import Path

from config import DEFAULT_INTERVAL
from sensors import cpu, fans, nvme
from ui.terminal import Terminal, render


def positive_interval(raw: str) -> float:
    """
    Преобразование строки интервала в секунды.

    Примечание:
    - Допускается только положительное значение.
    - 0 запрещен.
    """
    value = raw.replace(',', '.')

    if not re.fullmatch(r'\d+(?:\.\d+)?', value):
        raise argparse.ArgumentTypeError('интервал должен быть положительным числом')
    result = float(value)

    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('интервал должен быть больше 0')
    return result


def load_average() -> str:
    """
    Получение средней системной нагрузки Linux.
    Возвращаем первые 3 значения:
    - 1 минута
    - 5 минут
    - 15 минут
    """
    try:
        return ' '.join(Path('/proc/loadavg').read_text().split()[:3])
    except OSError:
        return 'н/д'


def main() -> int:
    """
    Функция терминального монитора Raspberry Pi.

    Выполняет:
    1. Чтение и проверку аргументов командной строки.
    2. Поиск NVMe-контроллера.
    3. Инициализацию доступа к NVMe SMART.
    4. Подготовку терминала и обработку Ctrl+Z.
    5. Периодический опрос всех датчиков.
    6. Отображение показателей в терминале.
    7. Корректное завершение работы.

    Returns:
        int: 0 при штатном завершении.
    """

    # -------------------------------
    # 1. ПАРАМЕТРЫ ЗАПУСКА
    # -------------------------------
    parser = argparse.ArgumentParser(description='Монитор CPU и NVMe Raspberry Pi')
    parser.add_argument('interval',                # Имя аргумента
                        nargs='?',                 # Аргумент необязательный
                        type=positive_interval,    # Функция проверки и преобразования введённого значения
                        default=DEFAULT_INTERVAL,  # Значение из config.py, если аргумент не передан
                        help='интервал обновления в секундах (например, 1 или 0,5)')
    args = parser.parse_args()

    # -------------------------------
    # 2. ИНИЦИАЛИЗАЦИЯ NVMe
    # -------------------------------
    # NVMe — необязательный компонент.
    # Поиск NVMe выполняем один раз при запускепоиск выполняем один раз при запуске.
    nvme_monitor = nvme.NVMeMonitor()

    # -------------------------------
    # 3. ОБРАБОТКА SIGTSTP
    # -------------------------------
    # Если SIGTSTP придёт извне, завершаем монитор вместо приостановки процесса
    def handle_sigtstp(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTSTP, handle_sigtstp)

    # -------------------------------
    # 4. ПОДГОТОВКА ТЕРМИНАЛА
    # -------------------------------
    # Циклическая перерисовка экран.
    with Terminal() as terminal:
        while True:
            render(args.interval,              # Интервал обновления
                   cpu.read_temperature(),     # Текущая температура CPU
                   nvme_monitor.read(),        # Список NVMe: модель, температура, износ, SMART и ошибки
                   load_average(),             # Средняя нагрузка Linux
                   fans.read_cpu_fan(),        # Показатели вентилятора CPU
                   fans.read_noctua_fan())     # Показатели Noctua

            # Проверка выхода
            if terminal.should_exit(args.interval):
                break
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError) as exc:
        print(f'Ошибка: {exc}', file=__import__('sys').stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
