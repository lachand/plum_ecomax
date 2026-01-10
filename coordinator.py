import logging
import asyncio
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN, UPDATE_INTERVAL, SENSOR_TYPES, CLIMATE_TYPES, NUMBER_TYPES

_LOGGER = logging.getLogger(__name__)

class PlumDataUpdateCoordinator(DataUpdateCoordinator):
    """Gère la récupération des données centralisée."""

    def __init__(self, hass, device):
        self.device = device
        self.available_slugs = [] # Liste des paramètres valides détectés
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self):
        """C'est ici que la magie opère toutes les 30 secondes."""
        data = {}
        
        # 1. Si c'est la première fois, on détermine quels paramètres existent
        if not self.available_slugs:
            await self._detect_available_parameters()

        # 2. On boucle sur tous les paramètres connus
        # On utilise votre méthode get_value optimisée avec retries
        for slug in self.available_slugs:
            try:
                val = await self.device.get_value(slug, retries=2)
                if val is not None:
                    data[slug] = val
            except Exception as e:
                _LOGGER.warning(f"Erreur lecture {slug}: {e}")
        
        return data

    async def _detect_available_parameters(self):
        """Scan initial pour ne pas interroger des circuits fantômes."""
        _LOGGER.info("🔍 Scan initial des paramètres disponibles...")
        
        # On construit la liste de TOUS les slugs dont on a besoin
        targets = []
        
        # 1. Sensors
        targets.extend(list(SENSOR_TYPES.keys()))
        
        # 2. Climates (Température + Consigne)
        for conf in CLIMATE_TYPES.values():
            targets.extend(conf) # Ajoute [temp, consigne]
            
        # 3. Numbers
        targets.extend(list(NUMBER_TYPES.keys()))
        
        # On vérifie un par un s'ils répondent (méthode Scan)
        valid_slugs = []
        for slug in targets:
            # Vérif si présent dans le JSON
            if slug not in self.device.params_map:
                continue
                
            # Test de lecture
            val = await self.device.get_value(slug, retries=2)
            
            # Si valide (pas None, pas 999, pas 0.0 pour les temps)
            if val is not None and val != 999.0:
                 valid_slugs.append(slug)
                 _LOGGER.debug(f"Paramètre détecté : {slug}")
        
        self.available_slugs = list(set(valid_slugs)) # Dédoublonnage
        _LOGGER.info(f"✅ {len(self.available_slugs)} paramètres actifs retenus.")
