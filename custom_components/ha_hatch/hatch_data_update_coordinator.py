import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta, UTC, datetime
from inspect import isawaitable
import json
from logging import getLogger, Logger
import os
import traceback
from typing import Final

from awscrt.mqtt import Connection
from hatch_rest_api import (
    RestDevice,
    get_rest_devices,
)
from hatch_rest_api.errors import AuthError, RateError
from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER: Final[Logger] = getLogger(__name__)

ALARM_REFRESH_INTERVAL: Final = timedelta(minutes=10)
DEFAULT_RETRY_INTERVAL: Final = timedelta(minutes=1)
RATE_LIMIT_RETRY_INTERVAL: Final = timedelta(minutes=15)
MAX_RATE_LIMIT_RETRY_INTERVAL: Final = timedelta(hours=6)
AWSCRT_MISMATCH_RETRY_INTERVAL: Final = timedelta(minutes=15)
AWSCRT_MISMATCH_TRACE_FILE: Final = "awscrt/mqtt.py"
AWSCRT_MISMATCH_TRACE_NAME: Final = "_on_connection_interrupted"

AlarmRefreshCallback = Callable[[], None] | Callable[[], Awaitable[None]]


class HatchDataUpdateCoordinator(DataUpdateCoordinator[list[dict]]):
    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        config_entry: ConfigEntry,
    ) -> None:
        self.email: str = email
        self.password: str = password
        self.mqtt_connection: Connection | None = None
        self.rest_devices: list[RestDevice] = []
        self.expiration_time: int = 0
        self.custom_sounds: list[dict] = []
        self.custom_sounds_by_name: dict[str, dict] = {}
        self.custom_sounds_by_id: dict[int, dict] = {}
        self._alarm_refresh_callbacks: set[AlarmRefreshCallback] = set()
        self._alarm_refresh_unsub: Callable[[], None] | None = None
        self._alarm_refresh_lock = asyncio.Lock()
        self._retry_backoff_until: datetime | None = None
        self._retry_backoff_reasons: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}-{self.email}",
            always_update=False,
        )

    def _disconnect_mqtt(self) -> None:
        if self.mqtt_connection is None:
            return

        try:
            self.mqtt_connection.disconnect()
        except Exception as error:
            _LOGGER.debug(
                "MQTT disconnect failed: %s",
                error,
            )
        finally:
            self.mqtt_connection = None

    def _rest_device_unsub(self) -> None:
        for rest_device in self.rest_devices:
            rest_device.remove_callback(self.async_update_listeners)

    def rest_device_by_thing_name(self, thing_name: str) -> RestDevice | None:
        return next(
            (device for device in self.rest_devices if device.thing_name == thing_name),
            None,
        )

    def async_start_alarm_refresh(self) -> None:
        if self._alarm_refresh_unsub is not None:
            return

        async def _refresh_alarms(_now: datetime) -> None:
            await self._async_notify_alarm_refresh_callbacks()

        self._alarm_refresh_unsub = async_track_time_interval(
            self.hass,
            _refresh_alarms,
            ALARM_REFRESH_INTERVAL,
        )

    def async_add_alarm_refresh_callback(
        self, callback: AlarmRefreshCallback
    ) -> Callable[[], None]:
        self._alarm_refresh_callbacks.add(callback)

        def _remove() -> None:
            self._alarm_refresh_callbacks.discard(callback)

        return _remove

    async_register_alarm_refresh_callback = async_add_alarm_refresh_callback

    async def _async_notify_alarm_refresh_callbacks(self) -> None:
        if not self._alarm_refresh_callbacks:
            return

        async with self._alarm_refresh_lock:
            for callback in list(self._alarm_refresh_callbacks):
                try:
                    result = callback()
                    if isawaitable(result):
                        await result
                except Exception as error:
                    _LOGGER.error("Alarm refresh callback failed", exc_info=error)

    def _clear_retry_backoff(self) -> None:
        self._retry_backoff_reasons.clear()
        self._retry_backoff_until = None

    def _set_retry_backoff(self, reason: str, retry_interval: timedelta) -> None:
        self._retry_backoff_reasons.add(reason)
        self._retry_backoff_until = datetime.now(UTC) + retry_interval

    def _raise_if_retry_backoff_active(self) -> None:
        retry_at = self._retry_backoff_until
        if retry_at is None:
            return

        remaining = retry_at - datetime.now(UTC)
        if remaining <= timedelta(0):
            return

        self.update_interval = remaining
        raise UpdateFailed(
            f"Retry backoff active until {retry_at.isoformat()} for {self.email}"
        )

    def _load_custom_sounds(self) -> None:
        """Load optional custom sound mapping from custom_sounds.json if present."""
        config_paths = [
            self.hass.config.path("custom_components", "ha_hatch", "custom_sounds.json"),
            self.hass.config.path("hatch_custom_sounds.json"),
            os.path.join(os.path.dirname(__file__), "custom_sounds.json"),
        ]

        custom_sounds = None
        loaded_path = None
        for path in config_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        custom_sounds = json.load(f)
                    loaded_path = path
                    break
                except Exception as err:
                    _LOGGER.error("Failed to load custom sounds from %s: %s", path, err)

        if not custom_sounds or not isinstance(custom_sounds, list):
            return

        _LOGGER.info("Loaded %d custom sounds from %s", len(custom_sounds), loaded_path)

        self.custom_sounds = custom_sounds
        self.custom_sounds_by_name = {}
        self.custom_sounds_by_id = {}

        for item in custom_sounds:
            if not isinstance(item, dict):
                continue
            sound_id = item.get("id")
            title = item.get("title")
            url = (
                item.get("url")
                or item.get("wavUrl")
                or item.get("mp3Url")
                or (f"https://assets.ctfassets.net/custom/{item.get('filename')}" if item.get("filename") else None)
            )
            if not sound_id or not title or not url:
                continue

            sound_dict = {
                "id": sound_id,
                "title": title,
                "wavUrl": url,
                "mp3Url": url,
            }
            self.custom_sounds_by_name[title] = sound_dict
            self.custom_sounds_by_id[sound_id] = sound_dict

        for rest_device in self.rest_devices:
            if not hasattr(rest_device, "sounds") or not isinstance(rest_device.sounds, list):
                rest_device.sounds = []
            if not hasattr(rest_device, "sounds_by_name") or not isinstance(rest_device.sounds_by_name, dict):
                rest_device.sounds_by_name = {}
            if not hasattr(rest_device, "sounds_by_id") or not isinstance(rest_device.sounds_by_id, dict):
                rest_device.sounds_by_id = {}

            existing_ids = {
                s.get("id") for s in rest_device.sounds if isinstance(s, dict)
            }
            for title, sound_dict in self.custom_sounds_by_name.items():
                sound_id = sound_dict["id"]
                if sound_id not in existing_ids:
                    rest_device.sounds.append(sound_dict)
                    existing_ids.add(sound_id)

                rest_device.sounds_by_name[title] = sound_dict
                rest_device.sounds_by_id[sound_id] = sound_dict

    def _is_awscrt_connect_signature_mismatch(self, error: Exception) -> bool:
        if not isinstance(error, TypeError) or "argument" not in str(error):
            return False

        return any(
            frame.filename.endswith(AWSCRT_MISMATCH_TRACE_FILE)
            and frame.name == AWSCRT_MISMATCH_TRACE_NAME
            for frame in traceback.extract_tb(error.__traceback__)
        )

    async def _async_update_data(self) -> list[dict]:
        self._raise_if_retry_backoff_active()
        try:
            _LOGGER.debug(f"_async_update_data: {self.email}")
            self._disconnect_mqtt()

            def disconnect():
                _LOGGER.debug(f"disconnected: {self.email}")

            def resumed(return_code, session_present):
                _LOGGER.debug(
                    f"resumed: {self.email}, return_code: {return_code}, session_present: {session_present}"
                )

            client_session = async_get_clientsession(self.hass)
            (
                _,
                self.mqtt_connection,
                self.rest_devices,
                self.expiration_time,
            ) = await get_rest_devices(
                email=self.email,
                password=self.password,
                client_session=client_session,
                on_connection_interrupted=disconnect,
                on_connection_resumed=resumed,
            )
            _LOGGER.debug(
                f"credentials expire at: {datetime.fromtimestamp(self.expiration_time, UTC)}"
            )
            self.update_interval = datetime.fromtimestamp(self.expiration_time - 60, UTC) - datetime.now(UTC)
            self._clear_retry_backoff()
            self._load_custom_sounds()
            for rest_device in self.rest_devices:
                rest_device.register_callback(self.async_update_listeners)
            # Re-login replaces every RestDevice instance, so alarm-derived entities
            # must reconcile against the new objects to keep references current.
            await self._async_notify_alarm_refresh_callbacks()
            return [rest_device.__repr__() for rest_device in self.rest_devices]
        except AuthError as error:
            self._clear_retry_backoff()
            raise ConfigEntryAuthFailed(
                "Hatch credentials rejected during setup"
            ) from error
        except RateError as error:
            self._set_retry_backoff("rate_limit", RATE_LIMIT_RETRY_INTERVAL)
            raise UpdateFailed(
                f"Hatch API rate limit active for {self.email}; retrying in {RATE_LIMIT_RETRY_INTERVAL}"
            ) from error
        except Exception as error:
            if self._is_awscrt_connect_signature_mismatch(error):
                self._set_retry_backoff("awscrt_mismatch", AWSCRT_MISMATCH_RETRY_INTERVAL)
                _LOGGER.error(
                    "AWS CRT connection signature mismatch for %s; backing off for %s",
                    self.email,
                    AWSCRT_MISMATCH_RETRY_INTERVAL,
                    exc_info=error,
                )
                raise UpdateFailed(
                    f"AWS CRT connection signature mismatch for {self.email}; backing off for {AWSCRT_MISMATCH_RETRY_INTERVAL}"
                ) from error

            self._clear_retry_backoff()
            raise UpdateFailed(
                f"Unknown error connecting to Hatch: {error}"
            ) from error

    async def async_shutdown(self) -> None:
        if self._alarm_refresh_unsub is not None:
            self._alarm_refresh_unsub()
            self._alarm_refresh_unsub = None
        self._alarm_refresh_callbacks.clear()
        self._disconnect_mqtt()
        self._rest_device_unsub()
        await super().async_shutdown()
