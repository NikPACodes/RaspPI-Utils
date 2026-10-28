"""
Модель обнаружения NVMe-накопителей и чтения SMART через nvme-cli."""
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NVMeSnapshot:
    # Температура NVMe в °C
    temperature: float | None
    # Показатель износа накопителя по SMART
    percentage_used: int | None
    # Содержимое поля critical_warning
    critical_warning: str | None


def locate_controller() -> Path:
    """
    Поиск доступного NVMe-контроллера Linux.

    Переменная окружения NVME_CTRL позволяет вручную выбрать контроллер.
    """
    # Проверяем переменную окружения NVME_CTRL
    override = os.environ.get('NVME_CTRL', '').strip()

    if override:
        path = Path(override)
        if not path.exists():
            raise RuntimeError(f'NVMe-контроллер не найден: {path}')
        return path

    # Проверяем список NVMe-контроллеров Linux
    for item in sorted(Path('/sys/class/nvme').glob('nvme*')):

        # Проверка соответствия имени контроллера формату nvme
        # Прим.: nvme0, nvme1, ...  (nvme0n1 не подходит т.к. это накопитель, а не контроллер)
        if re.fullmatch(r'nvme\d+', item.name):
            device = Path('/dev') / item.name
            if device.exists():
                return device
    raise RuntimeError('NVMe-контроллер не найден')


def model(controller: Path) -> str:
    """
    Получение названия модели NVMe-накопителя через sysfs.
    """
    try:
        # Формируем путь к model и читаем sysfs
        value = (Path('/sys/class/nvme') / controller.name / 'model').read_text().strip()
        return ' '.join(value.split()) or 'неизвестная модель'
    except OSError:
        return 'неизвестная модель'


class NVMeReader:
    """
    Чтение SMART-показателей NVMe.

    Выполнение команды nvme smart-log требует sudo.
    """
    def __init__(self, controller: Path):
        """
        Подготовка чтения SMART указанного контроллера
        """
        if not shutil.which('nvme'):
            raise RuntimeError('Не установлен nvme-cli: sudo apt install -y nvme-cli')

        self.controller = controller
        # По умолчанию пытаемся читать SMART от имени текущего пользователя.
        self.sudo = False

        # Пробное чтение без root
        if os.geteuid() != 0 and self._command(use_sudo=False).returncode != 0:
            print('Для чтения SMART NVMe требуется sudo.', flush=True)

            try:
                # Запрос sudo
                result = subprocess.run(['sudo', '-v'], check=False)
            except OSError as exc:
                raise RuntimeError(f'Не удалось запустить sudo: {exc}') from exc

            if result.returncode != 0:
                raise RuntimeError('Не удалось получить права для SMART NVMe')
            # Дальнейшие команды SMART будут выполняться через sudo
            self.sudo = True

        # Проверяем SMART сразу, а не заменяем ошибки подключения фиктивным нулём.
        if self._command(self.sudo).returncode != 0:
            raise RuntimeError(f'Не удаётся прочитать SMART: {controller}')


    def _command(self, use_sudo: bool) -> subprocess.CompletedProcess[str]:
        """
        Выполнение команды получения SMART NVMe.
        """
        cmd = ['nvme', 'smart-log', str(self.controller)]
        if use_sudo:
            cmd = ['sudo', '-n', *cmd]
        return subprocess.run(cmd, capture_output=True, text=True, check=False)


    def read(self) -> NVMeSnapshot:
        """
        Чтение SMART NVMe и извлечение показателей.
        """
        # Выполнение nvme smart-log.
        result = self._command(self.sudo)

        if result.returncode != 0:
            return NVMeSnapshot(None, None, None)

        data = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition(':')
            if sep:
                data[key.strip().lower()] = value.strip()

        def number(key: str, as_float: bool = False) -> int | None:
            # Извлечение первого числа из SMART-поля.
            pattern = r"-?\d+(?:\.\d+)?" if as_float else r"-?\d+"
            match = re.search(pattern, data.get(key, ''))

            if not match:
                return None
            if as_float:
                return float(match.group())
            return int(match.group())

        temp = number('temperature', as_float=True)  # температура
        used = number('percentage_used')                  # износ
        warning = data.get('critical_warning')
        return NVMeSnapshot(temp, used, warning)