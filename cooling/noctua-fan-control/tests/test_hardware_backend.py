"""
Тесты аппаратного PWM RP1 и определения GPIO-контроллера.

Проверяем:
- Обнаружение PWM0 по аппаратному идентификатору, а не номеру pwmchip.
- Запрет перехвата активного PWM без подтверждения его состояния.
- Сохранение включённого выхода при duty=0 и после close().
- Безопасную установку периода и проверку полярности.
- Ограничение GPIO18 и проверку pinmux.
- Обнаружение номера /dev/gpiochipN для TACH.
- Передачу активного PWM без пропадания сигнала.

Все операции с sysfs, pinctrl и gpiodetect моделируются.
Реальные GPIO Raspberry Pi не используются.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import daemon
from noctua import hardware

pytestmark = pytest.mark.hardware

def setup_fake_sysfs(tmp_path: Path, *, enabled='0',
                                        period='0',
                                        duty='0',
                                        polarity='normal',) -> Path:
    """
    Создаёт временную структуру sysfs для RP1 PWM0, канал 2.
    """
    chip = tmp_path / 'pwmchip0'
    chip.mkdir()
    (chip / 'npwm').write_text('4')

    device = tmp_path / 'devices' / '1f00098000.pwm'
    device.mkdir(parents=True)
    (chip / 'device').symlink_to(device, target_is_directory=True)

    channel = chip / 'pwm2'
    channel.mkdir()
    for name, value in (('enable', enabled),
                        ('period', period),
                        ('duty_cycle', duty),
                        ('polarity', polarity),):
        (channel / name).write_text(value)

    return channel


def mock_pwm_channel(monkeypatch, channel: Path) -> None:
    """
    Направляет тестовый PWM backend на временный sysfs-канал.
    """
    monkeypatch.setattr(hardware, 'detect_rp1_pwm0', lambda: channel.parent)
    monkeypatch.setattr(hardware, 'assert_pin_mux_gpio18', lambda: None)


def test_01_detect_rp1_pwm0_from_device_name(tmp_path):
    """
    Поиск RP1 PWM0 по идентификатору устройства.

    Проверка:
    Создаём контроллер с идентификатором 1f00098000.pwm.

    Ожидаемый результат:
    - Возвращается нужный pwmchip независимо от его номера.
    """
    channel = setup_fake_sysfs(tmp_path)
    assert hardware.detect_rp1_pwm0(root=tmp_path) == channel.parent


def test_02_detect_missing_rp1_fails_closed(tmp_path):
    """
    Отсутствие RP1 PWM0.

    Проверка:
    Ищем контроллер в пустом sysfs.

    Ожидаемый результат:
    - RuntimeError вместо выбора неподходящего устройства.
    """
    with pytest.raises(RuntimeError, match='не найден'):
        hardware.detect_rp1_pwm0(root=tmp_path)


@pytest.mark.parametrize(
    'period, duty, expected_duty',
    [
        pytest.param('40000', '20000', None, id='без-подтверждения'),
        pytest.param('40000', '18000', 20000, id='другое-duty'),
        pytest.param('50000', '20000', 20000, id='другой-период'),
    ],
)
def test_03_active_pwm_rejected_without_matching_handoff(tmp_path, monkeypatch,
                                                         period, duty, expected_duty):
    """
    Запрет перехвата активного PWM при отсутствии точного подтверждения.

    Проверки:
    1) Не передаём разрешение на захват уже включённого канала.
    2) Передаём разрешение, но фактический duty или period не совпадает.

    Ожидаемый результат:
    - Все три ситуации завершаются RuntimeError.
    - period, duty_cycle и enable остаются без изменений.
    """
    channel = setup_fake_sysfs(tmp_path, enabled='1', period=period, duty=duty)
    mock_pwm_channel(monkeypatch, channel)

    with pytest.raises(RuntimeError, match='уже включён'):
        hardware.RP1HardwarePWM(18, initial_value=0.6, frequency=25000, adopt_expected_duty_ns=expected_duty,)

    assert (channel / 'period').read_text() == period
    assert (channel / 'duty_cycle').read_text() == duty
    assert (channel / 'enable').read_text() == '1'


def test_04_zero_duty_keeps_output_enabled(tmp_path, monkeypatch):
    """
    PWM=0 и закрытие backend не отключают аппаратный канал.

    Проверки:
    1) Запускаем PWM с duty 60% и периодом 40000 нс.
    2) Переходим на PWM=0 и вызываем close().

    Ожидаемый результат:
    - Начальный duty равен 24000 нс.
    - После PWM=0 duty равен нулю, enable остаётся равным 1.
    - close() не отключает канал.
    """
    channel = setup_fake_sysfs(tmp_path, period='40000', duty='10000')
    mock_pwm_channel(monkeypatch, channel)

    pwm = hardware.RP1HardwarePWM(18, initial_value=0.6, frequency=25000)
    assert (channel / 'period').read_text() == '40000'
    assert (channel / 'duty_cycle').read_text() == '24000'

    pwm.value = 0.0
    assert (channel / 'duty_cycle').read_text() == '0'
    assert (channel / 'enable').read_text() == '1'

    pwm.close()
    assert (channel / 'enable').read_text() == '1'


def test_05_shorter_period_resets_old_duty(tmp_path, monkeypatch):
    """
    Безопасная смена периода при слишком большом прежнем duty.

    Проверка:
    Уменьшаем период с 80000 до 40000 нс при старом duty=60000 нс.

    Ожидаемый результат:
    - Период становится равным 40000 нс, duty — 20000 нс.
    """
    channel = setup_fake_sysfs(tmp_path, period='80000', duty='60000')
    mock_pwm_channel(monkeypatch, channel)

    hardware.RP1HardwarePWM(18, initial_value=0.5, frequency=25000)

    assert (channel / 'period').read_text() == '40000'
    assert (channel / 'duty_cycle').read_text() == '20000'


def test_06_inverted_polarity_rejected(tmp_path, monkeypatch):
    """
    Запрет запуска с инверсной полярностью.

    Проверка:
    Подготавливаем канал с polarity=inversed.

    Ожидаемый результат:
    - RuntimeError, канал не включается.
    """
    channel = setup_fake_sysfs(tmp_path, polarity='inversed')
    mock_pwm_channel(monkeypatch, channel)

    with pytest.raises(RuntimeError, match='polarity=normal'):
        hardware.RP1HardwarePWM(18, initial_value=0.5, frequency=25000)

    assert (channel / 'enable').read_text() == '0'


def test_07_only_gpio18_allowed():
    """
    Ограничение аппаратного backend на GPIO18.

    Проверка:
    Запрашиваем GPIO17 вместо GPIO18.

    Ожидаемый результат:
    - ValueError до обращения к аппаратуре.
    """
    with pytest.raises(ValueError, match='GPIO18'):
        hardware.RP1HardwarePWM(17, initial_value=0.5, frequency=25000)


def test_08_gpiodetect_selects_rp1_for_tach(monkeypatch):
    """
    Определение настоящего номера GPIO-чипа для TACH.

    Проверки:
    1) Имитируем gpiodetect с RP1 и дополнительным gpio-brcmstb.
    2) Имитируем существование /dev/gpiochip0.

    Ожидаемый результат:
    - Выбирается gpiochip0 с идентификатором pinctrl-rp1.
    """
    monkeypatch.setattr(
        daemon.subprocess,
        'run',
        lambda *args, **kwargs: SimpleNamespace(
                returncode=0,
                stdout=('gpiochip0 [pinctrl-rp1] (54 lines)\n'
                        'gpiochip10 [gpio-brcmstb@107d508500] (32 lines)\n'),
                stderr='',
        ),
    )

    original_exists = Path.exists
    monkeypatch.setattr(
        Path,
        'exists',
        lambda self: str(self) == '/dev/gpiochip0' or original_exists(self),
    )
    assert daemon.find_rp1_gpiochip() == 0


def test_09_mux_requires_hardware_pwm(monkeypatch):
    """
    GPIO18 должен быть назначен на PWM0_CHAN2.

    Проверка:
    Имитируем режим input в выводе pinctrl.

    Ожидаемый результат:
    - RuntimeError, запуск с неправильным pinmux отклоняется.
    """
    monkeypatch.setattr(
        hardware.subprocess,
        'run',
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='18: ip pn | hi // GPIO18 = input',
            stderr='',
        ),
    )
    with pytest.raises(RuntimeError, match='не настроен'):
        hardware.assert_pin_mux_gpio18()


def test_10_explicit_handoff_keeps_signal(tmp_path, monkeypatch):
    """
    Передача работающего PWM без пропадания сигнала.

    Проверки:
    1) Принимаем канал с подтверждёнными period=40000 и duty=20000.
    2) Меняем duty до 60%, затем закрываем backend.

    Ожидаемый результат:
    - При принятии сохраняются period, duty_cycle и enable.
    - После изменения duty становится 24000.
    - После close() канал остаётся включённым.
    """
    channel = setup_fake_sysfs(tmp_path, enabled='1', period='40000', duty='20000')
    mock_pwm_channel(monkeypatch, channel)

    pwm = hardware.RP1HardwarePWM(18, initial_value=0.6, frequency=25000, adopt_expected_duty_ns=20000)
    assert pwm.value == 0.5
    assert (channel / 'period').read_text() == '40000'
    assert (channel / 'duty_cycle').read_text() == '20000'
    assert (channel / 'enable').read_text() == '1'

    pwm.value = 0.6
    assert (channel / 'duty_cycle').read_text() == '24000'
    assert (channel / 'enable').read_text() == '1'

    pwm.close()
    assert (channel / 'enable').read_text() == '1'