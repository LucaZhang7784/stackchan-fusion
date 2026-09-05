"""Serial log capture for latency measurement (COM8, 115200).
Runs hidden in background; writes timestamped lines to latency_check/serial_*.log
"""
import os
import sys
import time

import serial
from serial.tools import list_ports

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"
BAUD = 115200
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
STACKCHAN_PORT = "COM8"
STACKCHAN_VID = 0x303A
STACKCHAN_PID = 0x1001


def stamp():
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"


def validate_stackchan_port() -> None:
    """Hard safety boundary: StackChan diagnostics may only open its ESP32-S3 COM8."""
    if PORT.upper() != STACKCHAN_PORT:
        raise SystemExit(f"refusing {PORT}: StackChan diagnostics only permit {STACKCHAN_PORT}")
    info = next((p for p in list_ports.comports() if p.device.upper() == STACKCHAN_PORT), None)
    if info is None:
        raise SystemExit(f"{STACKCHAN_PORT} not present")
    if info.vid != STACKCHAN_VID or info.pid != STACKCHAN_PID:
        raise SystemExit(
            f"refusing {STACKCHAN_PORT}: expected VID_{STACKCHAN_VID:04X}:PID_{STACKCHAN_PID:04X}, "
            f"got VID_{(info.vid or 0):04X}:PID_{(info.pid or 0):04X}"
        )


def main():
    validate_stackchan_port()
    log_path = os.path.join(LOG_DIR, time.strftime("serial_%Y%m%d_%H%M%S.log"))
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{stamp()}] === capture started port={PORT} baud={BAUD} ===\n")
        f.flush()
        ser = serial.Serial(PORT, BAUD, timeout=0.2)
        ser.dtr = False
        ser.rts = False
        while True:
            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                out = f"[{stamp()}] {line}"
                f.write(out + "\n")
                f.flush()
                print(out, flush=True)
            except KeyboardInterrupt:
                break
            except Exception as e:  # keep running on transient errors
                f.write(f"[{stamp()}] CAPTURE-ERR {e}\n")
                f.flush()
                time.sleep(1)


if __name__ == "__main__":
    main()
