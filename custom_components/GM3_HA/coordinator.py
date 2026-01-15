import logging
import asyncio
import time
from datetime import timedelta
from typing import Any, Dict, Optional, Tuple
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN, UPDATE_INTERVAL, SENSOR_TYPES, CLIMATE_TYPES, NUMBER_TYPES, SCHEDULE_TYPES, WATER_HEATER_TYPES

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
        @brief Sanitizes the raw value based on JSON limits or Generic constraints.
        @details
        1. Checks for protocol errors (None, 999).
        2. Checks specific limits defined in device_map.json (Priority).
        3. Checks generic limits defined in VALIDATION_RANGES (Fallback).
        
        @return Tuple[bool, Any] (IsValid, SafeValue).
        """
        # A. Basic protocol checks
        if raw_val is None:
            return False, None
            
        if isinstance(raw_val, (int, float)):
            if raw_val == 999.0 or raw_val == 999:
                _LOGGER.debug(f"⚠️ Rejection: {slug} returned sensor error code {raw_val}")
                return False, None
                
        param_def = self.device.params_map.get(slug, {})
        json_min = param_def.get("min")
        json_max = param_def.get("max")
        json_max_delta = param_def.get("max_delta")

        if (json_min is not None or json_max is not None) and isinstance(raw_val, (int, float)):
            is_valid = True
            
            if json_min is not None and raw_val < json_min:
                is_valid = False
            if json_max is not None and raw_val > json_max:
                is_valid = False
            if json_max_delta is not None and cached_val is not None and abs(cached_val - raw_val) > json_max_delta :
                is_valide = False
                
            if not is_valid:
                _LOGGER.warning(
                    f"🛑 Specific Outlier detected for {slug}: {raw_val}. "
                    f"JSON Limits [{json_min}, {json_max}]. Holding last state ({cached_val})."
                )
                return False, None
        
            return True, raw_val

        if isinstance(raw_val, (int, float)):
            for keyword, (min_v, max_v) in VALIDATION_RANGES.items():
                if keyword in slug:
                    if not (min_v <= raw_val <= max_v):
                        _LOGGER.warning(
                            f"🛑 Generic Outlier detected for {slug}: {raw_val}. "
                            f"Global Limits [{min_v}, {max_v}]. Holding last state ({cached_val})."
                        )
                        return False, None
                    break # Stop at first matching rule
                    
        return True, raw_val
        

    async def async_set_value(self, slug: str, value: Any) -> bool:
        """
        @brief Writes a value to the device and updates the local cache immediately.
        @details This implements the 'Write-Through' pattern. It allows the UI to 
        update instantly without waiting for the next poll interval (Optimistic UI).
        
        @param slug The parameter identifier.
        @param value The value to write.
        @return bool True if the write was successful.
        """
        # 1. Perform the physical write
        success = await self.device.set_value(slug, value)
        
        if success:
            # 2. Update cache immediately (Optimistic update)
            async with self._cache_lock:
                self._cache[slug] = value
                self._timestamps[slug] = time.time()
            
            # 3. Notify Home Assistant that data has changed
            # This forces entities to refresh their state from our cache
            self.async_set_updated_data(self._cache)
            _LOGGER.info(f"✅ Value {slug}={value} set and cache updated.")
        else:
            _LOGGER.error(f"❌ Failed to set {slug}={value}")
            
        return success

    async def _detect_available_parameters(self):
        """
        @brief Initial scan to filter out unsupported parameters.
        @details Checks existence in JSON map and attempts a physical read.
        """
        _LOGGER.info("🔍 Initial scan of available parameters...")
        
        targets = []
        
        # 1. Sensors
        targets.extend(list(SENSOR_TYPES.keys()))
        
        # 2. Climates (Temperature + Target)
        for conf in CLIMATE_TYPES.values():
            targets.extend(conf) 
            
        # 3. Numbers
        targets.extend(list(NUMBER_TYPES.keys()))

        for conf in WATER_HEATER_TYPES.values():
            targets.extend(conf)
        
        valid_slugs = []
        for slug in targets:
            if slug not in self.device.params_map:
                continue
                
            val = await self.device.get_value(slug, retries=5)
            
            # Filter invalid values (999.0 often indicates a disconnected probe)
            if val is not None and val != 999.0:
                 valid_slugs.append(slug)
                 _LOGGER.debug(f"Detected parameter: {slug}")
        
        self.available_slugs = list(set(valid_slugs))
        _LOGGER.info(f"✅ {len(self.available_slugs)} active parameters retained.")