#!/usr/bin/env python
# -*- coding: utf-8 -*-
#  SPDX-License-Identifier: Apache-2.0
"""
Support to interface with Alexa Devices.

For more details about this platform, please refer to the documentation at
https://community.home-assistant.io/t/echo-devices-alexa-as-media-player-testers-needed/58639
"""
import asyncio
import logging
import re
from typing import List, Text  # noqa pylint: disable=unused-import

from homeassistant import util
from homeassistant.components.media_player import MediaPlayerDevice
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MUSIC,
    SUPPORT_NEXT_TRACK,
    SUPPORT_PAUSE,
    SUPPORT_PLAY,
    SUPPORT_PLAY_MEDIA,
    SUPPORT_PREVIOUS_TRACK,
    SUPPORT_SELECT_SOURCE,
    SUPPORT_SHUFFLE_SET,
    SUPPORT_STOP,
    SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON,
    SUPPORT_VOLUME_MUTE,
    SUPPORT_VOLUME_SET,
)
from homeassistant.const import (
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
)
from homeassistant.helpers.event import async_call_later

from . import (
    CONF_EMAIL,
    DATA_ALEXAMEDIA,
    DOMAIN as ALEXA_DOMAIN,
    MIN_TIME_BETWEEN_FORCED_SCANS,
    MIN_TIME_BETWEEN_SCANS,
    hide_email,
    hide_serial,
)
from .const import PLAY_SCAN_INTERVAL
from .helpers import _catch_login_errors, add_devices, retry_async

SUPPORT_ALEXA = (
    SUPPORT_PAUSE
    | SUPPORT_PREVIOUS_TRACK
    | SUPPORT_NEXT_TRACK
    | SUPPORT_STOP
    | SUPPORT_VOLUME_SET
    | SUPPORT_PLAY
    | SUPPORT_PLAY_MEDIA
    | SUPPORT_TURN_OFF
    | SUPPORT_TURN_ON
    | SUPPORT_VOLUME_MUTE
    | SUPPORT_PAUSE
    | SUPPORT_SELECT_SOURCE
    | SUPPORT_SHUFFLE_SET
)
_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = [ALEXA_DOMAIN]


@retry_async(limit=5, delay=2, catch_exceptions=True)
async def async_setup_platform(hass, config, add_devices_callback, discovery_info=None):
    # pylint: disable=unused-argument
    """Set up the Alexa media player platform."""
    devices = []  # type: List[AlexaClient]
    account = config[CONF_EMAIL]
    account_dict = hass.data[DATA_ALEXAMEDIA]["accounts"][account]
    for key, device in account_dict["devices"]["media_player"].items():
        if key not in account_dict["entities"]["media_player"]:
            alexa_client = AlexaClient(device, account_dict["login_obj"])
            await alexa_client.init(device)
            devices.append(alexa_client)
            (
                hass.data[DATA_ALEXAMEDIA]["accounts"][account]["entities"][
                    "media_player"
                ][key]
            ) = alexa_client
        else:
            _LOGGER.debug(
                "%s: Skipping already added device: %s:%s",
                hide_email(account),
                hide_serial(key),
                alexa_client,
            )
    return await add_devices(hide_email(account), devices, add_devices_callback)


async def async_setup_entry(hass, config_entry, async_add_devices):
    """Set up the Alexa media player platform by config_entry."""
    return await async_setup_platform(
        hass, config_entry.data, async_add_devices, discovery_info=None
    )


async def async_unload_entry(hass, entry) -> bool:
    """Unload a config entry."""
    account = entry.data[CONF_EMAIL]
    account_dict = hass.data[DATA_ALEXAMEDIA]["accounts"][account]
    for device in account_dict["entities"]["media_player"].values():
        await device.async_remove()
    return True


class AlexaClient(MediaPlayerDevice):
    """Representation of a Alexa device."""

    def __init__(self, device, login):
        # pylint: disable=unused-argument
        """Initialize the Alexa device."""
        from alexapy import AlexaAPI

        # Class info
        self._login = login
        self.alexa_api = AlexaAPI(self, login)
        self.auth = None
        self.alexa_api_session = login.session
        self.account = hide_email(login.email)

        # Logged in info
        self._authenticated = None
        self._can_access_prime_music = None
        self._customer_email = None
        self._customer_id = None
        self._customer_name = None

        # Device info
        self._device_name = None
        self._device_serial_number = None
        self._device_type = None
        self._device_family = None
        self._device_owner_customer_id = None
        self._software_version = None
        self._available = None
        self._capabilities = []
        self._cluster_members = []
        self._locale = None
        # Media
        self._session = None
        self._media_duration = None
        self._media_image_url = None
        self._media_title = None
        self._media_pos = None
        self._media_album_name = None
        self._media_artist = None
        self._media_player_state = None
        self._media_is_muted = None
        self._media_vol_level = None
        self._previous_volume = None
        self._source = None
        self._source_list = []
        self._shuffle = None
        self._repeat = None
        self._playing_parent = None
        # Last Device
        self._last_called = None
        self._last_called_timestamp = None
        # Do not Disturb state
        self._dnd = None
        # Polling state
        self._should_poll = True
        self._last_update = util.utcnow()
        self._listener = None
        self._bluetooth_state = None
        self._app_device_list = None
        self._parent_clusters = None
        self._timezone = None

    async def init(self, device):
        """Initialize."""
        await self.refresh(device)

    async def async_added_to_hass(self):
        """Perform tasks after loading."""
        # Register event handler on bus
        self._listener = self.hass.bus.async_listen(
            f"{ALEXA_DOMAIN}_{hide_email(self._login.email)}"[0:32], self._handle_event
        )

    async def async_will_remove_from_hass(self):
        """Prepare to remove entity."""
        # Register event handler on bus
        self._listener()

    async def _handle_event(self, event):
        """Handle events.

        This will update last_called and player_state events.
        Each MediaClient reports if it's the last_called MediaClient and will
        listen for HA events to determine it is the last_called.
        When polling instead of websockets, all devices on same account will
        update to handle starting music with other devices. If websocket is on
        only the updated alexa will update.
        Last_called events are only sent if it's a new device or timestamp.
        Without polling, we must schedule the HA update manually.
        https://developers.home-assistant.io/docs/en/entity_index.html#subscribing-to-updates
        The difference between self.update and self.schedule_update_ha_state
        is self.update will pull data from Amazon, while schedule_update
        assumes the MediaClient state is already updated.
        """

        async def _refresh_if_no_audiopush(already_refreshed=False):
            email = self._login.email
            seen_commands = (
                self.hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                    "websocket_commands"
                ].keys()
                if "websocket_commands"
                in (self.hass.data[DATA_ALEXAMEDIA]["accounts"][email])
                else None
            )
            if (
                not already_refreshed
                and seen_commands
                and not (
                    "PUSH_AUDIO_PLAYER_STATE" in seen_commands
                    or "PUSH_MEDIA_CHANGE" in seen_commands
                    or "PUSH_MEDIA_PROGRESS_CHANGE" in seen_commands
                )
            ):
                # force refresh if player_state update not found, see #397
                _LOGGER.debug(
                    "%s: No PUSH_AUDIO_PLAYER_STATE/"
                    "PUSH_MEDIA_CHANGE/PUSH_MEDIA_PROGRESS_CHANGE in %s;"
                    "forcing refresh",
                    hide_email(email),
                    seen_commands,
                )
                await self.async_update()

        try:
            if not self.enabled:
                return
        except AttributeError:
            pass
        already_refreshed = False
        event_serial = None
        if "last_called_change" in event.data:
            event_serial = (
                event.data["last_called_change"]["serialNumber"]
                if event.data["last_called_change"]
                else None
            )
        elif "bluetooth_change" in event.data:
            event_serial = (
                event.data["bluetooth_change"]["deviceSerialNumber"]
                if event.data["bluetooth_change"]
                else None
            )
        elif "player_state" in event.data:
            event_serial = (
                event.data["player_state"]["dopplerId"]["deviceSerialNumber"]
                if event.data["player_state"]
                else None
            )
        elif "queue_state" in event.data:
            event_serial = (
                event.data["queue_state"]["dopplerId"]["deviceSerialNumber"]
                if event.data["queue_state"]
                else None
            )
        elif "push_activity" in event.data:
            event_serial = (
                event.data.get("push_activity", {}).get("key", {}).get("serialNumber")
            )
        if not event_serial:
            return
        self.available = True
        self.async_schedule_update_ha_state()
        if "last_called_change" in event.data:
            if event_serial == self.device_serial_number or any(
                item["serialNumber"] == event_serial for item in self._app_device_list
            ):
                _LOGGER.debug(
                    "%s is last_called: %s",
                    self.name,
                    hide_serial(self.device_serial_number),
                )
                self._last_called = True
                self._last_called_timestamp = event.data["last_called_change"][
                    "timestamp"
                ]
            else:
                self._last_called = False
            if self.hass and self.async_schedule_update_ha_state:
                email = self._login.email
                force_refresh = not (
                    self.hass.data[DATA_ALEXAMEDIA]["accounts"][email]["websocket"]
                )
                self.async_schedule_update_ha_state(force_refresh=force_refresh)
        elif "bluetooth_change" in event.data:
            if event_serial == self.device_serial_number:
                _LOGGER.debug(
                    "%s bluetooth_state update: %s",
                    self.name,
                    hide_serial(event.data["bluetooth_change"]),
                )
                self._bluetooth_state = event.data["bluetooth_change"]
                # the setting of bluetooth_state is not consistent as this
                # takes from the event instead of the hass storage. We're
                # setting the value twice. Architectually we should have a
                # single authorative source of truth.
                self._source = await self._get_source()
                self._source_list = await self._get_source_list()
                if self.hass and self.async_schedule_update_ha_state:
                    self.async_schedule_update_ha_state()
        elif "player_state" in event.data:
            player_state = event.data["player_state"]
            if event_serial == self.device_serial_number:
                if "audioPlayerState" in player_state:
                    _LOGGER.debug(
                        "%s state update: %s",
                        self.name,
                        player_state["audioPlayerState"],
                    )
                    # allow delay before trying to refresh to avoid http 400 errors
                    await asyncio.sleep(2)
                    await self.async_update()
                    already_refreshed = True
                elif "mediaReferenceId" in player_state:
                    _LOGGER.debug(
                        "%s media update: %s",
                        self.name,
                        player_state["mediaReferenceId"],
                    )
                    await self.async_update()
                    already_refreshed = True
                elif "volumeSetting" in player_state:
                    _LOGGER.debug(
                        "%s volume updated: %s",
                        self.name,
                        player_state["volumeSetting"],
                    )
                    self._media_vol_level = player_state["volumeSetting"] / 100
                    if self.hass and self.async_schedule_update_ha_state:
                        self.async_schedule_update_ha_state()
                elif "dopplerConnectionState" in player_state:
                    self.available = player_state["dopplerConnectionState"] == "ONLINE"
                    if self.hass and self.async_schedule_update_ha_state:
                        self.async_schedule_update_ha_state()
                await _refresh_if_no_audiopush(already_refreshed)
        elif "push_activity" in event.data:
            if self.state in {STATE_IDLE, STATE_PAUSED, STATE_PLAYING}:
                _LOGGER.debug(
                    "%s checking for potential state update due to push activity on %s",
                    self.name,
                    hide_serial(event_serial),
                )
                # allow delay before trying to refresh to avoid http 400 errors
                await asyncio.sleep(2)
                await self.async_update()
                already_refreshed = True
        if "queue_state" in event.data:
            queue_state = event.data["queue_state"]
            if event_serial == self.device_serial_number:
                if (
                    "trackOrderChanged" in queue_state
                    and not queue_state["trackOrderChanged"]
                    and "loopMode" in queue_state
                ):
                    self._repeat = queue_state["loopMode"] == "LOOP_QUEUE"
                    _LOGGER.debug(
                        "%s repeat updated to: %s %s",
                        self.name,
                        self._repeat,
                        queue_state["loopMode"],
                    )
                elif "playBackOrder" in queue_state:
                    self._shuffle = queue_state["playBackOrder"] == "SHUFFLE_ALL"
                    _LOGGER.debug(
                        "%s shuffle updated to: %s %s",
                        self.name,
                        self._shuffle,
                        queue_state["playBackOrder"],
                    )
                await _refresh_if_no_audiopush(already_refreshed)

    async def _clear_media_details(self):
        """Set all Media Items to None."""
        # General
        self._media_duration = None
        self._media_image_url = None
        self._media_title = None
        self._media_pos = None
        self._media_album_name = None
        self._media_artist = None
        self._media_player_state = None
        self._media_is_muted = None
        # volume is also used for announce/tts so state should remain
        # self._media_vol_level = None

    async def _set_authentication_details(self, auth):
        """Set Authentication based off auth."""
        self._authenticated = auth["authenticated"]
        self._can_access_prime_music = auth["canAccessPrimeMusicContent"]
        self._customer_email = auth["customerEmail"]
        self._customer_id = auth["customerId"]
        self._customer_name = auth["customerName"]

    @util.Throttle(MIN_TIME_BETWEEN_SCANS, MIN_TIME_BETWEEN_FORCED_SCANS)
    @_catch_login_errors
    async def refresh(self, device=None, skip_api: bool = False):
        """Refresh device data.

        This is a per device refresh and for many Alexa devices can result in
        many refreshes from each individual device. This will call the
        AlexaAPI directly.

        Args:
        device (json): A refreshed device json from Amazon. For efficiency,
                       an individual device does not refresh if it's reported
                       as offline.
        no_api (bool): Whether to only due a device json update and not hit the API

        """
        if device is not None:
            self._device_name = device["accountName"]
            self._device_family = device["deviceFamily"]
            self._device_type = device["deviceType"]
            self._device_serial_number = device["serialNumber"]
            self._app_device_list = device["appDeviceList"]
            self._device_owner_customer_id = device["deviceOwnerCustomerId"]
            self._software_version = device["softwareVersion"]
            self._available = device["online"]
            self._capabilities = device["capabilities"]
            self._cluster_members = device["clusterMembers"]
            self._parent_clusters = device["parentClusters"]
            self._bluetooth_state = device["bluetooth_state"]
            self._locale = device["locale"] if "locale" in device else "en-US"
            self._timezone = device["timeZoneId"] if "timeZoneId" in device else "UTC"
            self._dnd = device["dnd"] if "dnd" in device else None
            await self._set_authentication_details(device["auth_info"])
        session = None
        if self.available:
            _LOGGER.debug("%s: Refreshing %s", self.account, self.name)
            if "PAIR_BT_SOURCE" in self._capabilities:
                self._source = await self._get_source()
                self._source_list = await self._get_source_list()
            self._last_called = await self._get_last_called()
            if self._last_called:
                self._last_called_timestamp = self.hass.data[DATA_ALEXAMEDIA][
                    "accounts"
                ][self._login.email]["last_called"]["timestamp"]
            if skip_api:
                return
            if "MUSIC_SKILL" in self._capabilities:
                if self._parent_clusters and self.hass:
                    playing_parents = list(
                        filter(
                            lambda x: (
                                self.hass.data[DATA_ALEXAMEDIA]["accounts"][
                                    self._login.email
                                ]["entities"]["media_player"].get(x)
                                and self.hass.data[DATA_ALEXAMEDIA]["accounts"][
                                    self._login.email
                                ]["entities"]["media_player"][x].state
                                == STATE_PLAYING
                            ),
                            self._parent_clusters,
                        )
                    )
                else:
                    playing_parents = []
                parent_session = {}
                if playing_parents:
                    if len(playing_parents) > 1:
                        _LOGGER.warning(
                            "Found multiple playing parents " "please file an issue"
                        )
                    parent = self.hass.data[DATA_ALEXAMEDIA]["accounts"][
                        self._login.email
                    ]["entities"]["media_player"][playing_parents[0]]
                    self._playing_parent = parent
                    parent_session = parent.session
                if parent_session:
                    session = parent_session.copy()
                    session["isPlayingInLemur"] = False
                    session["lemurVolume"] = None
                    session["volume"] = (
                        parent_session["lemurVolume"]["memberVolume"][
                            self.device_serial_number
                        ]
                        if parent_session.get("lemurVolume")
                        and parent_session.get("lemurVolume", {})
                        .get("memberVolume", {})
                        .get(self.device_serial_number)
                        else session["volume"]
                    )
                    session = {"playerInfo": session}
                else:
                    self._playing_parent = None
                    session = await self.alexa_api.get_state()
        await self._clear_media_details()
        # update the session if it exists
        self._session = session if session else None
        if self._session and self._session.get("playerInfo"):
            self._session = self._session["playerInfo"]
            if self._session.get("transport"):
                self._shuffle = (
                    self._session["transport"]["shuffle"] == "SELECTED"
                    if (
                        "shuffle" in self._session["transport"]
                        and self._session["transport"]["shuffle"] != "DISABLED"
                    )
                    else None
                )
                self._repeat = (
                    self._session["transport"]["repeat"] == "SELECTED"
                    if (
                        "repeat" in self._session["transport"]
                        and self._session["transport"]["repeat"] != "DISABLED"
                    )
                    else None
                )
            if self._session.get("state"):
                self._media_player_state = self._session["state"]
                self._media_pos = self._session.get("progress", {}).get("mediaProgress")
                self._media_title = self._session.get("infoText", {}).get("title")
                self._media_artist = self._session.get("infoText", {}).get("subText1")
                self._media_album_name = self._session.get("infoText", {}).get(
                    "subText2"
                )
                self._media_image_url = self._session.get("mainArt", {}).get("url")
                self._media_duration = self._session.get("progress", {}).get(
                    "mediaLength"
                )
                if not self._session.get("lemurVolume"):
                    self._media_is_muted = (
                        self._session.get("volume", {}).get("muted")
                        if self._session.get("volume")
                        else self._media_is_muted
                    )
                    self._media_vol_level = (
                        self._session["volume"]["volume"] / 100
                        if self._session.get("volume")
                        and self._session.get("volume", {}).get("volume")
                        else self._media_vol_level
                    )
                else:
                    self._media_is_muted = (
                        self._session.get("lemurVolume", {})
                        .get("compositeVolume", {})
                        .get("muted")
                    )
                    self._media_vol_level = (
                        self._session["lemurVolume"]["compositeVolume"]["volume"] / 100
                        if (
                            self._session.get("lemurVolume", {})
                            .get("compositeVolume", {})
                            .get("volume")
                        )
                        else self._media_vol_level
                    )
                if self.hass and self._session.get("isPlayingInLemur"):
                    asyncio.gather(
                        *map(
                            lambda x: (
                                self.hass.data[DATA_ALEXAMEDIA]["accounts"][
                                    self._login.email
                                ]["entities"]["media_player"][x].async_update()
                            ),
                            filter(
                                lambda x: (
                                    self.hass.data[DATA_ALEXAMEDIA]["accounts"][
                                        self._login.email
                                    ]["entities"]["media_player"].get(x)
                                    and self.hass.data[DATA_ALEXAMEDIA]["accounts"][
                                        self._login.email
                                    ]["entities"]["media_player"][x].available
                                ),
                                self._cluster_members,
                            ),
                        )
                    )

    @property
    def source(self):
        """Return the current input source."""
        return self._source

    @property
    def source_list(self):
        """List of available input sources."""
        return self._source_list

    @_catch_login_errors
    async def async_select_source(self, source):
        """Select input source."""
        if source == "Local Speaker":
            await self.alexa_api.disconnect_bluetooth()
            self._source = "Local Speaker"
        elif self._bluetooth_state["pairedDeviceList"] is not None:
            for devices in self._bluetooth_state["pairedDeviceList"]:
                if devices["friendlyName"] == source:
                    await self.alexa_api.set_bluetooth(devices["address"])
                    self._source = source
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    async def _get_source(self):
        source = "Local Speaker"
        if self._bluetooth_state.get("pairedDeviceList"):
            for device in self._bluetooth_state["pairedDeviceList"]:
                if (
                    device["connected"] is True
                    and device["friendlyName"] in self.source_list
                ):
                    return device["friendlyName"]
        return source

    async def _get_source_list(self):
        sources = []
        if self._bluetooth_state.get("pairedDeviceList"):
            for devices in self._bluetooth_state["pairedDeviceList"]:
                if devices["profiles"] and "A2DP-SOURCE" in devices["profiles"]:
                    sources.append(devices["friendlyName"])
        return ["Local Speaker"] + sources

    async def _get_last_called(self):
        try:
            last_called_serial = (
                None
                if self.hass is None
                else (
                    self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email][
                        "last_called"
                    ]["serialNumber"]
                )
            )
        except (TypeError, KeyError):
            last_called_serial = None
        _LOGGER.debug(
            "%s: Last_called check: self: %s reported: %s",
            self._device_name,
            hide_serial(self._device_serial_number),
            hide_serial(last_called_serial),
        )
        return last_called_serial is not None and (
            self._device_serial_number == last_called_serial
            or any(
                item["serialNumber"] == last_called_serial
                for item in self._app_device_list
            )
        )

    @property
    def available(self):
        """Return the availability of the client."""
        return self._available

    @available.setter
    def available(self, state):
        """Set the availability state."""
        self._available = state

    @property
    def hidden(self):
        """Return whether the sensor should be hidden."""
        return "MUSIC_SKILL" not in self._capabilities

    @property
    def unique_id(self):
        """Return the id of this Alexa client."""
        return self.device_serial_number

    @property
    def name(self):
        """Return the name of the device."""
        return self._device_name

    @property
    def device_serial_number(self):
        """Return the machine identifier of the device."""
        return self._device_serial_number

    @property
    def session(self):
        """Return the session, if any."""
        return self._session

    @property
    def state(self):
        """Return the state of the device."""
        if not self.available:
            return STATE_UNAVAILABLE
        if self._media_player_state == "PLAYING":
            return STATE_PLAYING
        if self._media_player_state == "PAUSED":
            return STATE_PAUSED
        if self._media_player_state == "IDLE":
            return STATE_IDLE
        return STATE_STANDBY

    @_catch_login_errors
    async def async_update(self):
        """Get the latest details on a media player.

        Because media players spend the majority of time idle, an adaptive
        update should be used to avoid flooding Amazon focusing on known
        play states. An initial version included an update_devices call on
        every update. However, this quickly floods the network for every new
        device added. This should only call refresh() to call the AlexaAPI.
        """
        try:
            if not self.enabled:
                return
        except AttributeError:
            pass
        if self.entity_id is None:
            # Device has not initialized yet
            return
        email = self._login.email
        if email not in self.hass.data[DATA_ALEXAMEDIA]["accounts"]:
            return
        device = self.hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"][
            "media_player"
        ][self.unique_id]
        seen_commands = (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                "websocket_commands"
            ].keys()
            if "websocket_commands"
            in (self.hass.data[DATA_ALEXAMEDIA]["accounts"][email])
            else None
        )
        await self.refresh(  # pylint: disable=unexpected-keyword-arg
            device, no_throttle=True
        )
        websocket_enabled = self.hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
            "websocket"
        )
        if (
            self.state in [STATE_PLAYING]
            and
            #  only enable polling if websocket not connected
            (
                not websocket_enabled
                or not seen_commands
                or not (
                    "PUSH_AUDIO_PLAYER_STATE" in seen_commands
                    or "PUSH_MEDIA_CHANGE" in seen_commands
                    or "PUSH_MEDIA_PROGRESS_CHANGE" in seen_commands
                )
            )
        ):
            self._should_poll = False  # disable polling since manual update
            if (
                self._last_update == 0
                or util.dt.as_timestamp(util.utcnow())
                - util.dt.as_timestamp(self._last_update)
                > PLAY_SCAN_INTERVAL
            ):
                _LOGGER.debug(
                    "%s playing; scheduling update in %s seconds",
                    self.name,
                    PLAY_SCAN_INTERVAL,
                )
                async_call_later(
                    self.hass,
                    PLAY_SCAN_INTERVAL,
                    lambda _: self.async_schedule_update_ha_state(force_refresh=True),
                )
        elif self._should_poll:  # Not playing, one last poll
            self._should_poll = False
            if not websocket_enabled:
                _LOGGER.debug(
                    "Disabling polling and scheduling last update in"
                    " 300 seconds for %s",
                    self.name,
                )
                async_call_later(
                    self.hass,
                    300,
                    lambda _: self.async_schedule_update_ha_state(force_refresh=True),
                )
            else:
                _LOGGER.debug("Disabling polling for %s", self.name)
        self._last_update = util.utcnow()
        self.async_schedule_update_ha_state()

    @property
    def media_content_type(self):
        """Return the content type of current playing media."""
        if self.state in [STATE_PLAYING, STATE_PAUSED]:
            return MEDIA_TYPE_MUSIC
        return STATE_STANDBY

    @property
    def media_artist(self):
        """Return the artist of current playing media, music track only."""
        return self._media_artist

    @property
    def media_album_name(self):
        """Return the album name of current playing media, music track only."""
        return self._media_album_name

    @property
    def media_duration(self):
        """Return the duration of current playing media in seconds."""
        return self._media_duration

    @property
    def media_position(self):
        """Return the duration of current playing media in seconds."""
        return self._media_pos

    @property
    def media_position_updated_at(self):
        """When was the position of the current playing media valid."""
        return self._last_update

    @property
    def media_image_url(self) -> Text:
        """Return the image URL of current playing media."""
        if self._media_image_url:
            return re.sub("\\(", "%28", re.sub("\\)", "%29", self._media_image_url))
            # fix failure of HA media player ui to quote "(" or ")"
        return None

    @property
    def media_image_remotely_accessible(self):
        """Return whether image is accessible outside of the home network."""
        return bool(self._media_image_url)

    @property
    def media_title(self):
        """Return the title of current playing media."""
        return self._media_title

    @property
    def device_family(self):
        """Return the make of the device (ex. Echo, Other)."""
        return self._device_family

    @property
    def dnd_state(self):
        """Return the Do Not Disturb state."""
        return self._dnd

    @dnd_state.setter
    def dnd_state(self, state):
        """Set the Do Not Disturb state."""
        self._dnd = state

    @_catch_login_errors
    async def async_set_shuffle(self, shuffle):
        """Enable/disable shuffle mode."""
        await self.alexa_api.shuffle(shuffle)
        self._shuffle = shuffle

    @property
    def shuffle(self):
        """Return the Shuffle state."""
        return self._shuffle

    @shuffle.setter
    def shuffle(self, state):
        """Set the Shuffle state."""
        self._shuffle = state
        self.async_schedule_update_ha_state()

    @property
    def repeat_state(self):
        """Return the Repeat state."""
        return self._repeat

    @repeat_state.setter
    def repeat_state(self, state):
        """Set the Repeat state."""
        self._repeat = state
        self.async_schedule_update_ha_state()

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_ALEXA

    @_catch_login_errors
    async def async_set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        if not self.available:
            return
        await self.alexa_api.set_volume(volume)
        self._media_vol_level = volume
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @property
    def volume_level(self):
        """Return the volume level of the client (0..1)."""
        return self._media_vol_level

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        if self.volume_level == 0:
            return True
        return False

    @_catch_login_errors
    async def async_mute_volume(self, mute):
        """Mute the volume.

        Since we can't actually mute, we'll:
        - On mute, store volume and set volume to 0
        - On unmute, set volume to previously stored volume
        """
        if not self.available:
            return

        self._media_is_muted = mute
        if mute:
            self._previous_volume = self.volume_level
            await self.alexa_api.set_volume(0)
        else:
            if self._previous_volume is not None:
                await self.alexa_api.set_volume(self._previous_volume)
            else:
                await self.alexa_api.set_volume(50)
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @_catch_login_errors
    async def async_media_play(self):
        """Send play command."""
        if not (self.state in [STATE_PLAYING, STATE_PAUSED] and self.available):
            return
        if self._playing_parent:
            await self._playing_parent.async_media_play()
        else:
            await self.alexa_api.play()
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @_catch_login_errors
    async def async_media_pause(self):
        """Send pause command."""
        if not (self.state in [STATE_PLAYING, STATE_PAUSED] and self.available):
            return
        if self._playing_parent:
            await self._playing_parent.async_media_pause()
        else:
            await self.alexa_api.pause()
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @_catch_login_errors
    async def async_turn_off(self):
        """Turn the client off.

        While Alexa's do not have on/off capability, we can use this as another
        trigger to do updates. For turning off, we can clear media_details.
        """
        self._should_poll = False
        await self.async_media_pause()
        await self._clear_media_details()

    @_catch_login_errors
    async def async_turn_on(self):
        """Turn the client on.

        While Alexa's do not have on/off capability, we can use this as another
        trigger to do updates.
        """
        self._should_poll = True
        await self.async_media_pause()

    @_catch_login_errors
    async def async_media_next_track(self):
        """Send next track command."""
        if not (self.state in [STATE_PLAYING, STATE_PAUSED] and self.available):
            return
        if self._playing_parent:
            await self._playing_parent.async_media_next_track()
        else:
            await self.alexa_api.next()
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @_catch_login_errors
    async def async_media_previous_track(self):
        """Send previous track command."""
        if not (self.state in [STATE_PLAYING, STATE_PAUSED] and self.available):
            return
        if self._playing_parent:
            await self._playing_parent.async_media_previous_track()
        else:
            await self.alexa_api.previous()
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @_catch_login_errors
    async def async_send_tts(self, message, **kwargs):
        """Send TTS to Device.

        NOTE: Does not work on WHA Groups.
        """
        await self.alexa_api.send_tts(message, customer_id=self._customer_id, **kwargs)

    @_catch_login_errors
    async def async_send_announcement(self, message, **kwargs):
        """Send announcement to the media player."""
        await self.alexa_api.send_announcement(
            message, customer_id=self._customer_id, **kwargs
        )

    @_catch_login_errors
    async def async_send_mobilepush(self, message, **kwargs):
        """Send push to the media player's associated mobile devices."""
        await self.alexa_api.send_mobilepush(
            message, customer_id=self._customer_id, **kwargs
        )

    @_catch_login_errors
    async def async_play_media(self, media_type, media_id, enqueue=None, **kwargs):
        # pylint: disable=unused-argument
        """Send the play_media command to the media player."""
        if media_type == "music":
            await self.async_send_tts(
                "Sorry, text to speech can only be called"
                " with the notify.alexa_media service."
                " Please see the alexa_media wiki for details."
            )
            _LOGGER.warning(
                "Sorry, text to speech can only be called"
                " with the notify.alexa_media service."
                " Please see the alexa_media wiki for details."
                "https://github.com/custom-components/alexa_media_player/wiki/Configuration%3A-Notification-Component#use-the-notifyalexa_media-service"
            )
        elif media_type == "sequence":
            await self.alexa_api.send_sequence(
                media_id, customer_id=self._customer_id, **kwargs
            )
        elif media_type == "routine":
            await self.alexa_api.run_routine(media_id)
        elif media_type == "sound":
            await self.alexa_api.play_sound(
                media_id, customer_id=self._customer_id, **kwargs
            )
        else:
            await self.alexa_api.play_music(
                media_type, media_id, customer_id=self._customer_id, **kwargs
            )
        if not (
            self.hass.data[DATA_ALEXAMEDIA]["accounts"][self._login.email]["websocket"]
        ):
            await self.async_update()

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        attr = {
            "available": self.available,
            "last_called": self._last_called,
            "last_called_timestamp": self._last_called_timestamp,
        }
        return attr

    @property
    def should_poll(self):
        """Return the polling state."""
        return self._should_poll

    @property
    def device_info(self):
        """Return the device_info of the device."""
        return {
            "identifiers": {(ALEXA_DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Amazon",
            "model": f"{self._device_family} {self._device_type}",
            "sw_version": self._software_version,
        }
