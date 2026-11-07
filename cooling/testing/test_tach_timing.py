
#!/usr/bin/env python3
import lgpio
import time
import statistics

CHIP = 0
GPIO = 17
timestamps = []

def on_pulse(chip, gpio, level, timestamp):
    if level == 1:
        timestamps.append(timestamp)

h = lgpio.gpiochip_open(CHIP)
cb = None

try:
    lgpio.gpio_claim_alert(
        h, GPIO, lgpio.RISING_EDGE, lgpio.SET_PULL_NONE
    )

    # Ignore glitches shorter than 100 us
    lgpio.gpio_set_debounce_micros(h, GPIO, 100)

    cb = lgpio.callback(
        h, GPIO, lgpio.RISING_EDGE, on_pulse
    )

    print("Collecting TACH pulses for 10 seconds...")
    time.sleep(10)

    if len(timestamps) < 2:
        print("Not enough pulses")
    else:
        intervals_ms = [
            (b - a) / 1_000_000
            for a, b in zip(timestamps, timestamps[1:])
        ]

        print(f"Pulses: {len(timestamps)}")
        print(f"Min interval: {min(intervals_ms):.3f} ms")
        print(f"Max interval: {max(intervals_ms):.3f} ms")
        print(
            f"Median interval: "
            f"{statistics.median(intervals_ms):.3f} ms"
        )

        print("\nFirst 20 intervals (ms):")
        print([round(x, 3) for x in intervals_ms[:20]])

finally:
    if cb is not None:
        cb.cancel()
    lgpio.gpiochip_close(h)
