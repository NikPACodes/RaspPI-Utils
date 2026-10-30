"""
Модель обнаружения NVMe-накопителей и чтения SMART через nvme-cli.
"""
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NVMeSnapshot:
    """
    SMART одного NVMe
    """
    # Температура NVMe в °C
    temperature: float | None = None
    # Показатель износа накопителя по SMART
    percentage_used: int | None = None
    # Содержимое поля critical_warning
    critical_warning: str | None = None

@dataclass(frozen=True)
class NVMeDevice:
    """
    Информацию об одном NVMe-контроллере
    """
    # Путь к NVMe-контроллеру
    controller: Path
    # Модель накопителя
    model: str
    # Показатели SMART
    snapshot: NVMeSnapshot
    # Ошибка чтения
    error: str | None = None


def locate_controllers() -> list[Path]:
    """
    Поиск доступных NVMe-контроллеров Linux.

    Переменная окружения NVME_CTRL позволяет вручную выбрать контроллеры.
    """
    # Проверяем переменную окружения NVME_CTRL
    override = os.environ.get('NVME_CTRL', '').strip()

    if override:
        devices = [Path(name.strip()) for name in override.split(',') if name.strip()]
    else:
        # Ищем NVMe через Linux sysfs
        devices = [Path('/dev') / p.name for p in Path('/sys/class/nvme').glob('nvme*')
                       if re.fullmatch(r'nvme\d+', p.name)]

    # Убираем дубликаты и сортируем по номеру контроллера
    result = sorted(set(devices), key=lambda p: (
        int(re.fullmatch(r'nvme(\d+)', p.name).group(1))
        if re.fullmatch(r'nvme(\d+)', p.name) else float('inf'), str(p)))

    return [p for p in result if p.exists()]


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

    При недостаточных правах доступа может потребоваться sudo.
    """
    def __init__(self, controller: Path, use_sudo: bool = False):
        """
        Подготовка чтения SMART одного контроллера
        """
        self.controller = controller
        # По умолчанию пытаемся читать SMART от имени текущего пользователя.
        self.sudo = use_sudo


    def _command(self) -> subprocess.CompletedProcess[str]:
        """
        Выполнение команды получения SMART NVMe.
        """
        cmd = ['nvme', 'smart-log', str(self.controller)]
        if self.sudo:
            cmd = ['sudo', '-n', *cmd]
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5)


    def read(self) -> tuple[NVMeSnapshot, str | None]:
        """
        Чтение SMART NVMe и извлечение показателей.
        """
        try:
            # Выполнение nvme smart-log.
            result = self._command()
        except subprocess.TimeoutExpired:
            return NVMeSnapshot(), 'Превышено время ожидания SMART (5 сек)'
        except OSError as exc:
            return NVMeSnapshot(), str(exc)

        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            return NVMeSnapshot(), (detail[-1][:150] if detail else 'Ошибка чтения SMART')

        data = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition(':')
            if sep:
                data[key.strip().lower()] = value.strip()

        def number(key: str, as_float: bool = False) -> int|float|None:
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



class NVMeMonitor:
    """
    Менеджер мониторинга NVMe-контроллеров.
    """
    def __init__(self):
        # Получаем список обнаруженных устройств
        self.controllers = locate_controllers()
        self.readers: list[NVMeReader] = []

        if not self.controllers:
            return

        # Проверка доступности команды nvme
        if not shutil.which('nvme'):
            # Нет утилиты: каждый диск покажет н/д, но монитор работает.
            self.readers = [NVMeReader(p) for p in self.controllers]
            return

        # По умолчанию читаем SMART без sudo
        use_sudo = False

        # Пробное чтение без root
        if os.geteuid() != 0:
            for controller in self.controllers:
                if NVMeReader(controller).read()[1] is not None:
                    print('Чтение SMART требует sudo; проверяем доступ...', flush=True)
                    try:
                        # Запрос sudo
                        use_sudo = subprocess.run(['sudo', '-v'], check=False).returncode == 0
                    except OSError:
                        use_sudo = False

                    break

        self.readers = [NVMeReader(p, use_sudo) for p in self.controllers]


    def read(self) -> list[NVMeDevice]:
        """
        Получение данных всех NVMe
        """
        devices = []
        for reader in self.readers:
            snapshot, error = reader.read()
            devices.append(NVMeDevice(controller = reader.controller,
                                      model = model(reader.controller),
                                      snapshot = snapshot,
                                      error = error))
        return devices