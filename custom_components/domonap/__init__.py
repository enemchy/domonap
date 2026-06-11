from __future__ import annotations

import logging
from datetime import datetime
from secrets import token_urlsafe
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    API,
    PARAM_ACCESS_TOKEN,
    PARAM_DEVICE_TOKEN,
    PARAM_INSTANCE_ID,
    PARAM_REFRESH_TOKEN,
    PARAM_REFRESH_EXPIRATION,
    MEDIA_PROXY,
    PARAM_WEBRTC_PROXY_SECRET,
    PLATFORMS,
    UPDATE_INTERVAL,
    WEBRTC_PROXY,
)

if TYPE_CHECKING:
    from .api import IntercomAPI

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    # Register global actions (services).
    from .actions import async_setup_actions
    from .media_proxy import DomonapMediaProxy, DomonapMediaProxyView
    from .webrtc_proxy import DomonapWebRTCProxy, DomonapWebRTCProxySessionView, DomonapWebRTCProxyView

    await async_setup_actions(hass)
    proxy = DomonapWebRTCProxy(hass)
    hass.data[DOMAIN][WEBRTC_PROXY] = proxy
    hass.http.register_view(DomonapWebRTCProxyView(proxy))
    hass.http.register_view(DomonapWebRTCProxySessionView(proxy))
    media_proxy = DomonapMediaProxy(hass)
    hass.data[DOMAIN][MEDIA_PROXY] = media_proxy
    hass.http.register_view(DomonapMediaProxyView(media_proxy))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .api import IntercomAPI
    from .notify_consumer import IntercomNotifyConsumer

    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    api = IntercomAPI(
        device_token=entry.data.get(PARAM_DEVICE_TOKEN),
        instance_id=entry.data.get(PARAM_INSTANCE_ID),
    )

    new_data = dict(entry.data)
    if not new_data.get(PARAM_WEBRTC_PROXY_SECRET):
        new_data[PARAM_WEBRTC_PROXY_SECRET] = token_urlsafe(24)
    if not new_data.get(PARAM_DEVICE_TOKEN):
        new_data[PARAM_DEVICE_TOKEN] = api.device_token
    if not new_data.get(PARAM_INSTANCE_ID):
        new_data[PARAM_INSTANCE_ID] = api.instance_id
    if new_data != entry.data:
        hass.config_entries.async_update_entry(entry, data=new_data)

    api.set_tokens(
        new_data.get(PARAM_ACCESS_TOKEN),
        new_data.get(PARAM_REFRESH_TOKEN),
        new_data.get(PARAM_REFRESH_EXPIRATION),
    )

    def update_entry(access_token: str, refresh_token: str, refresh_expiration_date: str) -> None:
        _LOGGER.debug("Updating entry tokens in config_entry data")
        new_data = dict(entry.data)
        new_data.setdefault(PARAM_DEVICE_TOKEN, api.device_token)
        new_data.setdefault(PARAM_INSTANCE_ID, api.instance_id)
        new_data.update(
            {
                PARAM_ACCESS_TOKEN: access_token,
                PARAM_REFRESH_TOKEN: refresh_token,
                PARAM_REFRESH_EXPIRATION: refresh_expiration_date,
            }
        )
        hass.config_entries.async_update_entry(entry, data=new_data)

    api.token_update_callback = update_entry

    consumer = IntercomNotifyConsumer(
        hass,
        api,
        hass.data[DOMAIN].get(MEDIA_PROXY),
        new_data.get(PARAM_WEBRTC_PROXY_SECRET),
    )
    hass.data[DOMAIN][entry.entry_id][API] = api
    hass.data[DOMAIN][entry.entry_id]["notify_consumer"] = consumer

    async def _update_tokens_tick(now: datetime) -> None:
        try:
            await api.update_token()
        except Exception:
            _LOGGER.debug("Token refresh failed", exc_info=True)

    unsub_refresh = async_track_time_interval(hass, _update_tokens_tick, UPDATE_INTERVAL)
    hass.data[DOMAIN][entry.entry_id]["unsub_refresh"] = unsub_refresh

    entry.async_create_background_task(hass, consumer.start(), "domonap_notify")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    if (unsub := stored.get("unsub_refresh")) is not None:
        try:
            unsub()
        except Exception:
            _LOGGER.debug("Error unsubscribing refresh timer", exc_info=True)

    consumer = stored.get("notify_consumer")
    if consumer:
        try:
            await consumer.stop()
        except Exception:
            _LOGGER.debug("Exception while stopping notify consumer", exc_info=True)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    api = stored.get(API)
    if api:
        try:
            await api.close()
        except Exception:
            _LOGGER.debug("Exception while closing API client", exc_info=True)

    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    # If this was the last entry, remove services.
    remaining_entries = [
        key for key in hass.data.get(DOMAIN, {}) if key != WEBRTC_PROXY
    ]
    if not remaining_entries:
        from .actions import async_unload_actions

        await async_unload_actions(hass)

    return unloaded
