from __future__ import annotations
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
    REST_PLUS_AUDIO_TRACKS,
    RestPlusAudioTrack,
    RestMiniAudioTrack,
    RestMini,
    REST_MINI_AUDIO_TRACKS,
)

from . import HatchDataUpdateCoordinator
from .hatch_entity import HatchEntity

_LOGGER = logging.getLogger(__name__)

STATE_PLAYING: MediaPlayerState = MediaPlayerState.PLAYING
STATE_IDLE: MediaPlayerState = MediaPlayerState.IDLE


class MediaRestEntity(HatchEntity, MediaPlayerEntity):
    _attr_media_content_type = MediaType.MUSIC
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER

    def __init__(self, coordinator: HatchDataUpdateCoordinator, thing_name: str, config_turn_on_media: bool):
        super().__init__(coordinator=coordinator, thing_name=thing_name, entity_type="Media Player")
        self.config_turn_on_media: bool = config_turn_on_media
        if isinstance(self.rest_device, RestMini):
            self.none_track = RestMiniAudioTrack.NONE
            self._attr_supported_features = (
                MediaPlayerEntityFeature.PAUSE
                | MediaPlayerEntityFeature.PLAY
                | MediaPlayerEntityFeature.STOP
                | MediaPlayerEntityFeature.SELECT_SOUND_MODE
                | MediaPlayerEntityFeature.VOLUME_SET
                | MediaPlayerEntityFeature.VOLUME_STEP
            )
        else:
            self.none_track = RestPlusAudioTrack.NONE
            self._attr_supported_features = (
                MediaPlayerEntityFeature.PAUSE
                | MediaPlayerEntityFeature.PLAY
                | MediaPlayerEntityFeature.STOP
                | MediaPlayerEntityFeature.SELECT_SOUND_MODE
                | MediaPlayerEntityFeature.VOLUME_SET
                | MediaPlayerEntityFeature.VOLUME_STEP
                | MediaPlayerEntityFeature.TURN_ON
                | MediaPlayerEntityFeature.TURN_OFF
            )

    @property
    def sound_mode_list(self) -> list[str]:
        custom_sounds = []
        if hasattr(self.coordinator, "custom_sounds_by_name") and self.coordinator.custom_sounds_by_name:
            custom_sounds = list(self.coordinator.custom_sounds_by_name.keys())
        elif hasattr(self.rest_device, "sounds_by_name") and self.rest_device.sounds_by_name:
            custom_sounds = list(self.rest_device.sounds_by_name.keys())

        if custom_sounds:
            return sorted(custom_sounds)

        if isinstance(self.rest_device, RestMini):
            return [x.name for x in REST_MINI_AUDIO_TRACKS[1:]]
        else:
            return [x.name for x in REST_PLUS_AUDIO_TRACKS[1:]]

    @property
    def state(self) -> MediaPlayerState | None:
        if not self.rest_device:
            return STATE_IDLE
        if isinstance(self.rest_device, RestMini) or self.rest_device.is_on:
            if self.rest_device.is_playing:
                return STATE_PLAYING
            else:
                return STATE_IDLE
        else:
            return STATE_IDLE

    @property
    def sound_mode(self) -> str | None:
        if not self.rest_device:
            return None
        if hasattr(self.rest_device, "audio_track") and self.rest_device.audio_track is not None:
            track_val = (
                self.rest_device.audio_track.value
                if hasattr(self.rest_device.audio_track, "value")
                else self.rest_device.audio_track
            )
            if hasattr(self.coordinator, "custom_sounds_by_id") and track_val in self.coordinator.custom_sounds_by_id:
                return self.coordinator.custom_sounds_by_id[track_val].get("title")
            if hasattr(self.rest_device, "sounds_by_id") and track_val in self.rest_device.sounds_by_id:
                return self.rest_device.sounds_by_id[track_val].get("title")
            if hasattr(self.rest_device.audio_track, "name"):
                return self.rest_device.audio_track.name
            return str(self.rest_device.audio_track)
        return None

    @property
    def volume_level(self) -> float | None:
        if not self.rest_device:
            return None
        return self.rest_device.volume / 100

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
        if not self.rest_device:
            return
        _LOGGER.debug("Select sound mode: %s", sound_mode)
        if hasattr(self.coordinator, "custom_sounds_by_name") and sound_mode in self.coordinator.custom_sounds_by_name:
            sound = self.coordinator.custom_sounds_by_name[sound_mode]
            track_id = sound.get("id")
            url = (
                sound.get("wavUrl")
                or sound.get("mp3Url")
                or sound.get("url")
                or (f"https://assets.ctfassets.net/custom/{sound.get('filename')}" if sound.get("filename") else None)
            )
            if hasattr(self.rest_device, "set_sound_url") and url:
                self.rest_device.set_sound_url(url)
            elif hasattr(self.rest_device, "set_audio_track"):
                self.rest_device.set_audio_track(track_id)
        elif hasattr(self.rest_device, "sounds_by_name") and sound_mode in self.rest_device.sounds_by_name:
            sound = self.rest_device.sounds_by_name[sound_mode]
            track_id = sound.get("id")
            self.rest_device.set_audio_track(track_id)
        else:
            track = self._find_track(sound_mode=sound_mode)
            if track is None:
                track = self.none_track
            self.rest_device.set_audio_track(track)

        if self.config_turn_on_media:
            self.turn_on()

    def media_pause(self) -> None:
        if not self.rest_device:
            return
        self.rest_device.turn_off()

    def media_stop(self) -> None:
        if not self.rest_device:
            return
        self.rest_device.turn_off()

    def turn_on(self) -> None:
        if not self.rest_device:
            return
        self.rest_device.turn_on()

    def turn_off(self) -> None:
        if not self.rest_device:
            return
        self.rest_device.turn_off()

    def _find_track(self, sound_mode: str) -> RestPlusAudioTrack | RestMiniAudioTrack | None:
        if isinstance(self.rest_device, RestMini):
            return next(
                (track for track in REST_MINI_AUDIO_TRACKS if track.name == sound_mode),
                None,
            )
        else:
            return next(
                (track for track in REST_PLUS_AUDIO_TRACKS if track.name == sound_mode),
                None,
            )
