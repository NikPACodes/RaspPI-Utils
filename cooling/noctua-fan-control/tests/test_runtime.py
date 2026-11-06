"""
Интеграционные тесты мониторинга и управляющего daemon.

Проверяем:
- Обнаружение устаревшего status.json.
- Работу CLI-команды settings.
- Режим read-only мониторинга CPU/HAT.
- Инициализацию PWM/TACH GPIO.
- Несколько последовательных циклов управления.
- Правильную запись итогового status.json.
- Освобождение тестовых GPIO-ресурсов.

Используются FakePWM/FakeTACH из conftest.py.
Настоящие GPIO Raspberry Pi не открываются.
"""
import json
import subprocess
import sys
import time
import pytest

from conftest import ROOT, daemon
from noctua import monitor

pytestmark = pytest.mark.runtime

def test_01_status_stale_is_not_current_rpm(project, capsys):
    """
    Устаревший status.json не считается актуальным.

    Проверка:
    Записываем status.json с timestamp на 100 секунд раньше текущего времени и RPM=9999.

    Ожидаемый результат:
    - Монитор обнаруживает устаревший статус.
    - Выводится предупреждение STATUS STALE.
    - Значение 9999 RPM не показывается как текущее.

    Защищает пользователя от ошибочных показаний, если управляющий daemon перестал работать.
    """
    project.status_path.write_text(json.dumps({'timestamp': time.time() - 100, 'rpm': 9999, 'applied_pwm': 153,}),
                                              encoding='utf-8')
    monitor.status()
    output = capsys.readouterr().out
    assert 'STATUS STALE' in output
    assert '9999 rpm' not in output


def test_02_fanctl_settings_and_cpu_monitor_are_read_only(project, capsys):
    """
    CLI и CPU/HAT мониторинг.

    Проверки:
    1. Запускаем fanctl.py settings отдельным процессом.
    2. Проверяем наличие таблиц CPU и NVMe.
    3. Вызываем отображение информации CPU/HAT кулера.

    Ожидаемый результат:
    - CLI завершается с кодом 0.
    - Настройки температурных кривых отображаются.
    - CPU/HAT монитор помечен как read-only.

    Тест не подтверждает принадлежность найденного источника RPM конкретному вентилятору Waveshare.
    """
    cli = subprocess.run([sys.executable, 'fanctl.py', 'settings'],
                         cwd=ROOT, text=True, capture_output=True, check=False)
    assert cli.returncode == 0, cli.stderr
    assert 'CPU levels' in cli.stdout and 'NVMe levels' in cli.stdout
    monitor.cpu_fan_info()
    assert 'read-only' in capsys.readouterr().out


def test_03_daemon_three_cycles_with_fake_gpio(project, monkeypatch):
    """
    Интеграционный запуск daemon без реального GPIO.

    Проверки:
    1) Имитируем GPIO-контроллер RP1.
    2) CPU стабильно показывает 60°C.
    3) NVMe стабильно показывает 40°C.
    4) Daemon выполняет три цикла управления.

    Ожидаемый результат:
    - PWM и TACH используют GPIO18 и GPIO17.
    - Заданная частота PWM составляет 25000 Гц.
    - CPU запрашивает PWM=115.
    - NVMe запрашивает PWM=51.
    - Итоговый PWM=115, источник CPU.
    - status.json содержит корректные значения.
    - После close() тестовые GPIO освобождены.
    """
    class StopAfter:
        """
        Завершает тестовый цикл после заданного числа итераций.
        """
        def __init__(self, loops):
            self.loops = loops
            self.done = 0

        def is_set(self):
            return self.done >= self.loops

        def wait(self, seconds):
            self.done += 1

    monkeypatch.setattr(daemon, 'STOP', StopAfter(3))
    monkeypatch.setattr(daemon, 'find_rp1_gpiochip', lambda: 0)
    monkeypatch.setattr(daemon, 'cpu_temp_c', lambda: 60.0)
    monkeypatch.setattr(daemon, 'nvme_current_temp_c', lambda: (40.0, [40.0]))

    controller = daemon.Controller()
    assert controller.pwm.pin == 18 and controller.tach.pin == 17
    assert controller.pwm.frequency == 25000
    controller.loop()
    state = json.loads(project.status_path.read_text(encoding='utf-8'))
    assert state['pwm_cpu_requested'] == 115
    assert state['pwm_nvme_requested'] == 51
    assert state['control_source'] == 'CPU'
    assert state['applied_pwm'] == 115
    assert state['nvme_average_samples_used'] == 3
    assert state['config_error'] is None
    controller.close()
    assert controller.pwm.closed and controller.tach.closed
