"""
ANSI-вывод и чтение клавиш терминала Linux.

Terminal временно переводит TTY в режим ввода без Enter и восстанавливает
исходные настройки при нормальном выходе и Ctrl+C.
Esc, Ctrl+Z, E и Q завершают цикл.
render() только выводит полученные показания и самостоятельно не обращается к оборудованию.
"""
import os
import select
import sys
import termios
import time
import tty
from datetime import datetime
import socket

from config import BAR_WIDTH, CPU_CRIT, CPU_WARN, NVME_CRIT, NVME_WARN
from sensors.nvme import NVMeDevice

RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'
GREEN = '\033[32m'
YELLOW = '\033[33m'
RED = '\033[31m'
CYAN = '\033[36m'


class Terminal:
    """
    Контекстный менеджер состояния терминала и чтения клавиш.
    """
    def __init__(self):
        # Изначальные настройки терминала
        self.original = None


    def __enter__(self):
        """
        Подготовить терминал для интерактивной работы.
        """
        if sys.stdin.isatty():
            # Получение файлового дескриптора stdin.
            fd = sys.stdin.fileno()

            # Сохраняем настройки терминала для восстановления (при завершении).
            self.original = termios.tcgetattr(fd)

            try:
                # Чтение клавиш сразу, без Enter.
                tty.setcbreak(fd)
                # Получение текущей настройки после включения cbreak
                attrs = termios.tcgetattr(fd)
                #  Отключаем управляющий символ VSUSP.
                attrs[6][termios.VSUSP] = 0  # Ctrl+Z как байт, а не SIGTSTP
                # Немедленное применение параметров.
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except BaseException:
                # Возвращение исходных настроек терминала
                termios.tcsetattr(fd, termios.TCSANOW, self.original)
                raise

        # Скрытие курсора
        print('\033[?25l', end='', flush=True)
        return self


    def __exit__(self, *_):
        """
        Восстановление первоначальных настроек терминала.
        """
        try:
            if self.original is not None:
                # Возвращение исходных настроек терминала
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, self.original)
        finally:
            # Сброс оформления и возвращение курсора
            print(f'\033[?25h{RESET}', end='\n', flush=True)


    def should_exit(self, interval: float) -> bool:
        """
        Ожидание интервала или выхода, возвращая True при выходе.
        """
        if not sys.stdin.isatty():
            time.sleep(interval)
            return False

        # select() ожидает данные на stdin.
        # Максимальное время ожидания задаёт interval.
        readable, _, _ = select.select([sys.stdin], [], [], interval)

        return bool(readable and os.read(sys.stdin.fileno(), 1) in (b'\x1b',  # Esc
                                                                    b'\x1a',  # Ctrl+Z
                                                                    b'e',     # e
                                                                    b'E',     # E
                                                                    b'q',     # q
                                                                    b'Q'))    # Q


def _temperature(label: str, value: float | None, warn: int, crit: int) -> str:
    """
    Формирование цветной строки температуры или признак «н/д».
    """
    if value is None:
        return f'  {label:<5}    н/д    {DIM}данные недоступны{RESET}'

    color, status = ((RED, 'КРИТИЧНО') if int(value) >= crit else
                     (YELLOW, 'ВЫСОКАЯ') if int(value) >= warn else (GREEN, 'НОРМА'))

    # Ограничение шкалы диапазоном 0–100 °C.
    # и подсчет количества заполненных символов.
    filled = max(0, min(100, int(value))) * BAR_WIDTH // 100

    # Создание цветовой шкалы
    bar = f'{color}{"█" * filled}{DIM}{"░" * (BAR_WIDTH - filled)}{RESET}'
    return f'  {label:<5} {color}{value:5.1f} °C{RESET}  {bar}  {color}{status}{RESET}'


def _nvme_details(device: NVMeDevice) -> str:
    """
    Отрисовка данных NVMe: износ, SMART предупреждение или причину недоступности.
    """
    snap = device.snapshot
    # Износ
    used = f'{snap.percentage_used}%' if snap.percentage_used is not None else 'н/д'
    # SMART предупреждения
    warning = snap.critical_warning if snap.critical_warning is not None else 'н/д'
    detail = f'    Износ: {used} | critical_warning: {warning}'
    if device.error:
        detail += f' | SMART: {device.error}'
    return detail


def render(interval, cpu, devices,
           load, cpu_fan, noctua_fan):
    """
    Перерисовка экран по готовым измерениям датчиков от monitor.py.
    """
    print('\033[H\033[2J', end='')
    print(f'{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════╗{RESET}')
    print(f'{BOLD}{CYAN}║               RASPBERRY PI — ТЕМПЕРАТУРЫ                     ║{RESET}')
    print(f'{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════╝{RESET}')
    print(f'  {datetime.now():%d.%m.%Y %H:%M:%S} | {socket.gethostname()} | обновление: {interval:g} с')
    print(_temperature('CPU', cpu, CPU_WARN, CPU_CRIT))
    if devices:
        print(f'\n  NVMe-контроллеры: {len(devices)}')
        for dev in devices:
            print(f'  {dev.controller}: {dev.model}')
            print(_temperature(dev.controller.name, dev.snapshot.temperature,
                               NVME_WARN, NVME_CRIT))
            print(_nvme_details(dev))
    else:
        print(f'\n  NVMe: {DIM}не обнаружены{RESET}')
    print(f'\n  Load average: {load}')
    print(f'  CPU Fan: RPM {cpu_fan.rpm if cpu_fan.rpm is not None else "н/д"} | PWM {cpu_fan.pwm_percent if cpu_fan.pwm_percent is not None else "н/д"}')
    print(f'  Noctua:  RPM {noctua_fan.rpm if noctua_fan.rpm is not None else "н/д"} | PWM {noctua_fan.pwm_percent if noctua_fan.pwm_percent is not None else "н/д"}')
    print(f'  {DIM}Пороги: CPU {CPU_WARN}/{CPU_CRIT} °C, NVMe {NVME_WARN}/{NVME_CRIT} °C{RESET}')
    print(f'  {DIM}Выход: Ctrl+C, Ctrl+Z, Esc, e или q{RESET}', flush=True)
