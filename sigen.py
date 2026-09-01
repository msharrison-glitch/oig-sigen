"""
Minimal Modbus TCP client for Sigenergy SigenStor plant-level control.

Deliberately stdlib-only -- no pymodbus, no pip install. We only need four
function codes and the protocol is simple enough that a dependency isn't
worth the friction.

Register addresses are used LITERALLY, exactly as printed in the Sigenergy
Modbus Protocol document (e.g. 40029 is sent as 40029 in the PDU, not offset
by 40001). This matches the community Home Assistant integration.

Reference: Sigenergy Modbus Protocol V2.7, 2025-05-23.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

# Plant-level Modbus identity
DEFAULT_PORT = 502
PLANT_UNIT_ID = 247

# The protocol doc requires >= 1000 ms between unicast requests.
MIN_REQUEST_INTERVAL = 1.0

# Function codes
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06
FC_WRITE_MULTIPLE = 0x10

MODBUS_EXCEPTIONS = {
    0x01: "Illegal function",
    0x02: "Illegal data address",
    0x03: "Illegal data value",
    0x04: "Slave device failure",
    0x05: "Acknowledge",
    0x06: "Slave device busy",
    0x08: "Memory parity error",
    0x0A: "Gateway path unavailable",
    0x0B: "Gateway target device failed to respond",
}


class ModbusError(RuntimeError):
    """A Modbus exception response, or a malformed reply."""


class SigenClient:
    """Synchronous Modbus TCP client, rate-limited per the protocol spec.

    Usage:
        with SigenClient("192.168.1.100") as c:
            print(c.read_u16(40031, holding=True))
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        unit_id: int = PLANT_UNIT_ID,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._txn = 0
        self._last_request = 0.0
        self._lock = threading.Lock()

    # -- connection management -------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "SigenClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transport --------------------------------------------------------

    def _throttle(self) -> None:
        """Honour the >= 1000 ms inter-request spacing the spec requires."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

    def _transact(self, pdu: bytes) -> bytes:
        """Send one PDU, return the response PDU (function code stripped)."""
        with self._lock:
            self.connect()
            assert self._sock is not None
            self._throttle()

            self._txn = (self._txn + 1) & 0xFFFF
            header = struct.pack(
                ">HHHB", self._txn, 0, len(pdu) + 1, self.unit_id
            )
            try:
                self._sock.sendall(header + pdu)
                reply_header = self._recv_exactly(7)
                txn, proto, length, unit = struct.unpack(">HHHB", reply_header)
                body = self._recv_exactly(length - 1)
            except (OSError, ModbusError):
                # A broken socket is not recoverable in place; force a
                # reconnect on the next call rather than wedging.
                self.close()
                raise
            finally:
                self._last_request = time.monotonic()

        if txn != self._txn:
            raise ModbusError(
                f"Transaction id mismatch: sent {self._txn}, got {txn}"
            )

        function = body[0]
        if function & 0x80:
            code = body[1]
            raise ModbusError(
                f"Modbus exception 0x{code:02X}: "
                f"{MODBUS_EXCEPTIONS.get(code, 'Unknown')}"
            )
        return body[1:]

    def _recv_exactly(self, n: int) -> bytes:
        assert self._sock is not None
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ModbusError("Connection closed by remote")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- primitives -------------------------------------------------------

    def read_registers(
        self, address: int, count: int, holding: bool = True
    ) -> list[int]:
        """Read `count` 16-bit registers starting at `address`."""
        fc = FC_READ_HOLDING if holding else FC_READ_INPUT
        payload = self._transact(struct.pack(">BHH", fc, address, count))
        byte_count = payload[0]
        data = payload[1 : 1 + byte_count]
        if len(data) != count * 2:
            raise ModbusError(
                f"Short read at {address}: expected {count * 2} bytes, "
                f"got {len(data)}"
            )
        return list(struct.unpack(f">{count}H", data))

    def write_registers(self, address: int, values: list[int]) -> None:
        """Write one or more 16-bit registers starting at `address`."""
        if len(values) == 1:
            self._transact(
                struct.pack(">BHH", FC_WRITE_SINGLE, address, values[0])
            )
            return
        body = struct.pack(
            ">BHHB", FC_WRITE_MULTIPLE, address, len(values), len(values) * 2
        ) + struct.pack(f">{len(values)}H", *values)
        self._transact(body)

    # -- typed accessors --------------------------------------------------

    def read_u16(self, address: int, holding: bool = True) -> int:
        return self.read_registers(address, 1, holding)[0]

    def read_u32(self, address: int, holding: bool = True) -> int:
        hi, lo = self.read_registers(address, 2, holding)
        return (hi << 16) | lo

    def read_s32(self, address: int, holding: bool = True) -> int:
        value = self.read_u32(address, holding)
        return value - (1 << 32) if value & (1 << 31) else value

    def write_u16(self, address: int, value: int) -> None:
        self.write_registers(address, [value & 0xFFFF])

    def write_u32(self, address: int, value: int) -> None:
        self.write_registers(
            address, [(value >> 16) & 0xFFFF, value & 0xFFFF]
        )
