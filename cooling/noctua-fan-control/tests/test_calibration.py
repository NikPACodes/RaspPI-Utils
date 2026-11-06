"""
Тесты калибровки baseline RPM вентилятора Noctua.

Проверяем:
- Запрет калибровки без работающего daemon.
- Соответствие запрошенного и применённого PWM.
- Обработку отсутствующих показаний RPM.
- Сохранность предыдущего baseline при ошибках.
- Корректную запись новой калибровки.
- Восстановление предыдущего режима AUTO/MANUAL.

В тестах используются смоделированные значения RPM.
Физический вентилятор не запускается.
"""
import os
import time

import pytest
from conftest import calibration

pytestmark = pytest.mark.calibration

def test_01_calibration_no_running_daemon_does_not_change_config(project):
    """
    Калибровка запрещена без работающего daemon.

    Проверка:
    Запускаем calibrate(), когда актуальный статус управляющего процесса отсутствует.

    Ожидаемый результат:
    - Калибровка завершается RuntimeError.
    - config.json не изменяется.
    - Предыдущий baseline остаётся сохранённым.
    """
    previous = project.config_path.read_bytes()
    with pytest.raises(RuntimeError, match='Daemon не отвечает'):
        calibration.calibrate()
    assert project.config_path.read_bytes() == previous


def test_02_calibration_rejects_mismatched_pwm(project, monkeypatch):
    """
    Запрет калибровки при неправильном PWM.
    Защищает от некорректной калибровки при аварийном переопределении PWM.

    Проверки:
    - В конфигурации уже сохранён baseline.
    - Калибровка запрашивает очередной PWM.
    - Daemon сообщает фактически применённый PWM=255.

    Ожидаемый результат:
    - Несовпадение PWM обнаруживается.
    - Новые значения RPM не записываются в baseline.
    - Предыдущий baseline сохраняется.
    - Исходный режим AUTO восстанавливается.
    """
    cfg = project.get()
    cfg['health']['baseline_rpm'] = {'77': 1500}
    project.put(cfg)
    monkeypatch.setattr(calibration, 'current_state', lambda: {'timestamp': time.time(),
                                                                      'daemon_pid': os.getpid(),
                                                                      'rpm': 1600,
                                                                      'mode': 'manual',
                                                                      'applied_pwm': 255,})
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(calibration.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='другое значение'):
        calibration.calibrate()
    assert project.get()['health']['baseline_rpm'] == {'77': 1500}
    assert project.get()['control']['mode'] == 'auto'


def test_03_calibration_preserves_baseline_when_rpm_missing(project, monkeypatch):
    """
    Отсутствие RPM не уничтожает baseline.

    Проверки:
    1) Имеется ранее сохранённая калибровка.
    2) Daemon сообщает корректный применённый PWM.
    3) Но измеренные RPM постоянно равны нулю.

    Ожидаемый результат:
    - Калибровка обнаруживает недостаток валидных проб.
    - Новый baseline не сохраняется.
    - Предыдущие значения RPM остаются прежними.
    - Режим AUTO восстанавливается.
    """
    cfg = project.get()
    cfg['health']['baseline_rpm'] = {'77': 1500}
    project.put(cfg)
    count = iter(range(100))

    def no_rpm():
        c = project.get()
        return {'timestamp': next(count), 'daemon_pid': os.getpid(),
                'mode': c['control']['mode'],
                'applied_pwm': c['control']['manual_pwm'], 'rpm': 0}

    monkeypatch.setattr(calibration, 'current_state', no_rpm)
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(calibration.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='четырёх'):
        calibration.calibrate()
    assert project.get()['health']['baseline_rpm'] == {'77': 1500}
    assert project.get()['control']['mode'] == 'auto'


def test_04_calibration_success_saves_and_restores_auto(project, monkeypatch):
    """
    Успешная калибровка и восстановление AUTO.
    Проверка алгоритма калибровки, а не достоверности реальной зависимости PWM/RPM.

    Проверка:
    Для каждого PWM создаём стабильное показание RPM: RPM = PWM * 20

    Калибровка последовательно обрабатывает уровни из температурных кривых и записывает baseline.

    Ожидаемый результат:
    - Все измеренные уровни сохранены.
    - PWM=255 исключён из длительной калибровки.
    - Режим AUTO восстановлен.
    - Предыдущее значение manual_pwm не потеряно.
    """
    counter = iter(range(1, 100))

    def simulated_rpm():
        c = project.get()
        pwm = c['control']['manual_pwm']
        return {'timestamp': next(counter), 'daemon_pid': os.getpid(),
                'mode': c['control']['mode'],
                'applied_pwm': pwm, 'rpm': pwm * 20}

    monkeypatch.setattr(calibration, 'current_state', simulated_rpm)
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(calibration.time, 'sleep', lambda _: None)
    calibration.calibrate()
    result = project.get()
    assert result['health']['baseline_rpm'] == {
        '77': 1540, '115': 2300, '153': 3060, '191': 3820, '230': 4600,
    }
    assert '255' not in result['health']['baseline_rpm']
    assert result['control']['mode'] == 'auto'
    assert result['control']['manual_pwm'] == 128
