"""Unit tests for config_flow.py: connection validation (Family: config
flow validation, mocked PlumDevice).

`_validate_connection()` and `_build_data_schema()` are free functions
(not ConfigFlow methods), so they can be exercised directly without a real
FlowManager/hass -- only `hass.config.path()` is used, stubbed with a
plain MagicMock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

from custom_components.plum_ecomax import config_flow as config_flow_module
from custom_components.plum_ecomax.config_flow import (
    PlumConfigFlow,
    _build_data_schema,
    _validate_connection,
)
from custom_components.plum_ecomax.const import (
    CONF_ACTIVE_CIRCUITS,
    CONF_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)
from custom_components.plum_ecomax.device import normalise_serial


def _fake_hass():
    hass = MagicMock()
    hass.config.path = MagicMock(side_effect=lambda p: f"/config/{p}")
    return hass


def _patch_device(monkeypatch, *, load_map_error=None, get_value_return=42, get_value_error=None):
    fake_device = MagicMock()
    fake_device.load_map = MagicMock(side_effect=load_map_error)
    fake_device.async_close = AsyncMock()
    if get_value_error is not None:
        fake_device.get_value = AsyncMock(side_effect=get_value_error)
    else:
        fake_device.get_value = AsyncMock(return_value=get_value_return)
    monkeypatch.setattr(config_flow_module, "PlumDevice", lambda *a, **k: fake_device)
    return fake_device


class TestValidateConnection:
    @pytest.mark.asyncio
    async def test_successful_probe_returns_no_error(self, monkeypatch):
        _patch_device(monkeypatch, get_value_return=42)

        error = await _validate_connection(
            _fake_hass(),
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PORT: 8899,
                CONF_PASSWORD: "0000",
            },
        )

        assert error is None

    @pytest.mark.asyncio
    async def test_map_load_failure_returns_cannot_load_map(self, monkeypatch):
        _patch_device(monkeypatch, load_map_error=OSError("no such file"))

        error = await _validate_connection(
            _fake_hass(),
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PASSWORD: "0000",
            },
        )

        assert error == "cannot_load_map"

    @pytest.mark.asyncio
    async def test_no_value_returned_means_cannot_connect(self, monkeypatch):
        _patch_device(monkeypatch, get_value_return=None)

        error = await _validate_connection(
            _fake_hass(),
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PASSWORD: "0000",
            },
        )

        assert error == "cannot_connect"

    @pytest.mark.asyncio
    async def test_exception_during_read_means_cannot_connect(self, monkeypatch):
        _patch_device(monkeypatch, get_value_error=OSError("connection refused"))

        error = await _validate_connection(
            _fake_hass(),
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PASSWORD: "0000",
            },
        )

        assert error == "cannot_connect"


class TestBuildDataSchema:
    def test_schema_validates_full_input(self):
        schema = _build_data_schema({})
        result = schema(
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PORT: 8899,
                CONF_USERNAME: "admin",
                CONF_PASSWORD: "0000",
                CONF_ACTIVE_CIRCUITS: ["2"],
            }
        )
        assert result[CONF_IP_ADDRESS] == "192.168.1.38"
        assert result[CONF_ACTIVE_CIRCUITS] == ["2"]

    def test_password_field_is_masked(self):
        from homeassistant.helpers.selector import TextSelector, TextSelectorType

        schema = _build_data_schema({})
        password_validator = next(
            v for k, v in schema.schema.items() if getattr(k, "schema", k) == CONF_PASSWORD
        )
        assert isinstance(password_validator, TextSelector)
        assert password_validator.config["type"] == TextSelectorType.PASSWORD

    def test_password_defaults_when_omitted(self):
        # vol.Required + a default means the key is auto-filled, not
        # enforced as "must be present" -- omitting it must not raise.
        schema = _build_data_schema({})
        result = schema({CONF_IP_ADDRESS: "192.168.1.38", CONF_ACTIVE_CIRCUITS: ["2"]})
        assert result[CONF_PASSWORD] == "0000"

    def test_ip_address_default_reflects_prior_input(self):
        schema = _build_data_schema({CONF_IP_ADDRESS: "10.0.0.5"})
        # Applying the schema to an empty dict pulls in the field defaults,
        # which is how the options flow pre-fills the form from entry.data.
        result = schema({CONF_PASSWORD: "0000", CONF_ACTIVE_CIRCUITS: ["2"]})
        assert result[CONF_IP_ADDRESS] == "10.0.0.5"

    def test_update_interval_defaults_when_omitted(self):
        schema = _build_data_schema({})
        result = schema(
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PASSWORD: "0000",
                CONF_ACTIVE_CIRCUITS: ["2"],
            }
        )
        assert result[CONF_UPDATE_INTERVAL] == UPDATE_INTERVAL

    def test_update_interval_default_reflects_prior_input(self):
        schema = _build_data_schema({CONF_UPDATE_INTERVAL: 60})
        result = schema(
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PASSWORD: "0000",
                CONF_ACTIVE_CIRCUITS: ["2"],
            }
        )
        assert result[CONF_UPDATE_INTERVAL] == 60

    def test_update_interval_accepts_value_within_bounds(self):
        schema = _build_data_schema({})
        result = schema(
            {
                CONF_IP_ADDRESS: "192.168.1.38",
                CONF_PASSWORD: "0000",
                CONF_ACTIVE_CIRCUITS: ["2"],
                CONF_UPDATE_INTERVAL: 90,
            }
        )
        assert result[CONF_UPDATE_INTERVAL] == 90

    def test_update_interval_rejects_value_below_minimum(self):
        schema = _build_data_schema({})
        with pytest.raises(vol.Invalid):
            schema(
                {
                    CONF_IP_ADDRESS: "192.168.1.38",
                    CONF_PASSWORD: "0000",
                    CONF_ACTIVE_CIRCUITS: ["2"],
                    CONF_UPDATE_INTERVAL: MIN_UPDATE_INTERVAL - 1,
                }
            )

    def test_update_interval_rejects_value_above_maximum(self):
        schema = _build_data_schema({})
        with pytest.raises(vol.Invalid):
            schema(
                {
                    CONF_IP_ADDRESS: "192.168.1.38",
                    CONF_PASSWORD: "0000",
                    CONF_ACTIVE_CIRCUITS: ["2"],
                    CONF_UPDATE_INTERVAL: MAX_UPDATE_INTERVAL + 1,
                }
            )


class TestSerialProbe:
    """The boiler's serial number ("uid") is read after the probe and becomes
    the entry's unique_id; an unreadable one must not fail the connection test.
    """

    @staticmethod
    def _device_with(monkeypatch, answers):
        dev = MagicMock()
        dev.load_map = MagicMock()
        dev.async_close = AsyncMock()

        async def get_value(slug, retries=3):
            ans = answers[slug]
            if isinstance(ans, Exception):
                raise ans
            return ans

        dev.get_value = get_value
        monkeypatch.setattr(config_flow_module, "PlumDevice", lambda *a, **k: dev)

    @pytest.mark.asyncio
    async def test_serial_is_returned_with_success(self, monkeypatch):
        self._device_with(monkeypatch, {"hdwstate": 1, "uid": "ABC123"})
        error, serial = await config_flow_module._probe_boiler(
            _fake_hass(), {CONF_IP_ADDRESS: "1.2.3.4", CONF_PASSWORD: "0000"}
        )
        assert (error, serial) == (None, "ABC123")

    @pytest.mark.asyncio
    async def test_unreadable_serial_does_not_fail_the_probe(self, monkeypatch):
        self._device_with(monkeypatch, {"hdwstate": 1, "uid": OSError("timeout")})
        error, serial = await config_flow_module._probe_boiler(
            _fake_hass(), {CONF_IP_ADDRESS: "1.2.3.4", CONF_PASSWORD: "0000"}
        )
        assert (error, serial) == (None, None)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  XY9 ", "XY9"),
            (b"XY9\x00\x00", "XY9"),
            ("", None),
            (None, None),
        ],
    )
    def test_normalise_serial(self, raw, expected):
        assert normalise_serial(raw) == expected


def _flow(entries=()):
    flow = PlumConfigFlow()
    flow.hass = _fake_hass()
    flow._async_current_entries = MagicMock(return_value=list(entries))
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    return flow


_USER_INPUT = {
    CONF_IP_ADDRESS: "1.2.3.4",
    CONF_PORT: 8899,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "0000",
    CONF_ACTIVE_CIRCUITS: ["2"],
}


class TestUserStepUniqueId:
    @pytest.mark.asyncio
    async def test_unique_id_is_the_serial_when_readable(self, monkeypatch):
        monkeypatch.setattr(
            config_flow_module, "_probe_boiler", AsyncMock(return_value=(None, "ABC123"))
        )
        flow = _flow()
        result = await flow.async_step_user(dict(_USER_INPUT))
        flow.async_set_unique_id.assert_awaited_once_with("ABC123")
        assert result["type"].value == "create_entry"

    @pytest.mark.asyncio
    async def test_unique_id_falls_back_to_ip(self, monkeypatch):
        monkeypatch.setattr(
            config_flow_module, "_probe_boiler", AsyncMock(return_value=(None, None))
        )
        flow = _flow()
        await flow.async_step_user(dict(_USER_INPUT))
        flow.async_set_unique_id.assert_awaited_once_with("1.2.3.4")

    @pytest.mark.asyncio
    async def test_ip_already_used_by_a_legacy_entry_aborts(self, monkeypatch):
        monkeypatch.setattr(
            config_flow_module, "_probe_boiler", AsyncMock(return_value=(None, "ABC123"))
        )
        legacy = MagicMock()
        legacy.data = {CONF_IP_ADDRESS: "1.2.3.4"}
        flow = _flow([legacy])
        result = await flow.async_step_user(dict(_USER_INPUT))
        assert result["type"].value == "abort"
        assert result["reason"] == "already_configured"


class TestReconfigure:
    @staticmethod
    def _reconfig_flow(entry, monkeypatch, probe):
        flow = _flow()
        flow._get_reconfigure_entry = MagicMock(return_value=entry)
        flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
        monkeypatch.setattr(config_flow_module, "_probe_boiler", AsyncMock(return_value=probe))
        return flow

    @pytest.mark.asyncio
    async def test_other_boiler_aborts_with_unique_id_mismatch(self, monkeypatch):
        entry = MagicMock(unique_id="ABC123", data={CONF_IP_ADDRESS: "1.2.3.4"})
        flow = self._reconfig_flow(entry, monkeypatch, (None, "OTHER"))
        result = await flow.async_step_reconfigure(dict(_USER_INPUT))
        assert result["reason"] == "unique_id_mismatch"
        flow.async_update_reload_and_abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_boiler_new_ip_updates_and_reloads(self, monkeypatch):
        entry = MagicMock(unique_id="ABC123", data={CONF_IP_ADDRESS: "1.2.3.4"})
        flow = self._reconfig_flow(entry, monkeypatch, (None, "ABC123"))
        new = {**_USER_INPUT, CONF_IP_ADDRESS: "1.2.3.9"}
        await flow.async_step_reconfigure(new)
        kwargs = flow.async_update_reload_and_abort.call_args.kwargs
        assert kwargs["unique_id"] == "ABC123"
        assert kwargs["data"][CONF_IP_ADDRESS] == "1.2.3.9"

    @pytest.mark.asyncio
    async def test_legacy_ip_keyed_entry_adopts_the_serial(self, monkeypatch):
        entry = MagicMock(unique_id="1.2.3.4", data={CONF_IP_ADDRESS: "1.2.3.4"})
        flow = self._reconfig_flow(entry, monkeypatch, (None, "ABC123"))
        await flow.async_step_reconfigure(dict(_USER_INPUT))
        assert flow.async_update_reload_and_abort.call_args.kwargs["unique_id"] == "ABC123"

    @pytest.mark.asyncio
    async def test_connection_error_reshows_the_form(self, monkeypatch):
        entry = MagicMock(unique_id="ABC123", data=dict(_USER_INPUT))
        flow = self._reconfig_flow(entry, monkeypatch, ("cannot_connect", None))
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        await flow.async_step_reconfigure(dict(_USER_INPUT))
        assert flow.async_show_form.call_args.kwargs["errors"] == {"base": "cannot_connect"}
