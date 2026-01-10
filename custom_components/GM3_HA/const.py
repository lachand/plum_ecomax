from homeassistant.const import (
    UnitOfTemperature,
    PERCENTAGE,
    UnitOfPower,
    UnitOfTime,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_IP_ADDRESS,
    CONF_PORT
)

DOMAIN = "plum_ecomax"
DEFAULT_PORT = 8899

CONF_ACTIVE_CIRCUITS = "active_circuits"

# Mapping simplifié (Juste les clés)
CIRCUIT_CHOICES = ["1", "2", "3", "4", "5", "6", "7"]

UPDATE_INTERVAL = 30

# --- CONFIGURATION DES CAPTEURS ---
# Format: "slug": [Unité, Icone, DeviceClass] (3 éléments)
SENSOR_TYPES = {
    "tempwthr": [UnitOfTemperature.CELSIUS, "mdi:thermometer", "temperature"],
    "boilerpower": [UnitOfPower.KILO_WATT, "mdi:flash", "power"],
    "worktime": [UnitOfTime.SECONDS, "mdi:clock-outline", None],
    "tempcwu": [UnitOfTemperature.CELSIUS, "mdi:water-boiler", "temperature"],
    "tempbufordown": [UnitOfTemperature.CELSIUS, "mdi:tank", "temperature"],
    "buforsetpoint": [UnitOfTemperature.CELSIUS, "mdi:target", "temperature"],

    "tempcircuit1": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit2": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit3": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit4": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit5": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit6": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit7": [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    
    "circuit1thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit2thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit3thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit4thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit5thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit6thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "circuit7thermostattemp" : [UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],

    "mixer1valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer2valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer3valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer4valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer5valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer6valveposition": [PERCENTAGE, "mdi:valve", None],
    "mixer7valveposition": [PERCENTAGE, "mdi:valve", None],
}

# --- THERMOSTATS ---
CLIMATE_TYPES = {
    "1": ["tempcircuit1", "circuit2comforttemp"],
    "2": ["tempcircuit2", "circuit2comforttemp"],
    "3": ["tempcircuit3", "circuit2comforttemp"],
    "4": ["tempcircuit4", "circuit2comforttemp"],
    "5": ["tempcircuit5", "circuit2comforttemp"],
    "6": ["tempcircuit6", "circuit2comforttemp"],
    "7": ["tempcircuit7", "circuit2comforttemp"],
}

# --- WATER HEATER ---
WATER_HEATER_CONFIG = {
    # Nom supprimé ici, géré par clé de trad "eau_chaude_sanitaire"
    "current": "tempcwu",
    "target": "hdwtsetpoint",
    "min": "hdwminsettemp",
    "max": "hdwmaxsettemp",
}

# --- NUMBER ---
# Format: "slug": [min, max, step, Icone] (4 éléments)
NUMBER_TYPES = {
    "hdwtsetpoint": [20, 70, 1, "mdi:water-thermometer"],
    "hdwminsettemp": [10, 50, 1, "mdi:thermometer-chevron-down"],
    "hdwmaxsettemp": [50, 80, 1, "mdi:thermometer-chevron-up"],
}