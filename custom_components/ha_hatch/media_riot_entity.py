import logging
from typing import Any
from collections.abc import Mapping

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerDeviceClass,
    MediaPlayerState,
    MediaType,
)
from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from hatch_rest_api import (
    REST_IOT_AUDIO_TRACKS,
    REST_BABY_AUDIO_TRACKS,
    RIoTAudioTrack,
    RestBabyAudioTrack,
    RestBaby,
)

from . import HatchDataUpdateCoordinator
from .hatch_entity import HatchEntity

_LOGGER = logging.getLogger(__name__)

STATE_PLAYING: MediaPlayerState = MediaPlayerState.PLAYING
STATE_IDLE: MediaPlayerState = MediaPlayerState.IDLE


class MediaRiotEntity(HatchEntity, MediaPlayerEntity):
    _attr_media_content_type = MediaType.MUSIC
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER

    def __init__(self, coordinator: HatchDataUpdateCoordinator, thing_name: str):
        super().__init__(
            coordinator=coordinator, thing_name=thing_name, entity_type="Media Player"
        )
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.SELECT_SOUND_MODE
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.SELECT_SOURCE
        )
        self._attr_extra_state_attributes = {}

    @property
    def sound_mode_list(self) -> list[str]:
        _LOGGER.warning(
            "CALLING sound_mode_list: coordinator=%s, has_attr=%s, count=%d",
            self.coordinator,
            hasattr(self.coordinator, "custom_sounds_by_name"),
            len(getattr(self.coordinator, "custom_sounds_by_name", {})),
        )
        audio_data = (
            REST_IOT_AUDIO_TRACKS[1:]
            if not isinstance(self.rest_device, RestBaby)
            else REST_BABY_AUDIO_TRACKS[1:]
        )
        custom_sounds = []
        if hasattr(self.coordinator, "custom_sounds_by_name") and self.coordinator.custom_sounds_by_name:
            custom_sounds = list(self.coordinator.custom_sounds_by_name.keys())
        elif self.rest_device and hasattr(self.rest_device, "sounds_by_name") and self.rest_device.sounds_by_name:
            custom_sounds = list(self.rest_device.sounds_by_name.keys())
        return sorted(set([x.name for x in audio_data] + custom_sounds))

    @property
    def state(self) -> MediaPlayerState | None:
        if self.rest_device and self.rest_device.is_playing:
            return STATE_PLAYING
        else:
            return STATE_IDLE

    @property
    def sound_mode(self) -> str | None:
        _LOGGER.debug("looking up sound mode")
        if not self.rest_device:
            return None
        if hasattr(self.rest_device, "audio_track") and self.rest_device.audio_track is not None:
            if hasattr(self.rest_device.audio_track, "name"):
                return self.rest_device.audio_track.name
            track_val = (
                self.rest_device.audio_track.value
                if hasattr(self.rest_device.audio_track, "value")
                else self.rest_device.audio_track
            )
            if hasattr(self.rest_device, "sounds_by_id") and track_val in self.rest_device.sounds_by_id:
                return self.rest_device.sounds_by_id[track_val].get("title")
            if hasattr(self.coordinator, "custom_sounds_by_id") and track_val in self.coordinator.custom_sounds_by_id:
                return self.coordinator.custom_sounds_by_id[track_val].get("title")
            return str(self.rest_device.audio_track)
        else:
            return None

    @property
    def volume_level(self) -> float | None:
        if not self.rest_device:
            return None
        return self.rest_device.volume / 100

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        if not self.rest_device:
            return {}
        attrs: dict[str, Any] = {}
        if hasattr(self.rest_device, "current"):
            attrs["current"] = getattr(self.rest_device, "current", None)
        if hasattr(self.rest_device, "current_step"):
            attrs["current_step"] = getattr(self.rest_device, "current_step", None)
        if hasattr(self.rest_device, "current_favorite"):
            attrs["current_favorite"] = getattr(self.rest_device, "current_favorite", None)
        return attrs

    def set_volume_level(self, volume: float) -> None:
        if not self.rest_device:
            return
        self.rest_device.set_volume(round(volume * 100))

    def media_play(self) -> None:
        if not self.rest_device:
            return
        if self.state == STATE_PLAYING:
            _LOGGER.debug("media player already playing")
            return
        new_sound_mode = self.sound_mode or self.sound_mode_list[0]
        _LOGGER.debug("selecting sound mode of %s", new_sound_mode)
        self.select_sound_mode(new_sound_mode)

    def select_sound_mode(self, sound_mode: str) -> None:
        _LOGGER.debug("Select sound mode: %s", sound_mode)
        if not self.rest_device:
            return

        track = self._find_track(sound_mode=sound_mode)
        if track is not None:
            _LOGGER.info("Setting stock audio track: %s (%s)", sound_mode, track)
            self.rest_device.set_audio_track(track)
            return

        sound = None
        if hasattr(self.rest_device, "sounds_by_name") and sound_mode in self.rest_device.sounds_by_name:
            sound = self.rest_device.sounds_by_name[sound_mode]
        elif hasattr(self.coordinator, "custom_sounds_by_name") and sound_mode in self.coordinator.custom_sounds_by_name:
            sound = self.coordinator.custom_sounds_by_name[sound_mode]

        if sound:
            url = sound.get("wavUrl") or sound.get("mp3Url") or sound.get("url")
            _LOGGER.info("Dispatching custom sound %s (id=%s, url=%s)", sound_mode, sound.get("id"), url)
            self.rest_device.set_sound_url(url)
        else:
            self.rest_device.set_audio_track(self.none_track)

    def media_stop(self) -> None:
        if not self.rest_device:
            return
        self.rest_device.turn_off()

    def select_source(self, source: str) -> None:
        if not self.rest_device:
            return
        self.rest_device.set_favorite(source)

    @property
    def source_list(self) -> list[str]:
        if not self.rest_device:
            return []
        return self.rest_device.favorite_names()

    @property
    def source(self) -> str | None:
        if not self.rest_device:
            return None
        if hasattr(self.rest_device, "favorite_names") and self.rest_device.is_playing:
            favs = self.rest_device.favorite_names()
            fav_id = getattr(self.rest_device, "current_favorite", 0)
            if isinstance(fav_id, int) and 0 <= fav_id < len(favs):
                return favs[fav_id]
        return None

    @property
    def none_track(self) -> RIoTAudioTrack | RestBabyAudioTrack:
        if isinstance(self.rest_device, RestBaby):
            return RestBabyAudioTrack.NONE
        else:
            return RIoTAudioTrack.NONE

    def _find_track(self, sound_mode: str) -> RIoTAudioTrack | RestBabyAudioTrack | None:
        if isinstance(self.rest_device, RestBaby):
            return next(
                (
                    track
                    for track in REST_BABY_AUDIO_TRACKS
                    if track.name == sound_mode
                ),
                None,
            )
        else:
            return next(
                (
                    track
                    for track in REST_IOT_AUDIO_TRACKS
                    if track.name == sound_mode
                ),
                None,
            )
