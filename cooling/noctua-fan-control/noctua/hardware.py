"""
Аппаратное управление PWM на Raspberry Pi 5 через RP1.

Модуль отвечает за:
- поиск контроллера RP1 PWM0;
- проверку функции GPIO18 (PWM0_CHAN2);
- управление аппаратным PWM через Linux sysfs;
- безопасный приём уже включённого PWM-канала;
- преобразование duty от 0 до 1 в наносекунды.

При закрытии PWM остаётся включённым: отключённый сигнал
может привести к максимальным оборотам четырёхпроводного Noctua.
Значение duty перед завершением работы устанавливает daemon.

Частоту, полярность, электрические уровни и поведение вентилятора
при отключении питания необходимо проверить на Raspberry Pi.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

PWM_ROOT = Path("/sys/class/pwm")
RP1_PWM0_DEVICE = "1f00098000.pwm"
SUPPORTED_PIN = 18
SUPPORTED_CHANNEL = 2


# ------------------------------------------------------------
# Поиск аппаратного PWM RP1
# ------------------------------------------------------------
def detect_rp1_pwm0(root: Path = PWM_ROOT) -> Path:
    """
    Поиск RP1 PWM0 без привязки к номеру pwmchipN.
    """
    matches = []

    for chip in root.glob("pwmchip*"):
        try:
            target = (chip / "device").resolve()
            count = int((chip / "npwm").read_text().strip())
        except (OSError, ValueError):
            continue

        if target.name == RP1_PWM0_DEVICE and count > SUPPORTED_CHANNEL:
            matches.append(chip)

    if len(matches) != 1:
        raise RuntimeError(
            f"RP1 PWM0 ({RP1_PWM0_DEVICE}) не найден однозначно: "
            f"{len(matches)} совпадений. Проверь /sys/class/pwm."
        )

    return matches[0]


def assert_pin_mux_gpio18() -> None:
    """
    Проверка, что GPIO18 настроен как PWM0_CHAN2.
    """
    try:
        result = subprocess.run(
            ["pinctrl", "get", "18"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Не удалось проверить pinctrl GPIO18") from exc

    if result.returncode != 0 or not re.search(r"\bPWM0_CHAN2\b", result.stdout):
        raise RuntimeError(
            "GPIO18 не настроен как PWM0_CHAN2; PWM не запускается. "
            f"pinctrl: {result.stdout.strip()} {result.stderr.strip()}"
        )


# ------------------------------------------------------------
# Аппаратный PWM
# ------------------------------------------------------------
class RP1HardwarePWM:
    """
    Совместимый с PWMOutputDevice интерфейс value (0..1) и close().
    """

    def __init__(
        self,
        pin: int,
        *,
        initial_value: float,
        frequency: int,
        adopt_expected_duty_ns: int | None = None,
    ):
        self.pin = int(pin)
        self.frequency = int(frequency)
        self.period_ns = round(1_000_000_000 / self.frequency)
        self.closed = False
        self._value = None

        if self.pin != SUPPORTED_PIN:
            raise ValueError("Этот backend поддерживает только BCM GPIO18 (PWM0_CHAN2)")
        if not 21_000 <= self.frequency <= 28_000:
            raise ValueError("Для Noctua частота PWM должна быть от 21 до 28 кГц")
        if not 0.0 <= float(initial_value) <= 1.0:
            raise ValueError("initial_value должен быть между 0 и 1")

        assert_pin_mux_gpio18()
        self.chip = detect_rp1_pwm0()
        self.channel = self.chip / f"pwm{SUPPORTED_CHANNEL}"

        # ----------------------------------------------------
        # Приём уже включённого канала PWM
        # ----------------------------------------------------
        # Разрешается только при явном подтверждении состояния.
        if self.channel.is_dir():
            enabled = self._read("enable")

            if enabled != 0:
                current_period = self._read("period")
                current_duty = self._read("duty_cycle")
                current_polarity = (self.channel / "polarity").read_text().strip()

                if (
                    adopt_expected_duty_ns is None
                    or enabled != 1
                    or current_period != self.period_ns
                    or current_polarity != "normal"
                    or current_duty != int(adopt_expected_duty_ns)
                ):
                    raise RuntimeError(
                        f"{self.channel} уже включён: period={current_period}, "
                        f"duty={current_duty}, polarity={current_polarity}, "
                        f"enable={enabled}. Передача возможна только с явным "
                        "подтверждением измеренного состояния через "
                        "NOCTUA_ADOPT_PWM_DUTY_NS."
                    )

                # Принимаем канал без изменения period, duty и enable.
                # Первое новое значение duty задаст основной цикл daemon.
                self._value = current_duty / self.period_ns
                return

        else:
            self._write_path(self.chip / "export", str(SUPPORTED_CHANNEL))

            for _ in range(30):
                if self.channel.is_dir():
                    break
                time.sleep(0.01)

            if not self.channel.is_dir():
                raise RuntimeError(f"После export не появился {self.channel}")
            if self._read("enable") != 0:
                raise RuntimeError(f"{self.channel} неожиданно включён после export")

        # ----------------------------------------------------
        # Настройка периода и начального PWM
        # ----------------------------------------------------
        polarity = (self.channel / "polarity").read_text().strip()
        if polarity != "normal":
            raise RuntimeError(
                f"{self.channel}: ожидалась polarity=normal, сейчас {polarity}"
            )

        old_period = self._read("period")
        old_duty = self._read("duty_cycle")

        # Перед уменьшением периода сбрасываем duty, чтобы duty <= period.
        if old_period > 0 and old_duty > 0:
            self._write("duty_cycle", 0)

        self._write("period", self.period_ns)
        self._write("duty_cycle", round(self.period_ns * float(initial_value)))
        self._write("enable", 1)

        if self._read("enable") != 1:
            raise RuntimeError("Не удалось включить аппаратный PWM")

        self._value = float(initial_value)

    # --------------------------------------------------------
    # Чтение и запись sysfs
    # --------------------------------------------------------
    @staticmethod
    def _write_path(path: Path, text: str) -> None:
        """
        Запись параметра PWM с обработкой ошибок доступа.
        """
        try:
            path.write_text(text, encoding="ascii")
        except PermissionError as exc:
            raise PermissionError(
                f"Нет прав записи в {path}. Требуются разрешённые права на PWM sysfs."
            ) from exc
        except OSError as exc:
            raise OSError(f"Ошибка записи PWM {path}: {exc}") from exc

    def _write(self, name: str, number: int) -> None:
        """
        Запись числового параметра PWM.
        """
        self._write_path(self.channel / name, str(number))

    def _read(self, name: str) -> int:
        """
        Чтение числового параметра PWM.
        """
        try:
            return int((self.channel / name).read_text().strip())
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Не удалось прочитать {self.channel / name}: {exc}"
            ) from exc

    # --------------------------------------------------------
    # Управление duty
    # --------------------------------------------------------
    @property
    def value(self) -> float:
        return self._value

    @value.setter
    def value(self, duty: float) -> None:
        if self.closed:
            raise RuntimeError("PWM backend уже закрыт")

        duty = float(duty)
        if not 0 <= duty <= 1:
            raise ValueError("PWM duty вне диапазона 0..1")

        # duty=0 при enable=1 — не то же самое, что отключённый PWM.
        self._write("duty_cycle", round(self.period_ns * duty))
        self._value = duty

    def close(self) -> None:
        """
        Закрытие backend без отключения или unexport PWM-канала.
        """
        # Перед закрытием daemon устанавливает заданный fallback PWM.
        self.closed = True