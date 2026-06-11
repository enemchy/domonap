import logging
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from secrets import token_urlsafe
from typing import Any, Dict, Optional, Union
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)


DEFAULT_DEVICE_PLATFORM = "Android"
DEFAULT_DOM_APP = "mobile"
DEFAULT_JSON_CONTENT_TYPE = "application/json; charset=UTF-8"
DEFAULT_USER_AGENT = "okhttp/5.3.2"


def _with_app_header_suffix(value: str) -> str:
    return value if value.endswith(";") else f"{value};"


def _generate_device_token() -> str:
    return f"{token_urlsafe(22)}:APA91b{token_urlsafe(134)}"


class IntercomAPI:
    def __init__(
        self,
        base_url: str = "https://api.domonap.ru",
        device_token: Optional[str] = None,
        instance_id: Optional[str] = None,
        device_platform: str = DEFAULT_DEVICE_PLATFORM,
        dom_app: str = DEFAULT_DOM_APP,
        device_token_check_interval: int = 300,
        refresh_skew_seconds: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.refresh_expiration_date: Optional[str] = None
        self.device_token = device_token or _generate_device_token()
        self.instance_id = instance_id or str(uuid4())
        self.device_platform = device_platform
        self.dom_app = dom_app
        self.device_token_check_interval = device_token_check_interval
        self._last_device_token_check: Optional[datetime] = None
        self._updating_device_token: bool = False
        self.refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self.headers: Dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "dom-app": _with_app_header_suffix(self.dom_app),
            "dom-platform": _with_app_header_suffix(self.device_platform),
            "instanceId": _with_app_header_suffix(self.instance_id),
        }
        self.token_update_callback = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._external_session: Optional[aiohttp.ClientSession] = None
        self._closed = False

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if not self._session or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        return self._session

    async def _ensure_external_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Client is closed")
        if not self._external_session or self._external_session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._external_session = aiohttp.ClientSession(timeout=timeout)
        return self._external_session

    async def close(self):
        self._closed = True
        if self._session and not self._session.closed:
            await self._session.close()
        if self._external_session and not self._external_session.closed:
            await self._external_session.close()

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def set_tokens(self, access_token: str, refresh_token: str, refresh_expiration_date: str):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.refresh_expiration_date = refresh_expiration_date
        self.headers.pop("Authorization", None)
        if self._session and not self._session.closed:
            self._session._default_headers.clear()
            self._session._default_headers.update(self.headers)

    def _parse_dt(self, val: str) -> Optional[datetime]:
        fmts = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
        for fmt in fmts:
            try:
                return datetime.strptime(val, fmt)
            except ValueError:
                continue
        _LOGGER.warning("Cannot parse datetime: %s", val)
        return None

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    async def _maybe_refresh_token(self) -> None:
        if not self.refresh_expiration_date:
            return
        exp = self._parse_dt(self.refresh_expiration_date)
        if not exp:
            return
        if self._now_utc() >= (exp - self.refresh_skew):
            _LOGGER.info("Refreshing tokens (old refresh_expiration: %s, now: %s)", self.refresh_expiration_date, self._now_utc())
            await self.update_token()

    async def _maybe_update_device_token(self) -> None:
        if self._updating_device_token:
            return
        now = self._now_utc()
        if (
            self._last_device_token_check is None
            or (now - self._last_device_token_check).total_seconds() >= self.device_token_check_interval
        ):
            self._updating_device_token = True
            try:
                ok = await self.update_device_token(self.device_token)
                self._last_device_token_check = now
                if not ok:
                    _LOGGER.debug("Device token not updated")
            finally:
                self._updating_device_token = False

    async def _ensure_alive(self) -> None:
        await self._maybe_refresh_token()
        if self.access_token:
            await self._maybe_update_device_token()

    async def _post(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        need_auth: bool = False,
        ensure_alive: bool = True,
        send_auth: Optional[bool] = None,
        expect: str = "json",
        retry_on_401: bool = True,
    ) -> Union[Dict[str, Any], str]:
        if send_auth is None:
            send_auth = need_auth
        if need_auth:
            if not self.access_token:
                return {"error": "No access token available", "ok": False, "body": ""}
            if ensure_alive:
                await self._ensure_alive()

        session = await self._ensure_session()
        url = f"{self.base_url}{path}"

        async def _do() -> aiohttp.ClientResponse:
            headers = dict(self.headers)
            if payload is not None:
                headers["Content-Type"] = DEFAULT_JSON_CONTENT_TYPE
            if send_auth and self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            if payload is None:
                return await session.post(url, headers=headers, ssl=False)
            return await session.post(url, json=payload, headers=headers, ssl=False)

        resp = await _do()
        if resp.status == 401 and retry_on_401 and self.refresh_token:
            _LOGGER.warning("401 Unauthorized, refreshing token and retrying %s", path)
            await self.update_token()
            resp = await _do()

        if 200 <= resp.status < 300:
            if expect == "json":
                return await resp.json()
            return await resp.text()

        body_text = ""
        try:
            body_text = await resp.text()
        except Exception:
            pass
        err = {"error": f"HTTP {resp.status}", "status": resp.status, "body": body_text[:2000]}
        _LOGGER.error("Request failed: POST %s payload=%s -> %s", path, payload, err)
        return err

    async def update_device_token(self, device_token: str) -> bool:
        _LOGGER.debug("UpdateDeviceToken start")
        result = await self._post(
            "/sso-api/Authorization/UpdateDeviceToken",
            {"deviceToken": device_token, "platform": self.device_platform},
            need_auth=True,
            ensure_alive=False,
            expect="text",
            retry_on_401=True,
        )
        if isinstance(result, dict) and "error" in result:
            _LOGGER.error("UpdateDeviceToken failed: %s", result)
            return False
        self._last_device_token_check = self._now_utc()
        _LOGGER.debug("UpdateDeviceToken ok")
        return True

    async def authorize(self, country_code: str, phone_number: str) -> Union[bool, Dict[str, Any]]:
        payload = {"phoneNumber": self._phone_number(country_code, phone_number)}
        res = await self._post("/sso-api/Authorization/Authorize", payload, expect="text", need_auth=False)
        if isinstance(res, dict) and "error" in res:
            return {"error": f"Authorization failed: {res}"}
        return True

    async def confirm_authorization(
        self,
        country_code: str,
        phone_number: str,
        confirm_code: str,
        device_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "phoneNumber": self._phone_number(country_code, phone_number),
            "confirmCode": confirm_code,
            "deviceToken": device_token or self.device_token,
        }
        res = await self._post("/sso-api/Authorization/ConfirmAuthorization", payload, expect="json", need_auth=False)
        if isinstance(res, dict) and "error" in res and "status" in res:
            return res
        try:
            ct = res["completeToken"]
            self.set_tokens(ct["accessToken"], ct["refreshToken"], ct["refreshExpirationDate"])
            if self.token_update_callback:
                self.token_update_callback(ct["accessToken"], ct["refreshToken"], ct["refreshExpirationDate"])
            await self.update_device_token(device_token or self.device_token)
        except Exception as e:
            _LOGGER.exception("Unexpected response on confirm_authorization: %s", e)
        return res

    def _phone_number(self, country_code: str, phone_number: str) -> Dict[str, int]:
        return {"countryCode": int(country_code), "number": int(phone_number)}

    async def update_token(self) -> Dict[str, Any]:
        if not self.refresh_token:
            return {"error": "No refresh token available", "ok": False, "body": ""}
        _LOGGER.info("Begin refreshToken. Old refresh_expiration=%s now=%s", self.refresh_expiration_date, self._now_utc())
        res = await self._post(
            "/sso-api/Authorization/RefreshToken",
            {"refreshToken": self.refresh_token},
            expect="json",
            need_auth=False,
            retry_on_401=False,
        )
        if isinstance(res, dict) and "error" in res and "status" in res:
            return res
        try:
            self.set_tokens(res["accessToken"], res["refreshToken"], res["refreshExpirationDate"])
            _LOGGER.info("Tokens refreshed. New refresh_expiration=%s", res["refreshExpirationDate"])
            if self.token_update_callback:
                self.token_update_callback(res["accessToken"], res["refreshToken"], res["refreshExpirationDate"])
            return {
                "ok": True,
                "access_token": res["accessToken"],
                "refresh_token": res["refreshToken"],
                "refresh_expiration_date": res["refreshExpirationDate"],
            }
        except Exception as e:
            _LOGGER.exception("Unexpected refresh response: %s", e)
            return {"error": "Unexpected refresh response format", "ok": False, "body": str(res)}

    async def get_user(self) -> Union[Dict[str, Any], str]:
        return await self._post("/sso-api/User/GetUser", need_auth=True, expect="json")

    async def get_username(self):
        user = await self.get_user()
        if user:
            return user.get("userProfile").get("username")

    async def get_paged_keys(self, per_page: int = 100, current_page: int = 1, keys_type: str = "Main"):
        payload = {
            "currentPage": current_page,
            "perPage": per_page,
            "keysType": keys_type,
            "search": None,
        }
        return await self._post("/client-api/Key/GetPagedKeysByKeysType", payload, need_auth=True, expect="json")

    async def get_video_area(self):
        return await self._post(
            "/client-api/VideoCamera/GetVideoArea",
            need_auth=True,
            expect="json",
        )

    async def get_user_video_cameras(self, category: str):
        payload = {"category": category}
        return await self._post(
            "/client-api/VideoCamera/GetUserVideoCameras",
            payload,
            need_auth=True,
            expect="json",
        )

    async def get_user_key(self, key_id: str):
        payload = {"keyId": key_id}
        return await self._post("/client-api/Key/GetUserKey", payload, need_auth=True, expect="json")

    async def open_relay_by_door_id(self, door_id: str):
        payload = {"doorId": door_id}
        res = await self._post("/client-api/Device/OpenRelayByDoorId", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        return {"ok": True, "body": res}

    async def open_relay_by_key_id(self, key_id: str):
        payload = {"keyId": key_id}
        res = await self._post("/client-api/Device/OpenRelayByKeyId", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        return {"ok": True, "body": res}

    async def answer_call_notify(self, call_id: str):
        payload = {"callId": call_id}
        res = await self._post("/communication-api/Call/NotifyCallAnswered", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        _LOGGER.debug("answer_call_notify(%s) -> %s", call_id, res)
        return {"ok": True, "body": res}

    async def fetch_external_bytes(
        self,
        url: str,
        *,
        authorized: bool = True,
        headers: Optional[Dict[str, str]] = None,
        retry_on_401: bool = True,
    ) -> Dict[str, Any]:
        if authorized:
            auth_error = await self._ensure_external_auth()
            if auth_error:
                return auth_error

        session = await self._ensure_external_session()

        def _request():
            request_headers = dict(headers or {})
            if authorized:
                request_headers = self._authorized_external_headers(request_headers)
            return session.get(url, headers=request_headers)

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            body = await resp.read()
            if 200 <= resp.status < 300:
                return {
                    "ok": True,
                    "status": resp.status,
                    "body": body,
                    "content_type": resp.headers.get("Content-Type"),
                }
            return {
                "ok": False,
                "error": f"HTTP {resp.status}",
                "status": resp.status,
                "body": body[:2000].decode("utf-8", "replace"),
            }

        try:
            async with _request() as resp:
                if (
                    resp.status == 401
                    and authorized
                    and retry_on_401
                    and self.refresh_token
                ):
                    _LOGGER.warning(
                        "401 Unauthorized, refreshing token and retrying external GET %s",
                        url,
                    )
                    await self.update_token()
                    async with _request() as retry_resp:
                        return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("External GET failed: %s -> %s", url, err)
            return {"ok": False, "error": str(err), "body": ""}

    def _authorized_external_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        request_headers = dict(headers)
        if self.access_token:
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        return request_headers

    async def _ensure_external_auth(self) -> Optional[Dict[str, Any]]:
        if not self.access_token:
            return {"ok": False, "error": "No access token available", "body": ""}
        await self._ensure_alive()
        return None

    async def create_whep_session(self, whep_url: str, offer_sdp: str) -> Dict[str, Any]:
        auth_error = await self._ensure_external_auth()
        if auth_error:
            return auth_error

        session = await self._ensure_external_session()

        def _request():
            return session.post(
                whep_url,
                data=offer_sdp,
                headers=self._authorized_external_headers(
                    {
                        "Content-Type": "application/sdp",
                        "Accept": "application/sdp",
                    }
                ),
            )

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            answer_sdp = await resp.text()
            if resp.status != 201:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status}",
                    "status": resp.status,
                    "body": answer_sdp[:2000],
                }

            location = resp.headers.get("Location")
            if not location:
                return {
                    "ok": False,
                    "error": "WHEP response did not include a session URL",
                    "status": resp.status,
                    "body": answer_sdp[:2000],
                }

            return {
                "ok": True,
                "status": resp.status,
                "answer_sdp": answer_sdp,
                "location": location,
            }

        try:
            async with _request() as resp:
                if resp.status == 401 and self.refresh_token:
                    _LOGGER.warning("401 Unauthorized, refreshing token and retrying WHEP offer %s", whep_url)
                    await self.update_token()
                    async with _request() as retry_resp:
                        return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("WHEP offer failed: %s -> %s", whep_url, err)
            return {"ok": False, "error": str(err), "body": ""}

    async def send_whep_candidates(self, session_url: str, sdp_fragment: str) -> Dict[str, Any]:
        auth_error = await self._ensure_external_auth()
        if auth_error:
            return auth_error

        session = await self._ensure_external_session()

        def _request():
            return session.patch(
                session_url,
                data=sdp_fragment,
                headers=self._authorized_external_headers(
                    {
                        "Content-Type": "application/trickle-ice-sdpfrag",
                        "If-Match": "*",
                    }
                ),
            )

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            if resp.status in (200, 204):
                return {"ok": True, "status": resp.status}

            body = await resp.text()
            return {
                "ok": False,
                "error": f"HTTP {resp.status}",
                "status": resp.status,
                "body": body[:2000],
            }

        try:
            async with _request() as resp:
                if resp.status == 401 and self.refresh_token:
                    _LOGGER.warning("401 Unauthorized, refreshing token and retrying WHEP candidate %s", session_url)
                    await self.update_token()
                    async with _request() as retry_resp:
                        return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("WHEP candidate failed: %s -> %s", session_url, err)
            return {"ok": False, "error": str(err), "body": ""}

    async def close_whep_session(self, session_url: str) -> Dict[str, Any]:
        auth_error = await self._ensure_external_auth()
        if auth_error:
            return auth_error

        session = await self._ensure_external_session()

        def _request():
            return session.delete(
                session_url,
                headers=self._authorized_external_headers({}),
            )

        async def _handle_response(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
            if resp.status in (200, 204):
                return {"ok": True, "status": resp.status}
            return {
                "ok": False,
                "error": f"HTTP {resp.status}",
                "status": resp.status,
                "body": await resp.text(),
            }

        try:
            async with _request() as resp:
                if resp.status == 401 and self.refresh_token:
                    _LOGGER.warning("401 Unauthorized, refreshing token and retrying WHEP close %s", session_url)
                    await self.update_token()
                    async with _request() as retry_resp:
                        return await _handle_response(retry_resp)

                return await _handle_response(resp)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("WHEP session close failed: %s -> %s", session_url, err)
            return {"ok": False, "error": str(err), "body": ""}

    async def end_call_notify(self, call_id: str):
        payload = {"callId": call_id}
        res = await self._post("/communication-api/Call/NotifyCallEnded", payload, need_auth=True, expect="text")
        if isinstance(res, dict) and "error" in res:
            return res
        _LOGGER.debug("end_call_notify(%s) -> %s", call_id, res)
        return {"ok": True, "body": res}

    async def get_notify_id_token(self) -> Optional[str]:
        res = await self._post("/notificationHub/negotiate?negotiateVersion=1", need_auth=True, expect="json")
        if isinstance(res, dict) and "error" in res and "status" in res:
            _LOGGER.debug("negotiate failed: %s", res)
            return None
        token = res.get("connectionToken")
        _LOGGER.debug("get_notify_id_token -> %s", token)
        return token
