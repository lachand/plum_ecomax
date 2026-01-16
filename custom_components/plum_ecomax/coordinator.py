"""Data Update Coordinator for Plum EcoMAX.

This module provides the central data management logic for the integration.
It handles:

* **Polling**: Periodic fetching of data from the device.
* **Caching**: Storing values to reduce bus load.
* **Validation**: Filtering out invalid values (outliers, error codes) before they reach Home Assistant.
* **Write-Through**: Updating the local cache immediately after a successful write operation (Optimistic UI).
"""
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
    """Centralized data management with Robust Data Validation.

    This class extends Home Assistant's DataUpdateCoordinator to implement
    specific strategies for the ecoNET protocol, including caching,
    write-through updates, and aggressive data sanitization to prevent
    outliers from polluting the state machine.
    """

    def __init__(self, hass, device):
        """Initializes the coordinator.

        Args:
            hass: The Home Assistant core instance.
            device: The low-level PlumDevice instance used for communication.
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
        """Main update loop with Validation and Fallback.

        This method is called by Home Assistant at the defined interval.
        It iterates over known parameters, checks the cache freshness,
        and fetches new data if necessary.

        Returns:
            dict: The dictionary of validated data key-value pairs.
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
                raw_val = await self.device.get_value(slug, retries=5)
                
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
        """Sanitizes the raw value based on JSON limits or Generic constraints.

        The validation process follows a strict hierarchy:
        1.  Protocol errors (None, 999) are rejected immediately.
        2.  Specific limits defined in `device_map.json` (min/max) take priority.
        3.  Generic limits defined in `VALIDATION_RANGES` are applied as a fallback.

        Args:
            slug: The parameter identifier.
            raw_val: The raw value received from the device.
            cached_val: The previous known good value (for delta checks).

        Returns:
            Tuple[bool, Any]: A tuple containing (IsValid, SafeValue).
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
        """Writes a value to the device and verifies it was correctly set.

        This implements a 'Write & Verify' pattern with retries. It attempts to write
        the value, then reads it back to ensure the device accepted the change.
        If the read value differs from the target, it retries up to 5 times.

        Args:
            slug: The parameter identifier.
            value: The value to write.

        Returns:
            bool: True if the write was successful and verified.
        """
        max_retries = 5
        
        for attempt in range(1, max_retries + 1):
            # 1. Perform the physical write
            write_success = await self.device.set_value(slug, value)
            
            if write_success:
                # 2. Verification step: Read back the value
                # We wait a tiny bit to let the device process the write
                await asyncio.sleep(0.5) 
                
                verified_value = await self.device.get_value(slug, retries=1)
                
                # Check if the read value matches our target
                # We handle loose equality (e.g. 20.0 vs 20)
                if verified_value is not None:
                    # Conversion to float for robust comparison if numbers
                    try:
                        matches = float(verified_value) == float(value)
                    except (ValueError, TypeError):
                        matches = verified_value == value

                    if matches:
                        # 3. Success: Update cache (Optimistic update)
                        async with self._cache_lock:
                            self._cache[slug] = value
                            self._timestamps[slug] = time.time()
                        
                        self.async_set_updated_data(self._cache)
                        _LOGGER.info(f"✅ Value {slug}={value} set and verified (Attempt {attempt}).")
                        return True
                    else:
                        _LOGGER.warning(
                            f"⚠️ Write verification failed for {slug}. "
                            f"Wrote: {value}, Read back: {verified_value}. Retrying ({attempt}/{max_retries})..."
                        )
                else:
                    _LOGGER.warning(f"⚠️ Verification read failed for {slug}. Retrying ({attempt}/{max_retries})...")
            else:
                _LOGGER.error(f"❌ Physical write failed for {slug}. Retrying ({attempt}/{max_retries})...")
            
            # Exponential backoff before next retry (1s, 2s, 4s...)
            await asyncio.sleep(1.0 * attempt)

        _LOGGER.error(f"❌ Failed to set {slug}={value} after {max_retries} attempts.")
        return False

    async def _detect_available_parameters(self):
        """Initial scan to filter out unsupported parameters.

        Iterates through all possible parameters defined in `const.py`,
        checks if they exist in the device map, and attempts a physical read.
        Only responding parameters are retained for future polling.
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

        # 4. Water heater
        for conf in WATER_HEATER_TYPES.values():
            targets.extend(conf)

        # 5. Schedules
        targets.extend(list(SCHEDULE_TYPES.keys()))
        
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