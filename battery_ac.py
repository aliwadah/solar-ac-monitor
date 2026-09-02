"""
Battery -> TCL AC one-shot automation.

Checks the battery state-of-charge (SOC) of your solar system once. If it is
above the configured threshold, it powers on your TCL smart air-conditioner
through the TCL Home cloud.

Run:  python battery_ac.py
Exit: 0 on success, 1 on any error.
"""

import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
import time

import boto3
import httpx

from Crypto.Cipher import AES

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SISELI_USER = os.getenv("SISELI_USER", "")
SISELI_PASSWORD = os.getenv("SISELI_PASSWORD", "")
SISELI_DEVICE_ID = os.getenv("SISELI_DEVICE_ID", "")
BATTERY_THRESHOLD = float(os.getenv("BATTERY_THRESHOLD", "65"))

TCL_USER = os.getenv("TCL_USER", "")
TCL_PASSWORD = os.getenv("TCL_PASSWORD", "")
TCL_AC_NICKNAME = os.getenv("TCL_AC_NICKNAME", "").strip()

VERBOSE = os.getenv("VERBOSE", "true").lower() in ("1", "true", "yes")


def say(msg: str) -> None:
    if VERBOSE:
        print(msg)


# ---------------------------------------------------------------------------
# Part 1 — Siseli battery state of charge
# (IOT-Open signed auth — reverse-engineered from the solar.siseli.com portal
# JS bundle; this is the only auth method the server currently accepts.)
# ---------------------------------------------------------------------------
SISELI_APP_ID = "rBrTRfAPXz"
_SISELI_APP_SECRET_ENC = "I4D0KRr2339z3pQ/at91V9BpFAOe54DaTafwSm6suIQ="


def _decrypt_app_secret(app_id: str, encrypted_b64: str) -> str:
    """AES-128-CBC decrypt the portal's embedded app secret (mirrors qe())."""
    md5_hex = hashlib.md5(app_id.encode("utf-8")).hexdigest()
    key = md5_hex[:16].encode("ascii")
    iv = md5_hex[16:].encode("ascii")
    ciphertext = base64.b64decode(encrypted_b64)
    return AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext).rstrip(b"\x00").decode("utf-8")


def _iot_headers(body_bytes: bytes) -> dict:
    """Build the IOT-Open signed headers required by solar.siseli.com."""
    secret = _decrypt_app_secret(SISELI_APP_ID, _SISELI_APP_SECRET_ENC)
    nonce = os.urandom(16).hex()
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    sign_headers = {
        "IOT-Open-AppID": SISELI_APP_ID,
        "IOT-Open-Body-Hash": body_hash,
        "IOT-Open-Nonce": nonce,
    }
    qs_str = "&".join(f"{k}={sign_headers[k]}" for k in sorted(sign_headers))
    b64_qs = base64.b64encode(qs_str.encode("utf-8")).decode("ascii")
    hmac_bytes = hmac.new(secret.encode("utf-8"), b64_qs.encode("utf-8"), hashlib.sha256).digest()
    sign = hashlib.md5(hmac_bytes).hexdigest()
    return {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Origin": "https://solar.siseli.com",
        "Referer": "https://solar.siseli.com/",
        "IOT-Open-AppID": SISELI_APP_ID,
        "IOT-Open-Nonce": nonce,
        "IOT-Open-Body-Hash": body_hash,
        "IOT-Open-Sign": sign,
    }


def _siseli_login() -> str:
    """Log in to solar.siseli.com and return the access token."""
    if not SISELI_USER or not SISELI_PASSWORD:
        raise RuntimeError("SISELI_USER / SISELI_PASSWORD are not set in .env")
    payload = {
        "account": SISELI_USER,
        "password": hashlib.md5(SISELI_PASSWORD.encode("utf-8")).hexdigest(),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    r = httpx.post(
        "https://solar.siseli.com/apis/login/account",
        content=body,
        headers=_iot_headers(body),
        timeout=30,
    )
    data = r.json()
    if r.status_code != 200 or data.get("code") not in (0, None, "0"):
        raise RuntimeError(f"Siseli login failed: {data.get('message') or data}")
    payload_data = data.get("data") or data
    token = (
        payload_data.get("accessToken")
        or payload_data.get("iotToken")
        or payload_data.get("token")
        or ""
    )
    if not token:
        raise RuntimeError(f"Siseli login returned no token. Keys: {list(payload_data)}")
    return token


def _siseli_battery_soc(access_token: str) -> float:
    """Fetch the current battery capacity (SOC, %) for the device.

    Reads the live device-state snapshot. On this inverter model the SOC is
    exposed under the 'batteryCapacity' attribute (unit '%') — the historical
    'batterySOC' key is always null on this model.
    """
    r = httpx.get(
        "https://solar.siseli.com/apis/deviceState/simple/state/latest/v1",
        params={"deviceId": SISELI_DEVICE_ID, "dataSource": 1},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "IOT-Token": access_token,
            "IOT-Time-Zone": "Etc/UTC",
            "Origin": "https://solar.siseli.com",
            "Referer": "https://solar.siseli.com/",
        },
        timeout=30,
    )
    data = r.json()
    if r.status_code != 200 or data.get("code") not in (0, None):
        raise RuntimeError(f"Siseli device state failed: {data.get('message') or data}")

    fields = (data.get("data") or {}).get("fields") or {}
    for key in ("batteryCapacity", "batterySOC", "batteryStateOfCharge"):
        attr = fields.get(key)
        if attr and attr.get("value") is not None:
            try:
                return float(attr["value"])
            except (TypeError, ValueError):
                continue
    raise RuntimeError(
        "No battery SOC attribute found in device state. "
        f"Available keys: {sorted(fields.keys())}"
    )


def get_battery_soc() -> float:
    """Return the battery state of charge (%) for the configured device."""
    if not SISELI_DEVICE_ID:
        raise RuntimeError("SISELI_DEVICE_ID is not set in .env")
    token = _siseli_login()
    return _siseli_battery_soc(token)


# ---------------------------------------------------------------------------
# Part 2 — TCL Home cloud control
# ---------------------------------------------------------------------------
APP_LOGIN_URL = "https://pa.account.tcl.com/account/login?clientId=54148614"
APP_CLOUD_URL = "https://prod-center.aws.tcljd.com/v3/global/cloud_url_get"
APP_ID = "wx6e1af3fa84fbe523"


def md5_hex(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def sign_request(saas_token: str) -> tuple[str, str, str]:
    """Return (timestamp, nonce, sign) for TCL signed requests."""
    timestamp = str(int(time.time() * 1000))
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    sig = md5_hex(timestamp + nonce + saas_token)
    return timestamp, nonce, sig


def tcl_account_auth(http: httpx.Client) -> dict:
    """Step 0: SSO account login -> token."""
    pw = md5_hex(TCL_PASSWORD)
    payload = {
        "equipment": 2,
        "password": pw,
        "osType": 1,
        "username": TCL_USER,
        "clientVersion": "4.8.1",
        "osVersion": "6.0",
        "deviceModel": "AndroidAndroid SDK built for x86",
        "captchaRule": 2,
        "channel": "app",
    }
    headers = {
        "th_platform": "android",
        "th_version": "4.8.1",
        "th_appbulid": "830",
        "user-agent": "Android",
        "content-type": "application/json; charset=UTF-8",
    }
    r = http.post(APP_LOGIN_URL, json=payload, headers=headers, timeout=20)
    data = r.json()
    if r.status_code != 200 or data.get("status") != 1:
        raise RuntimeError(f"TCL account login failed: {data.get('message') or data}")
    user = data["user"]
    # 'refreshtoken' is lowercase one-word in the API response; 'user.username'
    # (numeric account id) is the SSO identifier used by the follow-up calls.
    return {
        "token": data["token"],
        "refresh_token": data.get("refreshtoken") or data.get("refresh_token", ""),
        "username": user.get("username") or user.get("email"),
        "country_abbr": user.get("country_abbr"),
    }


def tcl_cloud_urls(http: httpx.Client, username: str, token: str) -> dict:
    """Step 1: resolve which regional cloud server to use."""
    payload = {"ssoId": username, "ssoToken": token}
    headers = {"user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = http.post(APP_CLOUD_URL, json=payload, headers=headers, timeout=20)
    data = r.json()
    if r.status_code != 200 or len(data.get("data", {})) == 0:
        raise RuntimeError(f"TCL cloud_url_get failed: {data}")
    d = data["data"]
    return {
        "cloud_region": d.get("cloud_region"),
        "cloud_url": d.get("cloud_url"),
        "sso_url": d.get("sso_url"),
        "device_url": d.get("device_url"),
        "identity_pool_id": d.get("identity_pool_id"),
        "new_struct": d.get("new_struct"),
    }


def tcl_refresh_tokens(http: httpx.Client, cloud_url: str, username: str, access_token: str) -> dict:
    """Step 2: exchange SSO token for SaaS + Cognito tokens."""
    url = f"{cloud_url}/v3/auth/refresh_tokens"
    payload = {"userId": username, "ssoToken": access_token, "appId": APP_ID}
    headers = {"user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = http.post(url, json=payload, headers=headers, timeout=20)
    data = r.json()
    if r.status_code != 200 or not data.get("data"):
        raise RuntimeError(f"TCL refresh_tokens failed: {data}")
    d = data["data"]
    return {
        "saas_token": d.get("saas_token") or d.get("saasToken"),
        "cognito_token": d.get("cognito_token") or d.get("cognitoToken"),
        "cognito_id": d.get("cognito_id") or d.get("cognitoId"),
        "mqtt_endpoint": d.get("mqtt_endpoint") or d.get("mqttEndpoint"),
    }


def tcl_get_things(http: httpx.Client, device_url: str, saas_token: str) -> list[dict]:
    """Step 3: list the devices (ACs) on the account."""
    url = f"{device_url}/v3/user/get_things"
    ts, nonce, sig = sign_request(saas_token)
    headers = {
        "platform": "android",
        "appversion": "5.4.1",
        "thomeversion": "4.8.1",
        "accesstoken": saas_token,
        "accept-language": "en",
        "timestamp": ts,
        "nonce": nonce,
        "sign": sig,
        "user-agent": "Android",
        "content-type": "application/json; charset=UTF-8",
    }
    r = http.post(url, json={}, headers=headers, timeout=20)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"TCL get_things failed: {r.text}")
    return data.get("data") or []


def jwt_sub(jwt_token: str) -> str:
    """Decode (unverified) the 'sub' claim from a JWT cookie/token."""
    import jwt as pyjwt

    try:
        decoded = pyjwt.decode(jwt_token, options={"verify_signature": False})
        return decoded.get("sub", "")
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Could not decode JWT for cognito identity: {e}") from e


def tcl_aws_credentials(aws_region: str | None, cognito_token: str) -> dict:
    """Step 4: get temporary AWS IoT credentials via Cognito identity."""
    if not aws_region:
        raise RuntimeError("TCL did not return 'cloud_region' (AWS region).")
    identity_pool = jwt_sub(cognito_token)
    url = f"https://cognito-identity.{aws_region}.amazonaws.com/"
    payload = {
        "IdentityId": identity_pool,
        "Logins": {"cognito-identity.amazonaws.com": cognito_token},
    }
    headers = {
        "User-Agent": "aws-sdk-android/2.22.6",
        "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
        "content-type": "application/x-amz-json-1.1",
    }
    r = httpx.post(url, json=payload, headers=headers, timeout=20)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"TCL cognito credentials failed: {data}")
    return data


def tcl_power_on(ac_device_id: str, dry_run: bool = False) -> None:
    """Perform the full TCL auth flow and power the AC on.

    With dry_run=True the auth chain runs and the target AC is resolved,
    but no power-on command is published.
    """
    if not TCL_USER or not TCL_PASSWORD:
        raise RuntimeError("TCL_USER / TCL_PASSWORD are not set in .env")

    with httpx.Client() as http:
        auth = tcl_account_auth(http)
        urls = tcl_cloud_urls(http, auth["username"], auth["token"])
        tokens = tcl_refresh_tokens(http, urls["cloud_url"], auth["username"], auth["token"])
        things = tcl_get_things(http, urls["device_url"], tokens["saas_token"])

        if not things:
            raise RuntimeError("No devices found on your TCL Home account.")

        target = ac_device_id
        if not target and TCL_AC_NICKNAME:
            match = next(
                (t for t in things
                 if (t.get("nick_name") or t.get("nickName") or "") == TCL_AC_NICKNAME),
                None,
            )
            if match is None:
                raise RuntimeError(
                    f"No AC named '{TCL_AC_NICKNAME}'. Known: "
                    + ", ".join((t.get("nick_name") or t.get("nickName") or "?") for t in things)
                )
            target = match.get("deviceId") or match["device_id"]

        if not target:
            # default to the first AC-like thing on the account
            target = things[0].get("deviceId") or things[0]["device_id"]
            say(f"INFO: using first device on account id={target}")

        say(f"INFO: target AC resolved -> device id: {target}")

        if dry_run:
            say("DRY-RUN: auth + device resolution OK; no power-on published.")
            return

        aws = tcl_aws_credentials(urls["cloud_region"], tokens["cognito_token"])
        creds = aws["Credentials"]

        iot = boto3.client(
            "iot-data",
            region_name=urls["cloud_region"],
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretKey"],
            aws_session_token=creds["SessionToken"],
        )

        payload = json.dumps({
            "state": {"desired": {"powerSwitch": 1}},
            "clientToken": f"mobile_{int(time.time())}",
        })
        topic = f"$aws/things/{target}/shadow/update"
        iot.publish(topic=topic, qos=1, payload=payload)
        say(f"POWER ON command sent to AC device: {target}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    just_soc = "--just-soc" in sys.argv
    dry_run = "--dry-run" in sys.argv
    if not all([SISELI_USER, SISELI_PASSWORD, SISELI_DEVICE_ID]):
        print("Missing Siseli config. Fill in SISELI_USER / SISELI_PASSWORD / "
              "SISELI_DEVICE_ID in .env")
        return 2

    try:
        soc = get_battery_soc()
        print(f"Battery state of charge: {soc}%  (threshold {BATTERY_THRESHOLD}%)")

        if just_soc:
            print("Dry-run: only the battery was read, no AC action taken.")
            return 0

        if soc >= BATTERY_THRESHOLD:
            print("Battery above threshold -> turning on the AC.")
            tcl_power_on("", dry_run=dry_run)
            if not dry_run:
                print("Done.")
        else:
            print("Battery not above threshold -> AC left off.")
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())