#!/usr/bin/env python3
"""
Offline test: run the client against a fake Modbus TCP server.

This validates our MBAP framing, u16/u32/s32 decoding, write encoding and
exception handling WITHOUT touching real hardware. Run it before pointing
anything at the SigenStor.

    python3 test_mock.py
"""

from __future__ import annotations

import socket
import struct
import threading

import registers as R
from sigen import ModbusError, SigenClient

# address -> raw 16-bit value
HOLDING = {
    40001: 0xFFFF, 40002: 0xF894,   # s32 -1900  -> -1.90 kW
    40029: 1,
    40030: 0,
    40031: 3,
    40032: 0x0000, 40033: 0x2EE0,   # u32 12000  -> 12.0 kW
    40034: 0x0000, 40035: 0x2EE0,
    40038: 0x0000, 40039: 0x1388,   # u32 5000   -> 5.0 kW
}
INPUT = {
    30014: 875,                      # -> 87.5 %
    30064: 0x0000, 30065: 0x0640,    # u32 1600 -> 16.0 kWh
}


class MockPlant(threading.Thread):
    daemon = True

    def __init__(self, holding: dict | None = None,
                 input_regs: dict | None = None) -> None:
        super().__init__()
        # Default to the module-level tables so existing callers are
        # unaffected; pass your own to get an isolated plant.
        self.holding = HOLDING if holding is None else holding
        self.input = INPUT if input_regs is None else input_regs
        # Set >0 to make the next N requests fail as 'slave device failure',
        # so callers can prove their fail-safe path.
        self.faults = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.writes: list[tuple[int, list[int]]] = []

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            while True:
                header = self._recv(conn, 7)
                if not header:
                    return
                txn, _proto, length, _unit = struct.unpack(">HHHB", header)
                pdu = self._recv(conn, length - 1)
                if not pdu:
                    return
                reply = self._handle(pdu)
                conn.sendall(
                    struct.pack(">HHHB", txn, 0, len(reply) + 1, 247) + reply
                )

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf

    def _handle(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        if self.faults > 0:
            self.faults -= 1
            return struct.pack(">BB", fc | 0x80, 0x04)
        if fc in (0x03, 0x04):
            address, count = struct.unpack(">HH", pdu[1:5])
            table = self.holding if fc == 0x03 else self.input
            values = []
            for i in range(count):
                if address + i not in table:
                    return struct.pack(">BB", fc | 0x80, 0x02)
                values.append(table[address + i])
            return (struct.pack(">BB", fc, count * 2)
                    + struct.pack(f">{count}H", *values))
        if fc == 0x06:
            address, value = struct.unpack(">HH", pdu[1:5])
            self.holding[address] = value
            self.writes.append((address, [value]))
            return pdu
        if fc == 0x10:
            address, count = struct.unpack(">HH", pdu[1:5])
            values = list(struct.unpack(f">{count}H", pdu[6:6 + count * 2]))
            for i, v in enumerate(values):
                self.holding[address + i] = v
            self.writes.append((address, values))
            return struct.pack(">BHH", fc, address, count)
        return struct.pack(">BB", fc | 0x80, 0x01)


def main() -> int:
    plant = MockPlant()
    plant.start()

    failures = []

    def check(label: str, got, want) -> None:
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} "
              f"got {got!r}")
        if not ok:
            failures.append(f"{label}: expected {want!r}, got {got!r}")

    print(f"\nMock plant on 127.0.0.1:{plant.port}\n")

    # The 1 s throttle would make this test take a minute; the framing is
    # what we're checking, so bypass it.
    client = SigenClient("127.0.0.1", port=plant.port)
    client._throttle = lambda: None  # type: ignore[method-assign]

    with client:
        print("Reads")
        check("ESS SOC (u16, gain 10)",
              R.read(client, R.ESS_SOC), 87.5)
        check("Available charge capacity (u32, gain 100)",
              R.read(client, R.ESS_AVAILABLE_CHARGE_CAPACITY), 16.0)
        check("Remote EMS enable (u16)",
              R.read(client, R.REMOTE_EMS_ENABLE), 1)
        check("Remote EMS mode (u16)",
              R.read(client, R.REMOTE_EMS_MODE), 3)
        check("ESS max charge limit (u32, gain 1000)",
              R.read(client, R.ESS_MAX_CHARGE_LIMIT), 12.0)
        check("Active power target (s32, negative)",
              R.read(client, R.ACTIVE_POWER_TARGET), -1.9)

        print("\nWrites")
        client.write_u16(R.REMOTE_EMS_MODE.address,
                         R.EMS_MAX_SELF_CONSUMPTION)
        check("write_u16 mode -> 2",
              client.read_u16(R.REMOTE_EMS_MODE.address), 2)

        client.write_u32(R.ESS_MAX_CHARGE_LIMIT.address, 8500)
        check("write_u32 charge limit -> 8.5 kW",
              R.read(client, R.ESS_MAX_CHARGE_LIMIT), 8.5)
        check("write_u32 used FC16 with 2 registers",
              plant.writes[-1], (40032, [0x0000, 0x2134]))

        print("\nError handling")
        try:
            client.read_u16(49999, holding=True)
        except ModbusError as exc:
            check("illegal address raises ModbusError",
                  "Illegal data address" in str(exc), True)
        else:
            check("illegal address raises ModbusError", False, True)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. Framing and decoding are correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
