"""
Точка расширения для вентиляторов Raspberry Pi и Noctua.

Идет тестирование GPIO/sysfs вариантов схем подключения Noctua.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class FanSnapshot:
    rpm: int | None = None
    pwm_percent: float | None = None


def read_cpu_fan() -> FanSnapshot:
    return FanSnapshot()


def read_noctua_fan() -> FanSnapshot:
    return FanSnapshot()
