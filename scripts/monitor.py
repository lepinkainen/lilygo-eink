#!/usr/bin/env python3
"""Headless serial monitor for the LilyGo T5 4.7" V2.3 (ESP32-S3 native USB-CDC).

Usage: uvx --from pyserial python scripts/monitor.py [seconds]

The S3's USB-JTAG/serial peripheral is a true USB-CDC device, so the DTR/RTS
download-mode trap that exists on UART-bridged boards does not apply here.
We do not assert RTS to reset; we just read whatever the running firmware
prints. With 15-minute deep-sleep cycles, expect long silences punctuated by
~1-second bursts at each wake.
"""
import glob
import sys
import time

import serial


def wait_for_port(timeout: float) -> str:
    """Poll for /dev/cu.usbmodem* until one shows up or timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = sorted(glob.glob("/dev/cu.usbmodem*"))
        if candidates:
            return candidates[0]
        time.sleep(0.2)
    raise SystemExit(
        f"No /dev/cu.usbmodem* device after {timeout:.0f}s. "
        "Tap RST on the board (or replug)."
    )


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    port = wait_for_port(timeout=min(duration, 15.0))
    try:
        s = serial.Serial(port, 115200, timeout=1)
    except serial.SerialException as e:
        raise SystemExit(f"open {port}: {e}")
    end = time.time() + duration
    while time.time() < end:
        try:
            n = s.in_waiting
        except OSError:
            # Port can vanish mid-read when the firmware deep-sleeps.
            sys.stdout.write("\n[port disappeared — chip likely deep-slept]\n")
            sys.stdout.flush()
            return
        if n:
            sys.stdout.write(s.read(n).decode("utf-8", errors="replace"))
            sys.stdout.flush()
        else:
            time.sleep(0.05)


if __name__ == "__main__":
    main()
