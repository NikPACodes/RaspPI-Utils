"""
Тесты алгоритма управления Noctua NF-A4x20 5V PWM.

Проверяем:
- Температурные кривые CPU и NVMe.
- Приоритет охлаждения между двумя датчиками.
- Гистерезис и выдержку перед уменьшением PWM.
- Усреднение температуры NVMe.
- Режимы AUTO/MANUAL и аварийные пороги.
- Измерение RPM через TACH.
- Start-boost при запуске вентилятора с нуля.
- Предупреждение о снижении RPM относительно baseline.

Все температуры, импульсы и значения PWM моделируются.
Физическое поведение вентилятора этими тестами не подтверждается.
"""

import pytest
from conftest import daemon

pytestmark = pytest.mark.policy

def test_01_interpolation_and_two_sensor_priority(project):
    """
    Интерполяция PWM и приоритет CPU/NVMe.

    Проверки:
    1. Проверяем PWM ниже, между и выше LEVEL.
    2. CPU требует большего PWM, чем NVMe.
    3. NVMe требует большего PWM, чем CPU.

    Ожидаемый результат:
    - Интерполяция возвращает правильный PWM.
    - Итоговый PWM равен максимуму двух запросов.
    - Указывается правильный источник управления.
    """
    ctl = project.controller()
    curve = ctl.cfg['control']['nvme_levels']
    assert daemon.interp_pwm(20, curve) == 0
    assert daemon.interp_pwm(47.5, curve) == 134
    assert daemon.interp_pwm(80, curve) == 255
    assert ctl.choose_pwm(60, 40, 40)[:4] == (115, 115, 51, 'CPU')

    # Новый контроллер: предыдущие latch-значения не должны влиять на следующий независимый сценарий.
    ctl = project.controller()
    assert ctl.choose_pwm(50, 55, 55)[:4] == (191, 51, 191, 'NVME')


def test_02_hysteresis_three_degrees_and_dwell(project, monkeypatch):
    """
    Гистерезис 3°C и выдержка 15 секунд.
    Проверяется логику кода, а не реальная температура CPU или NVMe.

    Проверки:
    1) Исходная температура управления 46°C.
    2) Повышение до 48.9°C ещё не проходит гистерезис.
    3) Повышение до 49°C достигает порога.
    4) Понижение до 42°C сначала блокируется выдержкой.
    5) После 15 секунд снижение разрешается.

    Ожидаемый результат:
    - Мелкие температурные скачки не меняют PWM.
    - Повышение на 3°C принимается.
    - Снижение разрешено после выдержки.
    """
    ctl = project.controller()
    ctl.last_policy_change_time = 100
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 100.5)
    assert ctl.hysteresis_temp(48.9, 46) == (46, False)
    assert ctl.hysteresis_temp(49, 46) == (49, False)

    ctl.last_policy_change_time = 100
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 110)
    assert ctl.hysteresis_temp(42, 46) == (46, False)

    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 116)
    assert ctl.hysteresis_temp(42, 46) == (42, True)


def test_03_nvme_averaging_and_sensor_loss(project, monkeypatch):
    """
    Усреднение NVMe и потеря датчика.

    Проверки:
    1) Пять последовательных измерений NVMe: 49, 50, 52, 50, 49°C.
    2) Датчик перестаёт возвращать температуру.

    Ожидаемый результат:
    - После пяти измерений средняя температура 50°C.
    - История содержит пять выборок.
    - При потере датчика история очищается.
    - Устаревшее среднее не используется как текущее.

    Усреднение пяти выборок, не обязательно фиксированное временное окно в пять секунд.
    """
    ctl = project.controller()
    samples = iter([49, 50, 52, 50, 49])
    monkeypatch.setattr(daemon, 'cpu_temp_c', lambda: 50)
    monkeypatch.setattr(daemon, 'nvme_current_temp_c', lambda: (next(samples), [50]))
    for _ in range(5):
        result = ctl.read_temperatures()
    assert result[2] == 50.0
    assert len(ctl.nvme_history) == 5
    monkeypatch.setattr(daemon, 'nvme_current_temp_c', lambda: (None, []))
    assert ctl.read_temperatures()[2] is None
    assert len(ctl.nvme_history) == 0


def test_04_emergency_manual_and_missing_sensors(project):
    """
    MANUAL, аварийные температуры и отсутствие датчиков.

    Проверки:
    1. Включаем MANUAL с PWM=0.
    2. Повышаем CPU до аварийного порога 75°C.
    3. Повышаем NVMe до аварийного порога 65°C.
    4. Моделируем отсутствие обоих датчиков.
    5. Проверяем минимальный ненулевой PWM.

    Ожидаемый результат:
    - MANUAL PWM=0 принимается при обычных температурах.
    - Аварийный перегрев имеет приоритет над MANUAL.
    - При потере датчиков применяется PWM=153.
    - Ненулевой PWM ниже 51 поднимается до 51.
    """
    ctl = project.controller()
    ctl.cfg['control'].update(mode='manual', manual_pwm=0)

    assert ctl.choose_pwm(40, 40, 40)[0] == 0
    assert ctl.choose_pwm(75, 40, 40)[0] == 255
    assert ctl.choose_pwm(40, 65, 40)[0] == 255
    assert ctl.choose_pwm(None, None, None)[0] == 153

    ctl.cfg['control']['manual_pwm'] = 10
    assert ctl.choose_pwm(40, 40, 40)[0] == 51


def test_05_rpm_formula_and_tach_failsafe(project, monkeypatch):
    """
    Формула RPM и реакция на отсутствие TACH.

    Проверки:
    1) За одну секунду получено 187 импульсов.
    2) В конфигурации используется 2 импульса/оборот.
    3) Затем моделируется длительное отсутствие RPM.

    Формула:
    RPM = pulses * 60 / (time_seconds * pulses_per_rev)

    Ожидаемый результат:
    - Расчёт RPM даёт 5610 об/мин.
    - При отсутствии оборотов срабатывает TACH fail-safe.
    - Итоговый PWM по текущей конфигурации равен 153.
    - Возвращается предупреждение TACH.
    """
    ctl = project.controller()
    ctl.last_rpm_time = 10
    ctl.pulses = 187
    ctl.applied_pwm = 153
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 11)
    ctl.update_rpm()

    # За 1 секунду, PPR=2: 187 * 60 / (1 * 2) = 5610 RPM.
    assert ctl.rpm_ema == pytest.approx(5610)

    ctl.zero_since = 100
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 110)
    target, alarm = ctl.apply_tach_failsafe(77)
    assert target == 153
    assert 'TACH' in alarm


def test_06_start_boost_only_from_stopped_state(project, monkeypatch):
    """
    Start-boost только при запуске с нуля.

    Проверки:
    1. Вентилятор находится в состоянии PWM=0.
    2. Запрашиваем небольшой PWM=51.
    3. Проверяем кратковременное повышение до 255.
    4. Повышаем PWM с 51 до 77.

    Ожидаемый результат:
    - Переход 0→51 сначала устанавливает PWM=255.
    - Продолжительность boost равна 0.7 секунды.
    - Затем устанавливается PWM=51.
    - При переходе 51→77 boost не повторяется.
    """
    ctl = project.controller()
    ctl.applied_pwm = 0
    delays = []
    monkeypatch.setattr(daemon.time, 'sleep', delays.append)
    ctl.set_pwm(51)
    assert ctl.pwm.values[-2:] == [1.0, 51 / 255]
    assert delays == [0.7]

    ctl.set_pwm(77)
    assert ctl.pwm.value == pytest.approx(77 / 255)
    assert delays == [0.7]


def test_07_rpm_baseline_warning_requires_30_seconds(project, monkeypatch):
    """
    Предупреждение о снижении RPM относительно baseline.

    Проверки:
    1) Первоначально baseline отсутствует.
    2) Для PWM=115 задаём baseline=2000 RPM.
    3) Фактические RPM моделируем на уровне 1000.
    4) Проверяем состояние сразу и через 31 секунду.

    Ожидаемый результат:
    - Без baseline возвращается NO_BASELINE.
    - Сначала возвращается OBSERVE.
    - После выдержки возвращается CHECK_FAN.
    - Отношение RPM/baseline равно 0.5.

    Предупреждение означает необходимость проверки охлаждения.
    """
    ctl = project.controller()
    assert ctl.health()[0] == 'NO_BASELINE'
    ctl.applied_pwm = 115
    ctl.rpm_ema = 1000
    ctl.cfg['health']['baseline_rpm'] = {'115': 2000}
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 100)
    assert ctl.health()[0] == 'OBSERVE'
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 131)
    result = ctl.health()
    assert result[0] == 'CHECK_FAN'
    assert result[1] == 2000
    assert result[2] == pytest.approx(0.5)
