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
    "tempbuforup": ["Ballon Tampon (Haut)", UnitOfTemperature.CELSIUS, "mdi:tank", "temperature"],
    "tempbufordown": ["Ballon Tampon (Bas)", UnitOfTemperature.CELSIUS, "mdi:tank", "temperature"],
    "buforsetpoint": ["Consigne Ballon Tampon", UnitOfTemperature.CELSIUS, "mdi:target", "temperature"],

    # CIRCUITS MÉLANGEURS (TEMPÉRATURES)
    "tempcircuit2": ["Température Circuit 2", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit3": ["Température Circuit 3", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit4": ["Température Circuit 4", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit5": ["Température Circuit 5", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit6": ["Température Circuit 6", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],
    "tempcircuit7": ["Température Circuit 7", UnitOfTemperature.CELSIUS, "mdi:radiator", "temperature"],

    # VANNES MÉLANGEUSES (POSITION %)
    "mixer2valveposition": ["Vanne 2 Ouverture", PERCENTAGE, "mdi:valve", None],
    "mixer3valveposition": ["Vanne 3 Ouverture", PERCENTAGE, "mdi:valve", None],
    "mixer4valveposition": ["Vanne 4 Ouverture", PERCENTAGE, "mdi:valve", None],
    "mixer5valveposition": ["Vanne 5 Ouverture", PERCENTAGE, "mdi:valve", None],
    "mixer6valveposition": ["Vanne 6 Ouverture", PERCENTAGE, "mdi:valve", None],
    "mixer7valveposition": ["Vanne 7 Ouverture", PERCENTAGE, "mdi:valve", None],
}

# --- CONFIGURATION DES THERMOSTATS (Climate) ---
# Format: "Nom": ["slug_temp_actuelle", "slug_consigne"]
CLIMATE_TYPES = {
    "Circuit 2": ["tempcircuit2", "circuit2_romtempset"],
    "Circuit 3": ["tempcircuit3", "circuit3_romtempset"],
    "Circuit 4": ["tempcircuit4", "circuit4_romtempset"],
    "Circuit 5": ["tempcircuit5", "circuit5_romtempset"],
    "Circuit 6": ["tempcircuit6", "circuit6_romtempset"],
    "Circuit 7": ["tempcircuit7", "circuit7_romtempset"],
}

# --- CONFIGURATION DES NOMBRES (Consignes simples) ---
# Format: "slug": ["Nom", min, max, step, Icone]
NUMBER_TYPES = {
    "hdwtsetpoint": ["Consigne ECS", 20, 70, 1, "mdi:water-thermometer"],
    "hdwminsettemp": ["ECS Min", 10, 50, 1, "mdi:thermometer-chevron-down"],
    "hdwmaxsettemp": ["ECS Max", 50, 80, 1, "mdi:thermometer-chevron-up"],
}
