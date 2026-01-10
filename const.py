from homeassistant.const import (
    UnitOfTemperature,
    PERCENTAGE,
    UnitOfPower,
    UnitOfTime,
)

DOMAIN = "plum_ecomax"
CONF_IP_ADDRESS = "ip_address"
CONF_UID = "uid"

# Délai de rafraîchissement (30 secondes pour ne pas surcharger)
UPDATE_INTERVAL = 30

# --- CONFIGURATION DES CAPTEURS (Lecture Seule) ---
# Format: "slug": ["Nom Affiché", Unité, Icone, DeviceClass]
SENSOR_TYPES = {
    # GÉNÉRAL
    "tempwthr": ["Température Extérieure", UnitOfTemperature.CELSIUS, "mdi:thermometer", "temperature"],
    "boilerpower": ["Puissance Chaudière", UnitOfPower.KILO_WATT, "mdi:flash", "power"],
    "worktime": ["Temps de travail total", UnitOfTime.SECONDS, "mdi:clock-outline", None],
    
    # EAU CHAUDE SANITAIRE (ECS / CWU)
    "tempcwu": ["Température ECS", UnitOfTemperature.CELSIUS, "mdi:water-boiler", "temperature"],
    
    # BALLON TAMPON (BUFFER)
    "tempbufordown": ["Ballon Tampon (Bas)", UnitOfTemperature.CELSIUS, "mdi:tank", "temperature"],
    "buforsetpoint": ["Consigne Ballon Tampon", UnitOfTemperature.CELSIUS, "mdi:target", "temperature"],

    # CIRCUITS MÉLANGEURS (TEMPÉRATURES)
    "tempcircuit2": ["Température Circuit 2", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],

    # VANNES MÉLANGEUSES (POSITION %)
    "mixer2valveposition": ["Vanne 2 Ouverture", PERCENTAGE, "mdi:valve", None],
}

# --- CONFIGURATION DES THERMOSTATS (Climate) ---
# Format: "Nom": ["slug_temp_actuelle", "slug_consigne"]
CLIMATE_TYPES = {
    "Circuit 2": ["tempcircuit2", "circuit2_romtempset"],
}

# Configuration ECS (Eau Chaude Sanitaire)
# Format: [Slug_Actuelle, Slug_Consigne, Slug_Min, Slug_Max]
WATER_HEATER_CONFIG = {
    "name": "Eau Chaude Sanitaire",
    "current": "tempcwu",
    "target": "hdwtsetpoint",
    "min": "hdwminsettemp",
    "max": "hdwmaxsettemp",
}

# --- CONFIGURATION DES NOMBRES (Consignes simples) ---
# Format: "slug": ["Nom", min, max, step, Icone]
NUMBER_TYPES = {
    "hdwtsetpoint": ["Consigne ECS", 20, 70, 1, "mdi:water-thermometer"],
    "hdwminsettemp": ["ECS Min", 10, 50, 1, "mdi:thermometer-chevron-down"],
    "hdwmaxsettemp": ["ECS Max", 50, 80, 1, "mdi:thermometer-chevron-up"],
}
