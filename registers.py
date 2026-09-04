"""
Sigenergy plant-level register map.

Every entry below was taken from Sigenergy Modbus Protocol V2.7 (2025-05-23)
and verified to read cleanly against real hardware on 2026-08-30.

Addresses are literal -- see the note in sigen.py.

`gain` is the divisor applied to the raw integer to get engineering units,
so raw 875 with gain 10 is 87.5 %.
"""

from __future__ import annotations

from dataclasses import dataclass

# A U32 register reading all-bits-set has never been configured. The protocol
# doc doesn't name this a sentinel, but 4294967.295 kW is not a real limit.
U32_UNSET = 0xFFFFFFFF


@dataclass(frozen=True)
class Register:
    address: int
    name: str
    kind: str          # "u16" | "u32" | "s32"
    holding: bool      # True = 4xxxx (RW), False = 3xxxx (RO input)
    gain: float = 1.0
    unit: str = ""
    note: str = ""

    @property
    def writable(self) -> bool:
        return self.holding


# --- Remote EMS control mode enum (register 40031) ----------------------

EMS_PCS_REMOTE_CONTROL = 0
EMS_STANDBY = 1
EMS_MAX_SELF_CONSUMPTION = 2
EMS_COMMAND_CHARGE_GRID_FIRST = 3   # <- the Intelligent Go slot mode
EMS_COMMAND_CHARGE_PV_FIRST = 4
EMS_COMMAND_DISCHARGE_PV_FIRST = 5
EMS_COMMAND_DISCHARGE_ESS_FIRST = 6

EMS_MODE_NAMES = {
    EMS_PCS_REMOTE_CONTROL: "PCS remote control",
    EMS_STANDBY: "Standby",
    EMS_MAX_SELF_CONSUMPTION: "Maximum self-consumption",
    EMS_COMMAND_CHARGE_GRID_FIRST: "Command charging (grid first)",
    EMS_COMMAND_CHARGE_PV_FIRST: "Command charging (PV first)",
    EMS_COMMAND_DISCHARGE_PV_FIRST: "Command discharging (PV first)",
    EMS_COMMAND_DISCHARGE_ESS_FIRST: "Command discharging (ESS first)",
}


# --- Writable control registers -----------------------------------------

REMOTE_EMS_ENABLE = Register(
    40029, "Remote EMS enable", "u16", True,
    note="0 = disabled, 1 = enabled. Releasing control means writing 0 here, "
         "which returns the plant to its own configured EMS work mode.",
)

INDEPENDENT_PHASE_ENABLE = Register(
    40030, "Independent phase power control enable", "u16", True,
    note="Only valid when output type is L1/L2/L3/N. We never write this.",
)

REMOTE_EMS_MODE = Register(
    40031, "Remote EMS control mode", "u16", True,
    note="Only takes effect while 40029 = 1.",
)

ESS_MAX_CHARGE_LIMIT = Register(
    40032, "ESS max charging limit", "u32", True, gain=1000, unit="kW",
    note="Applies only in modes 3-6.",
)

ESS_MAX_DISCHARGE_LIMIT = Register(
    40034, "ESS max discharging limit", "u32", True, gain=1000, unit="kW",
    note="Applies only in modes 3-6.",
)

# --- ESS SOC limits (40046-40048) ---------------------------------------
#
# ORDERING SETTLED 2026-09-04 against the community integration
# (TypQxQ/Sigenergy-Local-Modbus, modbusregisterdefinitions.py), which names
# them BACKUP first:
#
#     40046 backup SOC  |  40047 charge cut-off  |  40048 discharge cut-off
#
# An earlier version of this file had them as charge / discharge / backup and
# called the ordering unverified, because 40046 reading 0 % looked like "never
# charge" on a plant that was plainly charging. We were simply off by one:
# 40046 is the backup reserve, and 0 % there is normal. This plant reads
# 40046 = 0 %, 40047 = 100 %, 40048 = 0 % -- no reserve, charge to full,
# discharge to empty, which is exactly "no restriction".
#
# Still READ-ONLY here by choice. The ordering is settled but the write
# semantics are not tested on this firmware, and a wrong write silently stops
# the battery. See the limits lesson above.

ESS_BACKUP_SOC = Register(
    40046, "ESS backup reserve SOC", "u16", True,
    gain=10, unit="%",
    note="Reserve held back for backup. 0 % here means no reserve.",
)

ESS_CHARGE_CUTOFF_SOC = Register(
    40047, "ESS charge cut-off SOC", "u16", True,
    gain=10, unit="%",
    note="Charging stops here. 100 % on this plant.",
)

ESS_DISCHARGE_CUTOFF_SOC = Register(
    40048, "ESS discharge cut-off SOC", "u16", True, gain=10, unit="%",
    note="Discharging stops here. 0 % on this plant.",
)


GRID_MAX_EXPORT_LIMIT = Register(
    40038, "Grid point max export limitation", "u32", True,
    gain=1000, unit="kW",
    note="Grid sensor required. Takes effect regardless of EMS mode.",
)

ACTIVE_POWER_TARGET = Register(
    40001, "Active power fixed adjustment target", "s32", True,
    gain=1000, unit="kW",
)


# --- Operational mode (READ-ONLY) ----------------------------------------
#
# An earlier version of this file stated flatly that the app's operational
# mode has NO Modbus register at plant or device level. That was wrong, and
# it cost real money: on 2026-09-03 a cloud restore silently did not take and
# the plant sat on a charging profile for eight hours, undetected, because we
# believed the only way to see the mode was the unofficial cloud API.
#
# We missed it by searching the HOLDING range (40040-40120, 40500-40560) for
# something WRITABLE. It is an input register, 30003, and read-only -- so the
# cloud is still the only way to SET the mode, but not to SEE it.
#
# Named plant_ems_work_mode by the community integration. Enum below.

EMS_WORK_MODE_MAX_SELF_CONSUMPTION = 0
EMS_WORK_MODE_AI = 1
EMS_WORK_MODE_TOU = 2
EMS_WORK_MODE_FULL_FEED_IN = 5

EMS_WORK_MODE_NAMES = {
    EMS_WORK_MODE_MAX_SELF_CONSUMPTION: "Maximum Self-Powered",
    EMS_WORK_MODE_AI: "Sigen AI",
    EMS_WORK_MODE_TOU: "TOU",
    EMS_WORK_MODE_FULL_FEED_IN: "Fully Fed to Grid",
}

# The numbering is ASSUMED to match the cloud API's operationMode, on one
# observation: 2026-09-04, 30003 = 1 while the cloud reported currentMode 1
# (Sigen AI). One agreeing data point is not a mapping. Until it has been
# watched through an actual change, this is logged and never acted on.
EMS_WORK_MODE = Register(
    30003, "EMS work mode (app operational mode)", "u16", False,
    note="Read-only. Enum mapping to the cloud API is provisional.",
)


# --- Read-only telemetry -------------------------------------------------

GRID_ACTIVE_POWER = Register(
    30005, "Grid sensor active power", "s32", False, gain=1000, unit="kW",
    note="Positive = importing from grid.",
)

PLANT_ACTIVE_POWER = Register(
    30031, "Plant active power", "s32", False, gain=1000, unit="kW",
)

PV_POWER = Register(
    30035, "PV power", "s32", False, gain=1000, unit="kW",
)

ESS_POWER = Register(
    30037, "ESS charge/discharge power", "s32", False, gain=1000, unit="kW",
    note="Positive = charging, negative = discharging.",
)

PLANT_RUNNING_STATE = Register(
    30051, "Plant running state", "u16", False,
)

ESS_SOC = Register(
    30014, "ESS state of charge", "u16", False, gain=10, unit="%",
)

ESS_AVAILABLE_CHARGE_CAPACITY = Register(
    30064, "ESS available max charging capacity", "u32", False,
    gain=100, unit="kWh",
)

ESS_RATED_CHARGE_POWER = Register(
    30068, "ESS rated charging power", "u32", False, gain=1000, unit="kW",
)

ESS_RATED_DISCHARGE_POWER = Register(
    30070, "ESS rated discharging power", "u32", False, gain=1000, unit="kW",
)

ESS_RATED_CAPACITY = Register(
    30083, "ESS rated energy capacity", "u32", False, gain=100, unit="kWh",
)


# Read in this order during a probe. Read-only first, so a failure on the
# writable block still leaves us with useful telemetry.
PROBE_SET: list[Register] = [
    ESS_SOC,
    ESS_AVAILABLE_CHARGE_CAPACITY,
    ESS_RATED_CAPACITY,
    ESS_RATED_CHARGE_POWER,
    ESS_RATED_DISCHARGE_POWER,
    ESS_POWER,
    PV_POWER,
    GRID_ACTIVE_POWER,
    PLANT_ACTIVE_POWER,
    PLANT_RUNNING_STATE,
    REMOTE_EMS_ENABLE,
    INDEPENDENT_PHASE_ENABLE,
    REMOTE_EMS_MODE,
    ESS_MAX_CHARGE_LIMIT,
    ESS_MAX_DISCHARGE_LIMIT,
    GRID_MAX_EXPORT_LIMIT,
    ACTIVE_POWER_TARGET,
    ESS_BACKUP_SOC,
    ESS_CHARGE_CUTOFF_SOC,
    ESS_DISCHARGE_CUTOFF_SOC,
]

# The compact set used for live monitoring during a control lease.
LIVE_SET: list[Register] = [
    EMS_WORK_MODE,
    ESS_SOC,
    ESS_POWER,
    PV_POWER,
    GRID_ACTIVE_POWER,
]


def read_raw(client, reg: Register) -> int:
    """Read one Register's raw integer value, no gain applied."""
    if reg.kind == "u16":
        return client.read_u16(reg.address, reg.holding)
    if reg.kind == "u32":
        return client.read_u32(reg.address, reg.holding)
    if reg.kind == "s32":
        return client.read_s32(reg.address, reg.holding)
    raise ValueError(f"Unknown register kind: {reg.kind}")


def read(client, reg: Register):
    """Read one Register, applying its gain.

    Returns None for a U32 register that has never been configured, so
    callers can distinguish 'unset' from a real 4294967.295 kW limit.
    """
    raw = read_raw(client, reg)
    if reg.kind == "u32" and raw == U32_UNSET:
        return None
    return raw / reg.gain if reg.gain != 1.0 else raw
