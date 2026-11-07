#!/usr/bin/env python3

import fcntl
import signal
import sys
import threading
import time

import lgpio


# =========================
# Configuration
# =========================

GPIO_CHIP = 0
TACH_GPIO = 17

PULSES_PER_REV = 2
MEASURE_SECONDS = 1.0

DEBOUNCE_US = 100
LOCK_FILE = "/tmp/noctua_rpm_gpio17.lock"


# =========================
# State
# =========================

pulse_count = 0
pulse_lock = threading.Lock()
stop_event = threading.Event()


def tach_callback(chip, gpio, level, timestamp):
    """Count rising edges from the tachometer."""
    global pulse_count

    if gpio != TACH_GPIO or level != 1:
        return

    with pulse_lock:
        pulse_count += 1


def signal_handler(signum, frame):
    """Request graceful shutdown."""
    stop_event.set()


def main():
    global pulse_count

    handle = None
    callback = None
    gpio_claimed = False

    # Prevent multiple instances.
    lock_file = open(LOCK_FILE, "w")

    try:
        fcntl.flock(
            lock_file.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )
    except BlockingIOError:
        print(
            "ERROR: Noctua RPM monitor is already running.",
            file=sys.stderr
        )
        lock_file.close()
        return 1

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Open GPIO controller.
        handle = lgpio.gpiochip_open(GPIO_CHIP)

        # Configure tachometer input.
        lgpio.gpio_claim_alert(
            handle,
            TACH_GPIO,
            lgpio.RISING_EDGE,
            lgpio.SET_PULL_NONE
        )

        gpio_claimed = True

        # Filter very short glitches.
        lgpio.gpio_set_debounce_micros(
            handle,
            TACH_GPIO,
            DEBOUNCE_US
        )

        # Register callback.
        callback = lgpio.callback(
            handle,
            TACH_GPIO,
            lgpio.RISING_EDGE,
            tach_callback
        )

        print("Noctua NF-A4x20 5V PWM")
        print(f"GPIO chip: {GPIO_CHIP}")
        print(f"TACH GPIO: {TACH_GPIO}")
        print(f"Pulses/revolution: {PULSES_PER_REV}")
        print(f"Measurement interval: {MEASURE_SECONDS}s")
        print("Press Ctrl+C to stop")
        print("-" * 46, flush=True)

        while not stop_event.is_set():

            with pulse_lock:
                pulse_count = 0

            start = time.monotonic()

            # Wait can be interrupted by shutdown.
            if stop_event.wait(MEASURE_SECONDS):
                break

            elapsed = time.monotonic() - start

            with pulse_lock:
                pulses = pulse_count

            rpm = (
                pulses * 60.0
                / (PULSES_PER_REV * elapsed)
            )

            status = "RUNNING" if pulses > 0 else "NO SIGNAL"

            print(
                f"Pulses: {pulses:5d} | "
                f"Time: {elapsed:.2f}s | "
                f"RPM: {rpm:7.0f} | "
                f"{status}",
                flush=True
            )

        print("\nShutdown requested.")

    except lgpio.error as exc:
        print(f"\nGPIO ERROR: {exc}", file=sys.stderr)
        print(
            "Check GPIO availability and existing processes.",
            file=sys.stderr
        )
        return 1

    finally:
        print("Cleaning up GPIO...", flush=True)

        try:
            if callback is not None:
                callback.cancel()
        finally:
            try:
                if handle is not None and gpio_claimed:
                    lgpio.gpio_free(handle, TACH_GPIO)
            finally:
                try:
                    if handle is not None:
                        lgpio.gpiochip_close(handle)
                finally:
                    fcntl.flock(
                        lock_file.fileno(),
                        fcntl.LOCK_UN
                    )
                    lock_file.close()

        print("GPIO released.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

