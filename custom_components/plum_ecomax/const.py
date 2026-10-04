"""Constants and Configuration Maps for Plum EcoMAX.

This module defines all the constant values, mapping dictionaries, and
configuration schemas used throughout the integration. It acts as the
central repository for:

* **Domain & Defaults**: Integration domain and default connection ports.
* **Mappings**: Translation maps between Plum device codes and Home Assistant states (HVAC, Presets).
* **Entity Definitions**: Configuration dictionaries for Sensors, Climates, Switches, etc.
* **Unit Definitions**: Standard units imported from Home Assistant.

Attributes:
    DOMAIN (str): The integration domain ('plum_ecomax').
    DEFAULT_PORT (int): The default TCP port for the ecoNET module (8899).
    CONF_ACTIVE_CIRCUITS (str): Configuration key for active heating circuits.
    UPDATE_INTERVAL (int): Polling interval in seconds (30).
    PLUM_TO_HA_HVAC (dict): Mapping from Plum WorkMode (0-3) to HA HVAC Modes.
    SENSOR_TYPES (dict): Definitions of available sensors [Unit, Icon, DeviceClass].
"""

from homeassistant.const import (
    PERCENTAGE,
    UnitOfPower,
    UnitOfTemperature,
)

# --- OPERATING MODE (pid 161) + solar-dump service ---
# The ecoSTER "manual control" service screen writes pid 161 = 2 to put the
# controller in manual mode and = 1 to return to automatic. Confirmed by
# passive RS-485 bus capture over several clean enter/exit cycles
# (IMPROVEMENT_PLAN_ARCHIVE.md section N): MANUAL_MODE_SLUG bit 64 follows it exactly,
# and a write of pid 161 = 2 from this integration was validated live to
# raise that bit and be cleanly reversible. Manual mode is the ONLY state in
# which hdwpumpforce (and the other force overrides) physically take effect.
OPERATING_MODE_SLUG = "operatingmode"
OPERATING_MODE_AUTO = 1
OPERATING_MODE_MANUAL = 2

# plum_ecomax.solar_to_buffer: manual mode + forced DHW pump for a capped
# duration, then guaranteed return to automatic. Used to bank solar-heated
# DHW into the buffer tanks ahead of a forecast overcast spell.
SOLAR_DUMP_FORCE_SLUG = "hdwpumpforce"
SOLAR_DUMP_FORCE_VALUE = 512
SOLAR_DUMP_MAX_MINUTES = 120

# DHW-temperature guard rails for the solar dump (service + the "DHW pump ->
# Solar buffer" switch). Exposed as RestoreNumber entities; the values are
# mirrored onto the coordinator so solar_dump.py can read them. A dump won't
# start below the start threshold, and stops once the DHW tank drops to the
# stop threshold -- so it doesn't drain the tank too far.
SOLAR_DUMP_START_TEMP_DEFAULT = 50
SOLAR_DUMP_STOP_TEMP_DEFAULT = 42
SOLAR_DUMP_TEMP_MIN = 25
SOLAR_DUMP_TEMP_MAX = 75

# --- AUTOMATIC solar dump (differential-temperature controller) ---
# A tick every AUTO_TICK_SECONDS while switch.solar_dump_auto is on: run the
# transfer pump in bursts when the DHW tank is hotter than the buffer by
# >= dt_start, stop when the gradient falls below AUTO_DT_STOP; charge hard
# until the buffer reaches its target, then only skim big gradients
# (dt_start * AUTO_DT_BALANCE_FACTOR) so both tanks drift up together with
# minimal circulator time. Never drains the DHW tank below the auto floor.
AUTO_TICK_SECONDS = 150
AUTO_DT_STOP = 3
AUTO_BUFFER_CEILING = 75
AUTO_MIN_RUN_SECONDS = 300
AUTO_MIN_REST_SECONDS = 720
AUTO_DT_BALANCE_FACTOR = 2
SOLAR_DUMP_BUFFER_TARGET_DEFAULT = 55
SOLAR_DUMP_DT_START_DEFAULT = 8
SOLAR_DUMP_DAILY_BUDGET_DEFAULT = 120
SOLAR_DUMP_AUTO_FLOOR_DEFAULT = 45

# All the solar-dump RestoreNumber entities, built from one loop in number.py.
# key -> (default, min, max, step, unit, icon, coordinator attribute).
# unit "C" = temperature, "K" = a temperature difference, "min" = minutes.
SOLAR_DUMP_NUMBERS = {
    "start_temp": (
        SOLAR_DUMP_START_TEMP_DEFAULT,
        SOLAR_DUMP_TEMP_MIN,
        SOLAR_DUMP_TEMP_MAX,
        1,
        "C",
        "mdi:thermometer-chevron-up",
        "solar_dump_start_temp",
    ),
    "stop_temp": (
        SOLAR_DUMP_STOP_TEMP_DEFAULT,
        SOLAR_DUMP_TEMP_MIN,
        SOLAR_DUMP_TEMP_MAX,
        1,
        "C",
        "mdi:thermometer-chevron-down",
        "solar_dump_stop_temp",
    ),
    "auto_ecs_floor": (
        SOLAR_DUMP_AUTO_FLOOR_DEFAULT,
        30,
        70,
        1,
        "C",
        "mdi:water-thermometer",
        "solar_dump_auto_ecs_floor",
    ),
    "buffer_target": (
        SOLAR_DUMP_BUFFER_TARGET_DEFAULT,
        30,
        80,
        1,
        "C",
        "mdi:target",
        "solar_dump_buffer_target",
    ),
    "dt_start": (
        SOLAR_DUMP_DT_START_DEFAULT,
        2,
        25,
        1,
        "K",
        "mdi:delta",
        "solar_dump_dt_start",
    ),
    "daily_budget": (
        SOLAR_DUMP_DAILY_BUDGET_DEFAULT,
        0,
        600,
        10,
        "min",
        "mdi:timer-cog-outline",
        "solar_dump_daily_budget",
    ),
}

# --- CONFIGURATION SWITCH (ON/OFF) ---
SWITCH_TYPES = {
    # Format: "slug": (friendly_name, on_value, off_value)
    "hdwstartoneloading": ("Force DHW reload", 1, 0),
    # 0x200 = bit 9 levé = marche forcée pompe ECS vers ballon solaire (ID 172)
    "hdwpumpforce": ("Force pompe ECS → ballon solaire", 512, 0),
    "hdwstartlegion": ("Cycle anti-légionellose", 1, 0),
    "operatingmode": ("Manual mode", OPERATING_MODE_MANUAL, OPERATING_MODE_AUTO),
}

# Switches that belong in the device's Configuration section rather than the
# main Controls card (a heavier action / not day-to-day).
CONFIG_SWITCHES = {"operatingmode"}

# --- CONFIGURATION SELECT (DROPDOWN) ---

# Mapping specific to DHW (ECS) Mode
# 0 = Off, 1 = Manual/Constant, 2 = Schedule/Auto
DHW_MODES_TO_HA = {0: "off", 1: "manual", 2: "auto"}

HA_TO_DHW_MODES = {"off": 0, "manual": 1, "auto": 2}

# Format: "slug": ("Friendly Name", Map_To_HA, Map_To_Plum)
SELECT_TYPES = {
    "hdwusermode": ("DHW Mode", DHW_MODES_TO_HA, HA_TO_DHW_MODES),
}

# --- LOCAL CONSTANT DEFINITIONS (Independent of HA) ---
# We define our own standard values to avoid any import issues
HVAC_MODE_OFF = "off"
HVAC_MODE_HEAT = "heat"
HVAC_MODE_AUTO = "auto"

PRESET_AWAY = "away"
PRESET_COMFORT = "comfort"
PRESET_ECO = "eco"
# -----------------------------------------------------------

# Mapping Plum -> Home Assistant
PLUM_TO_HA_HVAC = {
    0: HVAC_MODE_HEAT,  # Frost protection (0) = Active heating
    1: HVAC_MODE_HEAT,  # Comfort
    2: HVAC_MODE_HEAT,  # Eco
    3: HVAC_MODE_AUTO,  # Auto
}

PLUM_TO_HA_PRESET = {
    0: PRESET_AWAY,
    1: PRESET_COMFORT,
    2: PRESET_ECO,
}

# Inverse Mapping Home Assistant -> Plum
HA_TO_PLUM_HVAC = {
    HVAC_MODE_OFF: 0,
    HVAC_MODE_AUTO: 3,
}

HA_TO_PLUM_PRESET = {
    PRESET_AWAY: 0,
    PRESET_COMFORT: 1,
    PRESET_ECO: 2,
}

DOMAIN = "plum_ecomax"
DEFAULT_PORT = 8899

CONF_ACTIVE_CIRCUITS = "active_circuits"

# Simplified Mapping (Just the keys)
CIRCUIT_CHOICES = ["1", "2", "3", "4", "5", "6", "7"]

UPDATE_INTERVAL = 30

# Options/config flow key + bounds for making the polling interval tunable
# per install (some networks/boilers tolerate faster or need slower polling
# than the 30s default). Enforced in config_flow.py's schema.
CONF_UPDATE_INTERVAL = "update_interval"
MIN_UPDATE_INTERVAL = 10
MAX_UPDATE_INTERVAL = 300

# --- PARAMETERS USED IN DEVICE REGISTRY METADATA, NOT AS ENTITIES ---
# Still need to be polled into coordinator.data so device_info properties
# can read them (see device.py's boiler_device_info(serial_number=...)).
DEVICE_INFO_PARAMS = ["uid"]
# Factory serial number: the config entry's unique_id when readable (an IP can
# change), and the device registry's serial_number.
SERIAL_SLUG = "uid"

# --- BINARY SENSOR CONFIGURATION ---
# "Manual mode active": heatsourcemainpumpstate bit 6 (value 64). Empirically
# confirmed reliable across 3 independent physical panel tests
# (IMPROVEMENT_PLAN_ARCHIVE.md section H) -- the only state in which manual
# overrides like the hdwpumpforce switch actually have a physical effect;
# writing them while the panel isn't in manual mode is accepted and held by
# the boiler but does nothing.
MANUAL_MODE_SLUG = "heatsourcemainpumpstate"
MANUAL_MODE_BIT = 64

# Alarm bitmask registers (DP_INVENTORY.md "Alarmes & bits de diagnostic").
# Only the registers whose name unambiguously means "alarm" AND that read
# zero on a healthy boiler are exposed as a coarse "some bit is set" problem
# indicator -- individual bit meanings aren't documented and haven't been
# empirically decoded the way MANUAL_MODE_BIT was. alarmbits_1..5 are all 0
# on the live boiler (dp_scan capture) with no panel alarms.
#
# detectalarmstate / detectalarmsettings / workstate2-4 are deliberately NOT
# here: their names read as configuration/extended-state registers rather
# than live alarm flags, and both detect* registers are observed with a
# byte-filled value and zero alarms on the physical panel
# (detectalarmsettings=65280 / 0xFF00, detectalarmstate=16711680 / 0xFF0000)
# -- treating either as "problem" is a permanent false positive (a red
# entity plus an unfixable repair issue). They are exposed as plain
# diagnostic integer sensors instead (see DIAGNOSTIC_SENSOR_SLUGS below).
ALARM_BITMASK_SLUGS = [
    "alarmbits_1",
    "alarmbits_2",
    "alarmbits_3",
    "alarmbits_4",
    "alarmbits_5",
]

# --- SENSOR CONFIGURATION ---
# Format: "slug": [Unit, Icon, DeviceClass] (3 elements)
SENSOR_TYPES = {
    "tempwthr": [UnitOfTemperature.CELSIUS, "mdi:thermometer", "temperature"],
    "boilerpower": [UnitOfPower.KILO_WATT, "mdi:flash", "power"],
    "tempcwu": [UnitOfTemperature.CELSIUS, "mdi:water-boiler", "temperature"],
    "hdwpumpstate": [None, "mdi:pump", None],
    "tempbuforup": [UnitOfTemperature.CELSIUS, "mdi:water", "temperature"],
    "tempbufordown": [UnitOfTemperature.CELSIUS, "mdi:water", "temperature"],
    "tempclutch": [UnitOfTemperature.CELSIUS, "mdi:fire-alert", "temperature"],
    "buforsetpoint": [UnitOfTemperature.CELSIUS, "mdi:target", "temperature"],
    # No "tempcircuit1": circuit 1 has no dedicated flow-temp register in
    # the device map (id sequence 61,62,63,64,65,[gap],66=tempcircuit2,67=
    # tempcircuit3,...) -- circuit 1's room temperature is already covered
    # via circuit1thermostattemp, and climate.py's own fallback chain
    # doesn't need this slug either. See IMPROVEMENT_PLAN_ARCHIVE.md.
    "tempcircuit2": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit3": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit4": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit5": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit6": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit7": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit1thermostattemp": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit2thermostattemp": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit3thermostattemp": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit4thermostattemp": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit5thermostattemp": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    # No "circuit6thermostattemp" in the device map either -- climate.py's
    # fallback chain (circuitNthermostattemp -> tempcircuitN) already
    # covers circuit 6 via tempcircuit6, this was only a dead sensor entry.
    "circuit7thermostattemp": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "mixer1valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer2valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer3valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer4valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer5valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer6valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer7valveposition": [PERCENTAGE, "mdi:valve", None],
    # RAW/STRING parameters (device.py's RAW decode, DP_INVENTORY.md).
    # "uid" is deliberately NOT a sensor -- device.py's boiler_device_info()
    # shows it as the device registry's serial_number instead. It's kept
    # polled via DEVICE_INFO_PARAMS below.
    "circuit1name": [None, "mdi:label-outline", None],
    "circuit2name": [None, "mdi:label-outline", None],
    "circuit3name": [None, "mdi:label-outline", None],
    "circuit4name": [None, "mdi:label-outline", None],
    "circuit5name": [None, "mdi:label-outline", None],
    "circuit6name": [None, "mdi:label-outline", None],
    "circuit7name": [None, "mdi:label-outline", None],
    # Raw diagnostic registers (DP_INVENTORY.md "Alarmes & bits de
    # diagnostic"). Exposed as plain integers rather than decoded/booleans:
    # see ALARM_BITMASK_SLUGS above for why these aren't treated as "problem"
    # binary sensors. Gated into EntityCategory.DIAGNOSTIC via
    # DIAGNOSTIC_SENSOR_SLUGS below.
    "detectalarmsettings": [None, "mdi:bell-cog-outline", None],
    "detectalarmstate": [None, "mdi:bell-cog-outline", None],
    "workstate2": [None, "mdi:chip", None],
    "workstate3": [None, "mdi:chip", None],
    "workstate4": [None, "mdi:chip", None],
    # DHW circulation pump state (DP_INVENTORY.md "Circulation ECS") -- raw
    # register, meaning of the bits undocumented, so kept as a diagnostic int.
    "circulationstate": [None, "mdi:pump", None],
}

# SENSOR_TYPES slugs shown in the device's "Diagnostic" section instead of
# the main entity card -- raw registers meaningful mostly for troubleshooting
# (see comment on the sensors above).
DIAGNOSTIC_SENSOR_SLUGS = {
    "detectalarmsettings",
    "detectalarmstate",
    "workstate2",
    "workstate3",
    "workstate4",
    "circulationstate",
}

# --- THERMOSTATS ---
# One list per circuit of *every slug climate.py can read at runtime* -- the
# coordinator's initial scan (`_detect_available_parameters`) only ever polls
# slugs it finds here, so anything climate.py reads that's missing from this
# list is never refreshed after startup. That was the case for
# `circuitNactive` (the HVAC on/off state): climate.py read it but the list
# only had `circuitNworkstate`/`circuitNecotemp` (which no entity reads), so
# the thermostat always reported "heat" and an OFF written from HA snapped
# back at the next poll. Keep this in sync with the slugs climate.py's
# properties actually touch.
CLIMATE_TYPES = {
    str(i): [
        f"circuit{i}thermostattemp",  # current temp (primary)
        f"tempcircuit{i}",  # current temp (fallback, see climate.py)
        f"circuit{i}comforttemp",  # target temp
        f"circuit{i}active",  # HVAC on/off state
    ]
    for i in range(1, 8)
}

NUMBER_TYPES: dict[str, tuple[float, float, float, str]] = {
    # Force buffer tank loading for N minutes (0 = disabled)
    # Bypasses the normal temperature comparison logic (useful for solar pre-heating)
    "buforlongloadtime": (0, 180, 1, "mdi:timer"),
    # Anti-legionella cycle (DP_INVENTORY.md)
    "hdwlegionsetpoint": (60, 80, 1, "mdi:bacteria-outline"),
    "hdwlegionday": (0, 7, 1, "mdi:calendar"),
    "hdwlegionhour": (0, 23, 1, "mdi:clock-outline"),
    # DHW circulation pump timing (DP_INVENTORY.md "Circulation ECS").
    # Routed to the DHW device, EntityCategory.CONFIG (see number.py's
    # _ADVANCED_NUMBER_PREFIXES).
    "circulationtempstart": (20, 60, 1, "mdi:thermometer-water"),
    "circulationhisttemp": (1, 10, 1, "mdi:thermometer-minus"),
    "circulationtimework": (0, 60, 1, "mdi:timer-play"),
    "circulationtimestop": (0, 60, 1, "mdi:timer-pause"),
}

# Per-circuit heating curve tuning (DP_INVENTORY.md "Circuits — courbes de
# chauffe & limites"). Circuit 6's device map has no curvefloor/curveradiator
# entries (only circuits 1-5 and 7 do), so it's skipped for those two.
# Cooling setpoint bounds (DP_INVENTORY.md "Non catégorisés") exist for all
# 7 circuits.
for _circuit_id in range(1, 8):
    if _circuit_id != 6:
        NUMBER_TYPES[f"circuit{_circuit_id}curvefloor"] = (0.1, 4.0, 0.1, "mdi:chart-bell-curve")
        NUMBER_TYPES[f"circuit{_circuit_id}curveradiator"] = (0.1, 4.0, 0.1, "mdi:chart-bell-curve")
    NUMBER_TYPES[f"circuit{_circuit_id}basetemp"] = (20, 90, 1, "mdi:thermometer")
    NUMBER_TYPES[f"circuit{_circuit_id}tempreduction"] = (0, 15, 1, "mdi:thermometer-minus")
    NUMBER_TYPES[f"circuit{_circuit_id}minsetpointcooling"] = (
        10,
        40,
        1,
        "mdi:snowflake-thermometer",
    )
    NUMBER_TYPES[f"circuit{_circuit_id}maxsetpointcooling"] = (
        10,
        40,
        1,
        "mdi:snowflake-thermometer",
    )


WEEKDAY_TO_SLUGS = {
    0: ("mondayam", "mondaypm"),
    1: ("tuesdayam", "tuesdaypm"),
    2: ("wednesdayam", "wednesdaypm"),
    3: ("thursdayam", "thursdaypm"),
    4: ("fridayam", "fridaypm"),
    5: ("saturdayam", "saturdaypm"),
    6: ("sundayam", "sundaypm"),
}

# --- WATER HEATER CONFIGURATION ---
# Format: "Name": (Current_Temp, Setpoint, Min, Max, Mode_Slug, Force_Slug)
WATER_HEATER_TYPES = {
    "hdw": (
        "tempcwu",  # Current temperature
        "hdwtsetpoint",  # Setpoint
        "hdwminsettemp",  # Min bound
        "hdwmaxsettemp",  # Max bound
        "hdwusermode",  # Mode (0=Off, 1=Manual, 2=Auto)
    )
}

# Mapping Plum modes to Home Assistant Water Heater
# Off = Off
# Manual = Performance (or Gas/Electric)
# Auto = Eco
PLUM_TO_HA_WATER_HEATER = {
    0: "off",
    1: "performance",  # Considered as "Manual / Permanent Comfort"
    2: "eco",  # Considered as "Auto / Schedule"
}

HA_TO_PLUM_WATER_HEATER = {"off": 0, "performance": 1, "eco": 2}

SCHEDULE_TYPES = {}
for i in range(1, 8):  # Circuits 1 to 7
    for suffix_am, suffix_pm in WEEKDAY_TO_SLUGS.values():
        SCHEDULE_TYPES[f"circuit{i}{suffix_am}"] = f"Circuit {i} AM"
        SCHEDULE_TYPES[f"circuit{i}{suffix_pm}"] = f"Circuit {i} PM"

for suffix_am, suffix_pm in WEEKDAY_TO_SLUGS.values():
    SCHEDULE_TYPES[f"hdw{suffix_am}"] = "DHW AM"
    SCHEDULE_TYPES[f"hdw{suffix_pm}"] = "DHW PM"


# --- POLLING FRESHNESS ---
# Slugs that essentially never change on their own: setpoints, heating
# curves, weekly-schedule bitmasks, min/max bounds, circuit names, serial.
# The coordinator polls these with a long TTL (coordinator.DEFAULT_TTL).
# Everything else an entity reads is treated as live telemetry and re-read
# every polling cycle, so the configurable polling interval actually governs
# how fresh sensor data is (previously the hardcoded 300s TTL did, making
# the interval almost cosmetic -- a temperature refreshed once every 5 min
# regardless of a 10-300s setting).
STATIC_SLUGS = frozenset(
    set(NUMBER_TYPES)
    | set(SCHEDULE_TYPES)
    | set(DEVICE_INFO_PARAMS)
    | {f"circuit{i}name" for i in range(1, 8)}
    | {f"circuit{i}comforttemp" for i in range(1, 8)}
    | {"hdwtsetpoint", "hdwminsettemp", "hdwmaxsettemp"}
)
