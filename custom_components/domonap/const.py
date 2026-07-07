from homeassistant.const import Platform

import homeassistant.helpers.config_validation as cv
import voluptuous as vol


DOMAIN = 'domonap'
API = "api"
CONF_COUNTRY_CODE = "country_code"
CONF_PHONE_NUMBER = "phone_number"
CONF_CONFIRM_CODE = "confirm_code"

PARAM_ACCESS_TOKEN = "access_token"
PARAM_REFRESH_TOKEN = "refresh_token"
PARAM_REFRESH_EXPIRATION = "refresh_expiration_date"
PARAM_DEVICE_TOKEN = "device_token"
PARAM_INSTANCE_ID = "instance_id"
PARAM_WEBRTC_PROXY_SECRET = "webrtc_proxy_secret"
EVENT_INCOMING_CALL = "domonap_incoming_call"
WEBRTC_PROXY = "webrtc_proxy"
MEDIA_PROXY = "media_proxy"

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CAMERA, Platform.BINARY_SENSOR, Platform.SENSOR, Platform.IMAGE]

RESET_DELAY = 10 # секунды

WS_MESSAGE_END = "\x1e"
WS_HANDSHAKE_MESSAGE = '{"protocol":"json","version":1}' + WS_MESSAGE_END
WS_PING_MESSAGE = '{"type":6}' + WS_MESSAGE_END
WS_URL = "wss://api.domonap.ru/notificationHub/?id="

# SignalR keep-alive параметры (значения по умолчанию клиента Microsoft SignalR).
# Клиент шлёт app-level ping ({"type":6}) каждые WS_KEEPALIVE_INTERVAL секунд
# (keepAliveInterval), иначе сервер разрывает соединение по ClientTimeoutInterval.
# WS_SERVER_TIMEOUT (serverTimeout) — если за это время НЕ пришло ни одного
# сообщения (включая серверные ping), клиент считает соединение мёртвым, закрывает
# его и переподключается. Реализуется через aiohttp receive_timeout (без WS
# control-ping'ов, которых клиент Microsoft SignalR не использует).
WS_KEEPALIVE_INTERVAL = 15  # секунды
WS_SERVER_TIMEOUT = 30  # секунды
