"""
TapTap QR-code login for Phigros.

Obtains a LeanCloud `sessionToken` by the same device-code flow the game's
own TapTap SDK uses, so the player never has to pull `.userdata` off their
phone by hand.

Flow:
    1. request_qrcode()      -> ask TapTap for a device code + QR URL
    2. (player scans it in the TapTap app and confirms)
    3. poll_qrcode()         -> exchange the device code for an access token
    4. fetch_profile()       -> read the TapTap account profile
    5. exchange_for_session()-> trade that for a LeanCloud sessionToken

Only the standard library is used, matching core/cloud_service.py.
"""
import json
import time
import hmac
import base64
import hashlib
import random
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional
from uuid import uuid4

# Phigros' TapTap client credentials (identical to core/cloud_service.py).
CLIENT_ID = "rAK3FfdieFob2Nn8Am"
APP_KEY = "Qr9AEqtuoSVS3zeD6iVbM4ZC0AtkJcQ89tywVyi0"
CLOUD_SERVER = "https://rak3ffdi.cloud.tds1.tapapis.cn"

CHINA_WEB_HOST = "https://accounts.tapapis.cn"
CHINA_API_HOST = "https://open.tapapis.cn"
DEVICE_CODE_URL = CHINA_WEB_HOST + "/oauth2/v1/device/code"
TOKEN_URL = CHINA_WEB_HOST + "/oauth2/v1/token"
TAP_SDK_VERSION = "2.1"

# Device-code states reported by TapTap while we wait.
PENDING_ERRORS = ("authorization_pending", "authorization_waiting", "slow_down")
DENIED_ERRORS = ("access_denied",)
EXPIRED_ERRORS = ("expired_token", "invalid_grant", "device_token_expired")


def _sign_headers(headers: Dict[str, str], add_app_key: bool = False) -> Dict[str, str]:
    """
    Builds the X-LC-Sign header.

    The signature is md5(timestamp [+ app key]) paired with that timestamp;
    the app key is only folded in for the LeanCloud user endpoint.
    """
    timestamp = int(time.time() * 1000)
    payload = f"{timestamp}{APP_KEY}" if add_app_key else str(timestamp)
    digest = hashlib.md5(payload.encode()).hexdigest()
    headers["X-LC-Sign"] = f"{digest},{timestamp}"
    return headers


def _mac_authorization(token: Dict[str, Any], method: str, url: str) -> str:
    """
    Builds the MAC Authorization header TapTap's open API requires.

    The signed string is a fixed newline-separated normalisation of the
    request; the digest algorithm is whatever the token says it is.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    uri = parsed.path + (("?" + parsed.query) if parsed.query else "")

    timestamp = int(time.time())
    nonce = str(random.randint(0, 2147483647))
    normalized = f"{timestamp}\n{nonce}\n{method}\n{uri}\n{host}\n{port}\n\n"

    algorithm = token.get("mac_algorithm", "hmac-sha-1")
    if algorithm == "hmac-sha-256":
        digestmod = hashlib.sha256
    elif algorithm == "hmac-sha-1":
        digestmod = hashlib.sha1
    else:
        raise ValueError(f"不支持的 MAC 算法：{algorithm}")

    mac = hmac.new(token["mac_key"].encode(), normalized.encode(), digestmod)
    signature = base64.b64encode(mac.digest()).decode()
    return (
        'MAC id="%s",ts="%s",nonce="%s",mac="%s"'
        % (token["kid"], timestamp, nonce, signature)
    )


def _request(
    url: str,
    method: str = "GET",
    form: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    add_app_key: bool = False,
    timeout: int = 15,
) -> Dict[str, Any]:
    """Issues one signed request and returns the decoded JSON response."""
    req_headers = _sign_headers(dict(headers or {}), add_app_key)

    body: Optional[bytes] = None
    if json_body is not None:
        req_headers["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode("utf-8")
    elif form is not None:
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        body = urllib.parse.urlencode(
            {k: str(v) for k, v in form.items()}
        ).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        # TapTap reports "not scanned yet" as an HTTP error with a JSON body,
        # so a parseable body is data, not a failure.
        try:
            return json.loads(raw)
        except Exception:
            raise RuntimeError(f"TapTap API 错误 [{e.code}]：{raw}") from e
    except Exception as e:
        raise RuntimeError(f"网络请求失败：{e}") from e


def request_qrcode() -> Dict[str, Any]:
    """
    Asks TapTap for a login QR code.

    Returns the device_code / qrcode_url / interval / expires_in block,
    with the device_id we generated folded in (every later call needs it).
    """
    device_id = uuid4().hex
    # NOTE: `info` is sent as a Python-repr dict here, matching the TapTap
    # SDK's own wire format for this endpoint. The token endpoint below
    # expects real JSON instead; the two genuinely differ.
    result = _request(
        DEVICE_CODE_URL,
        method="POST",
        form={
            "client_id": CLIENT_ID,
            "response_type": "device_code",
            "scope": "public_profile",
            "version": TAP_SDK_VERSION,
            "platform": "unity",
            "info": str({"device_id": device_id}),
        },
    )

    data = result.get("data")
    if not data:
        raise RuntimeError(f"获取登录二维码失败：{result}")
    return {"device_id": device_id, **data}


def poll_qrcode(qr: Dict[str, Any]) -> Dict[str, Any]:
    """
    Checks whether the QR code has been approved yet.

    Returns {"state": ...} where state is one of:
        "pending"  - not scanned / not confirmed yet
        "ok"       - approved; "token" holds the access token
        "denied"   - the player rejected the request
        "expired"  - the code timed out, request a new one
    """
    result = _request(
        TOKEN_URL,
        method="POST",
        form={
            "grant_type": "device_token",
            "client_id": CLIENT_ID,
            "secret_type": "hmac-sha-1",
            "code": qr["device_code"],
            "version": "1.0",
            "platform": "unity",
            "info": json.dumps({"device_id": qr["device_id"]}),
        },
    )

    data = result.get("data")
    if not isinstance(data, dict):
        data = {}

    # TapTap reports "not scanned yet" as a populated `data` block carrying
    # an error, alongside success=false — so the presence of `data` alone
    # does NOT mean approval. Treat it as a token only when the handshake
    # actually succeeded and the MAC credentials are present.
    approved = bool(result.get("success")) and "error" not in data and "mac_key" in data
    if approved:
        return {"state": "ok", "token": data}

    error = str(
        data.get("error")
        or result.get("error")
        or data.get("error_description")
        or result.get("error_description")
        or ""
    ).lower()

    if any(e in error for e in DENIED_ERRORS):
        return {"state": "denied", "message": "你在 TapTap 中拒绝了本次登录请求。"}
    if any(e in error for e in EXPIRED_ERRORS):
        return {"state": "expired", "message": "二维码已过期，请重新获取。"}
    if any(e in error for e in PENDING_ERRORS) or not error:
        return {"state": "pending"}

    # An unrecognised error is still not fatal: report it and let the caller
    # decide whether to keep waiting.
    return {
        "state": "pending",
        "message": data.get("error_description") or result.get("error_description") or error,
    }


def fetch_profile(token: Dict[str, Any]) -> Dict[str, Any]:
    """Reads the TapTap account profile the approved token grants access to."""
    if not token:
        raise ValueError("传入的 token 无效")

    scope = token.get("scope") or ""
    endpoint = "profile" if "public_profile" in scope else "basic-info"
    url = f"{CHINA_API_HOST}/account/{endpoint}/v1?client_id={CLIENT_ID}"

    result = _request(
        url,
        method="GET",
        headers={"Authorization": _mac_authorization(token, "GET", url)},
    )
    data = result.get("data")
    if not data:
        raise RuntimeError(f"获取 TapTap 用户资料失败：{result}")
    return data


def exchange_for_session(profile: Dict[str, Any], token: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trades the TapTap credentials for a LeanCloud user record.

    The returned object carries the `sessionToken` every other cloud call
    in this program authenticates with.
    """
    auth_data = {**profile, **token}
    result = _request(
        f"{CLOUD_SERVER}/1.1/users",
        method="POST",
        json_body={"authData": {"taptap": auth_data}},
        headers={"X-LC-Id": CLIENT_ID},
        add_app_key=True,
    )
    if not result.get("sessionToken"):
        raise RuntimeError(f"换取 sessionToken 失败：{result}")
    return result


def complete_login(qr: Dict[str, Any], token: Dict[str, Any]) -> Dict[str, Any]:
    """Runs the post-approval half of the flow in one step."""
    profile = fetch_profile(token)
    return exchange_for_session(profile, token)


def render_qrcode_svg(url: str, box_size: int = 8, border: int = 2) -> str:
    """
    Renders a QR code as an inline SVG string.

    SVG keeps this dependency-light: it needs no image backend (Pillow),
    and the markup embeds straight into the page.
    """
    import qrcode
    import qrcode.image.svg as qrcode_svg
    import io

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(url)
    qr.make(fit=True)

    buffer = io.BytesIO()
    qr.make_image(image_factory=qrcode_svg.SvgPathImage).save(buffer)
    return buffer.getvalue().decode("utf-8")
