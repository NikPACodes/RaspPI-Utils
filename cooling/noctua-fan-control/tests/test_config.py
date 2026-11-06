"""Тесты конфигурации noctua-fan-control.

Проверяем:
- Корректность температурных кривых CPU/NVMe.
- Защиту от недопустимых значений.
- Сохранность конфигурации при ошибках.
- Переключение AUTO/MANUAL.
- Атомарное сохранение и резервные копии.
- Защиту от одновременного изменения настроек.
- Запрет второго экземпляра daemon.
- Безопасную горячую перезагрузку конфигурации.

Все операции выполняются с временным config.json.
Реальные GPIO Raspberry Pi не используются.
"""
import copy
import json

import pytest

from conftest import daemon
from noctua import config

pytestmark = pytest.mark.config


def test_01_validation_rejects_bad_curve_and_nan(project):
    """
    Валидация температурных кривых.

    Проверки:
    1. Проверяем исходную корректную конфигурацию.
    2. Устанавливаем PWM ниже предыдущего LEVEL.
    3. Передаём NaN вместо температуры.

    Ожидаемый результат:
    - Исходная конфигурация принимается.
    - Убывающий PWM отклоняется.
    - Неконечная температура отклоняется.
    """
    cfg = project.get()
    assert config.validate_config(cfg) == cfg

    broken = copy.deepcopy(cfg)
    broken['control']['nvme_levels'][4][1] = 20
    with pytest.raises(ValueError, match='PWM не должен уменьшаться'):
        config.validate_config(broken)

    broken = copy.deepcopy(cfg)
    broken['control']['cpu_levels'][3][0] = float('nan')
    with pytest.raises(ValueError, match='конечным'):
        config.validate_config(broken)


def test_02_invalid_level_keeps_original_config(project, monkeypatch):
    """
    Ошибочный LEVEL не повреждает config.json

    Проверка:
    Через set_level() пытаемся сохранить недопустимое уменьшение PWM в температурной кривой NVMe.

    Ожидаемый результат:
    - Операция завершается ошибкой ValueError.
    - Исходный config.json остаётся побайтно неизменным.
    """
    monkeypatch.setattr(config.time, 'sleep', lambda _: None)
    before = project.config_path.read_bytes()

    with pytest.raises(ValueError, match='PWM не должен уменьшаться'):
        config.set_level('nvme', 4, '55', '20')

    assert project.config_path.read_bytes() == before


def test_03_mode_pwm_backup_and_atomic_write(project, monkeypatch):
    """
    Сохранение ручного режима и резервной копии.

    Проверки:
    1. Переключаем контроллер в MANUAL с PWM=128.
    2. Проверяем создание резервной копии.
    3. Возвращаем режим AUTO.
    4. Проверяем отсутствие временных файлов записи.

    Ожидаемый результат:
    - MANUAL сохраняется с PWM=128.
    - Backup содержит предыдущую конфигурацию.
    - AUTO успешно восстанавливается.
    - Временные файлы атомарной записи отсутствуют.
    """
    monkeypatch.setattr(config.time, 'sleep', lambda _: None)
    config.set_pwm('128')

    assert project.get()['control']['mode'] == 'manual'
    assert project.get()['control']['manual_pwm'] == 128
    assert json.loads(project.config_path.with_suffix('.json.bak').read_text()) == project.original

    config.set_mode('auto')
    assert project.get()['control']['mode'] == 'auto'
    assert not list(project.tmp_path.glob('.config-*.tmp'))


def test_04_settings_lock_rejects_concurrent_writer(project):
    """
    Блокировка одновременного изменения настроек.

    Проверка:
    Открываем одну сессию редактирования конфигурации, затем пытаемся изменить PWM через вторую операцию.

    Ожидаемый результат:
    - Второй редактор получает ошибку занятости.
    - Конкурирующая запись не выполняется.

    Это предотвращает конфликт между CLI и калибровкой.
    """
    with config.edit_session():
        with pytest.raises(RuntimeError, match='Настройки заняты'):
            config.set_pwm(77)


def test_05_second_daemon_cannot_claim_pwm(project):
    """
    Только один daemon владеет PWM.
    Проверяется программная блокировка, не аппаратный GPIO.

    Проверки:
    1. Первый экземпляр получает файловую блокировку.
    2. Второй пытается получить ту же блокировку.
    3. Первый освобождает блокировку.
    4. Проверяем возможность повторного запуска.

    Ожидаемый результат:
    - Одновременный второй запуск запрещён.
    - После освобождения ресурса запуск разрешён.
    """
    with daemon.daemon_instance_lock():
        with pytest.raises(RuntimeError, match='уже запущен'):
            with daemon.daemon_instance_lock():
                pass
    with daemon.daemon_instance_lock():
        pass


def test_06_hot_reload_retains_last_valid_settings(project, capsys):
    """
    Ошибка hot-reload не останавливает управление.

    Проверки:
    1. Подготавливаем работающий контроллер.
    2. Записываем некорректную температурную кривую.
    3. Вызываем перезагрузку конфигурации.
    4. Пытаемся изменить GPIO18 на GPIO19 без рестарта.

    Ожидаемый результат:
    - Некорректная конфигурация отклоняется.
    - Последние рабочие настройки сохраняются.
    - Ошибка фиксируется в config_error.
    - Изменение GPIO требует перезапуска daemon.
    """
    ctl = project.controller()
    ctl.cfg_mtime = 0
    ctl.cfg_rejected_mtime = None
    ctl.config_error = None
    ctl.hardware_signature = (18, 17, 25000)

    invalid = project.get()
    invalid['control']['nvme_levels'][4][1] = 20
    project.put(invalid)
    ctl.reload_config()
    assert ctl.cfg['control']['nvme_levels'][4][1] == 191
    assert 'CONFIG_REJECTED' in ctl.config_error

    changed = project.get()
    changed['control']['nvme_levels'][4][1] = 191
    changed['gpio']['pwm_gpio'] = 19
    project.put(changed)
    ctl.reload_config()
    assert ctl.cfg['gpio']['pwm_gpio'] == 18
    assert 'перезапуск' in ctl.config_error
