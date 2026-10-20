"""Keeps one reader thread per USB serial port, reconnecting when a board is replugged.

Ports are not assigned to sensors up front. Each board announces itself by
the lines it sends (see sensor_protocol.py), so the PPG and temperature boards
can enumerate as /dev/ttyUSB0 or /dev/ttyACM1 in any order after a reboot.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

import serial
from serial.tools import list_ports

log = logging.getLogger("siren.serial")

SCAN_INTERVAL_S = 3.0


class SerialHub:
    def __init__(
        self,
        baud: int,
        explicit_ports: list[str],
        on_line: Callable[[str, bytes], None],
        on_port_event: Callable[[str, str, str | None], None],
        exclude: list[str] | None = None,
    ):
        self.baud = baud
        self.explicit_ports = explicit_ports
        # A USB-serial label printer must not be opened as a sensor. Resolve
        # /dev/serial/by-id/... links to the /dev/ttyUSB* name the scan sees.
        self.exclude = {p for p in (exclude or []) if p} | {os.path.realpath(p) for p in (exclude or []) if p}
        self.on_line = on_line
        self.on_port_event = on_port_event
        self._readers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scanner: threading.Thread | None = None

    def candidate_ports(self) -> list[str]:
        if self.explicit_ports:
            return [p for p in self.explicit_ports if p not in self.exclude]
        # Only USB devices. On Linux this skips the board's own UARTs
        # (ttyS*), on Windows it skips Bluetooth COM ports, either of which
        # can block on open.
        return [p.device for p in list_ports.comports() if p.vid is not None and p.device not in self.exclude]

    def start(self) -> None:
        if self._scanner and self._scanner.is_alive():
            return
        self._stop.clear()
        self._scanner = threading.Thread(target=self._scan_loop, name="serial-scan", daemon=True)
        self._scanner.start()

    def stop(self) -> None:
        self._stop.set()

    def rescan(self) -> None:
        self._scan_once()

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:  # never let a scan error kill the scanner
                log.exception("Serial scan failed")
            self._stop.wait(SCAN_INTERVAL_S)

    def _scan_once(self) -> None:
        for port in self.candidate_ports():
            with self._lock:
                reader = self._readers.get(port)
                if reader and reader.is_alive():
                    continue
                reader = threading.Thread(target=self._read_port, args=(port,), name=f"serial-{port}", daemon=True)
                self._readers[port] = reader
            reader.start()

    def _read_port(self, port: str) -> None:
        try:
            # serial_for_url also accepts pyserial URLs such as socket://host:port,
            # handy for feeding recorded sensor output during development.
            connection = serial.serial_for_url(port, self.baud, timeout=1)
        except (serial.SerialException, OSError, ValueError) as exc:
            self.on_port_event(port, "OPEN_FAILED", str(exc))
            return

        self.on_port_event(port, "OPENED", None)
        try:
            with connection:
                while not self._stop.is_set():
                    line = connection.readline()
                    if line:
                        self.on_line(port, line)
        except (serial.SerialException, OSError) as exc:
            self.on_port_event(port, "LOST", str(exc))
            return
        self.on_port_event(port, "CLOSED", None)
