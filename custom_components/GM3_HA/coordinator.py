import logging
import asyncio
import time
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN, UPDATE_INTERVAL, SENSOR_TYPES, CLIMATE_TYPES, NUMBER_TYPES

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTL = 25

## Definitions of physical limits for validation
# Format: "keyword_in_slug": (min_val, max_val)
VALIDATION_RANGES = {
    "temp": (-20, 100.0),     # Water temperature (avoid 0.0 which is often a sensor error)
    "power": (0, 100),       # Percentage
    "fan": (0, 100),         # Percentage
    "valveposition": (0, 100),         # Percentage
    "pressure": (0.0, 4.0),  # Bar
    "lambda": (0.0, 25.0),   # Oxygen level
}

class PlumDataUpdateCoordinator(DataUpdateCoordinator):
    """
    @class PlumDataUpdateCoordinator
    @brief Centralized data management with Robust Data Validation.
    @details Implements caching, write-through strategies, and data sanitization
    to prevent outliers from polluting the state machine.
    """

    def __init__(self, hass, device):
        """
        @brief Constructor.
        @param hass Home Assistant core instance.
        @param device The low-level PlumDevice instance.
        """
        self.device = device
        self.available_slugs = []
        
        # Cache System
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._cache_lock = asyncio.Lock()
        self.ttl = DEFAULT_TTL

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self):
        """
        @brief Main update loop with Validation and Fallback.
        """
        data = {}
        now = time.time()
        
        if not self.available_slugs:
            await self._detect_available_parameters()

        for slug in self.available_slugs:
            async with self._cache_lock:
                last_update = self._timestamps.get(slug, 0)
                is_fresh = (now - last_update) < self.ttl
                cached_val = self._cache.get(slug)

            # 1. Cache Hit
            if is_fresh and cached_val is not None:
                data[slug] = cached_val
                continue

            # 2. Fetch & Validate
            try:
                raw_val = await self.device.get_value(slug, retries=2)
                
                # --- VALIDATION STEP ---
                is_valid, final_val = self._validate_value(slug, raw_val, cached_val)

                if is_valid:
                    # Valid new data: Update cache
                    async with self._cache_lock:
                        self._cache[slug] = final_val
                        self._timestamps[slug] = time.time()
                    data[slug] = final_val
                else:
                    # Invalid data: Use fallback (Hold Last State)
                    if cached_val is not None:
                        data[slug] = cached_val
                        # We do NOT update the timestamp to retry fetch sooner if needed,
                        # OR we update it if we want to suppress the error for a cycle.
                        # Here, we keep old timestamp to retry next cycle.
                    
            except Exception as e:
                _LOGGER.warning(f"Error reading {slug}: {e}")
                if slug in self._cache:
                    data[slug] = self._cache[slug]
        
        return data

    def _validate_value(self, slug: str, raw_val: Any, cached_val: Any) -> Tuple[bool, Any]:
        """
        @brief Sanitizes the raw value based on physical constraints.
        @details Checks against known error codes (999, NaN) and physical ranges.
        
        @param slug The parameter identifier.
        @param raw_val The new value received from the device.
        @param cached_val The previous known good value (for logging context).
        @return Tuple[bool, Any] (IsValid, SafeValue).
        """
        # A. Basic protocol checks
        if raw_val is None:
            return False, None
            
        if isinstance(raw_val, (int, float)):
            # 999 is the standard error code for disconnected sensors in Plum ecoNET
            if raw_val == 999.0 or raw_val == 999:
                _LOGGER.debug(f"⚠️ Rejection: {slug} returned sensor error code {raw_val}")
                return False, None

        # B. Range checks (Heuristic)
        # We look for keywords in the slug to determine the rule
        for keyword, (min_v, max_v) in VALIDATION_RANGES.items():
            if keyword in slug and isinstance(raw_val, (int, float)):
                if not (min_v <= raw_val <= max_v):
                    _LOGGER.warning(
                        f"🛑 Outlier detected for {slug}: {raw_val} is outside [{min_v}, {max_v}]. "
                        f"Holding last state ({cached_val})."
                    )
                    return False, None
                break # Stop at first matching rule

        # C. Success
        return True, raw_val

    # ... (rest of methods: async_set_value, _detect_available_parameters keep unchanged)
    # Copier ici les méthodes async_set_value et _detect_available_parameters de la réponse précédente
