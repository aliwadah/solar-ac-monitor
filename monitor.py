"""
Battery monitor for GitHub Actions (always-on, no PC required).

Each run does ONE pass of the logic:

  * Reads battery SOC (solar.siseli.com).
  * Reads the AC's CURRENT power state (TCL AWS IoT shadow).
  * Applies rules:
      - Manual action (env TCL_ACTION=on/off): turn the AC on/off. For 'on',
        it first checks the battery is >= ON_THRESHOLD.
      - Auto rules on every run:
          - If AC is ON and battery < OFF_THRESHOLD  -> turn AC OFF.
          - If battery < ALERT_THRESHOLD             -> send ntfy notification.

Run headlessly: pass everything via environment variables.
"""

import base64
import hashlib
import hmac
import json
import os
import random
import string
import time

import boto3
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config (from environment / GitHub secrets)
# ---------------------------------------------------------------------------
SISELI_USER = os.getenv("SISELI_USER", "")
SISELI_PASSWORD = os.getenv("SISELI_PASSWORD", "")
SISELI_DEVICE_ID = os.getenv("SISELI_DEVICE_ID", "")

ON_THRESHOLD = float(os.getenv("ON_THRESHOLD", "50"))
OFF_THRESHOLD = float(os.getenv("OFF_THRESHOLD", "50"))
ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "30"))

TCL_USER = os.getenv("TCL_USER", "")
TCL_PASSWORD = os.getenv("TCL_PASSWORD", "")
TCL_AC_NICKNAME = os.getenv("TCL_AC_NICKNAME", "").strip()

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# Manual action for this run: "on", "off", or "" (auto only)
TCL_ACTION = os.getenv("TCL_ACTION", "").strip().lower()

# ---------------------------------------------------------------------------
# ntfy notification
# ---------------------------------------------------------------------------
def send_notification(title, body, priority=3):
    if not NTFY_TOPIC:
        print(f"[ntfy] topic missing; skipped: {title}")
        return
    try:
        r = httpx.post(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": str(priority),
                "Tags": "battery",
            },
            timeout=20,
        )
        print(f"[ntfy] sent: {title} (HTTP {r.status_code})")
    except Exception as e:  # noqa: BLE001
        print(f"[ntfy] failed: {e}")


# ---------------------------------------------------------------------------
# Siseli (battery) -- IOT-Open signed auth
# ---------------------------------------------------------------------------
SISELI_APP_ID = "rBrTRfAPXz"
_SISELI_APP_SECRET_ENC = "I4D0KRr2339z3pQ/at91V9BpFAOe54DaTafwSm6suIQ="


def _decrypt_app_secret(app_id, enc):
    from Crypto.Cipher import AES
    m = hashlib.md5(app_id.encode("utf-8")).hexdigest()
    return AES.new(m[:16].encode("ascii"), AES.MODE_CBC, m[16:].encode("ascii")).decrypt(
        base64.b64decode(enc)).rstrip(b"\x00").decode("utf-8")


def _iot_headers(body_bytes):
    secret = _decrypt_app_secret(SISELI_APP_ID, _SISELI_APP_SECRET_ENC)
    nonce = os.urandom(16).hex()
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    sh = {"IOT-Open-AppID": SISELI_APP_ID, "IOT-Open-Body-Hash": body_hash, "IOT-Open-Nonce": nonce}
    qs = "&".join(f"{k}={sh[k]}" for k in sorted(sh))
    b64 = base64.b64encode(qs.encode("utf-8")).decode("ascii")
    sig = hashlib.md5(hmac.new(secret.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).digest()).hexdigest()
    return {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
            "Origin": "https://solar.siseli.com", "Referer": "https://solar.siseli.com/",
            "IOT-Open-AppID": SISELI_APP_ID, "IOT-Open-Nonce": nonce,
            "IOT-Open-Body-Hash": body_hash, "IOT-Open-Sign": sig}


def siseli_login():
    payload = {"account": SISELI_USER,
               "password": hashlib.md5(SISELI_PASSWORD.encode("utf-8")).hexdigest()}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    r = httpx.post("https://solar.siseli.com/apis/login/account", content=body,
                   headers=_iot_headers(body), timeout=30)
    d = r.json()
    if r.status_code != 200 or d.get("code") not in (0, None, "0"):
        raise RuntimeError(f"Siseli login failed: {d.get('message') or d}")
    data = d.get("data") or d
    return data.get("accessToken") or data.get("iotToken") or data.get("token") or ""


def read_battery_soc():
    token = siseli_login()
    r = httpx.get(
        "https://solar.siseli.com/apis/deviceState/simple/state/latest/v1",
        params={"deviceId": SISELI_DEVICE_ID, "dataSource": 1},
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
                 "IOT-Token": token, "IOT-Time-Zone": "Etc/UTC",
                 "Origin": "https://solar.siseli.com", "Referer": "https://solar.siseli.com/"},
        timeout=30,
    )
    d = r.json()
    if r.status_code != 200 or d.get("code") not in (0, None):
        raise RuntimeError(f"Siseli device state failed: {d.get('message') or d}")
    fields = (d.get("data") or {}).get("fields") or {}
    for key in ("batteryCapacity", "batterySOC", "batteryStateOfCharge"):
        attr = fields.get(key)
        if attr and attr.get("value") is not None:
            try:
                return float(attr["value"])
            except (TypeError, ValueError):
                continue
    raise RuntimeError("No battery SOC attribute found.")


# ---------------------------------------------------------------------------
# TCL (AC) -- auth, shadow read, and control
# ---------------------------------------------------------------------------
APP_LOGIN_URL = "https://pa.account.tcl.com/account/login?clientId=54148614"
APP_CLOUD_URL = "https://prod-center.aws.tcljd.com/v3/global/cloud_url_get"
APP_ID = "wx6e1af3fa84fbe523"


def md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sign_request(saas):
    ts = str(int(time.time() * 1000))
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return ts, nonce, md5_hex(ts + nonce + saas)


def tcl_auth():
    payload = {"equipment": 2, "password": md5_hex(TCL_PASSWORD), "osType": 1, "username": TCL_USER,
               "clientVersion": "4.8.1", "osVersion": "6.0",
               "deviceModel": "AndroidAndroid SDK built for x86", "captchaRule": 2, "channel": "app"}
    headers = {"th_platform": "android", "th_version": "4.8.1", "th_appbulid": "830",
               "user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = httpx.post(APP_LOGIN_URL, json=payload, headers=headers, timeout=20)
    d = r.json()
    if r.status_code != 200 or d.get("status") != 1:
        raise RuntimeError(f"TCL login failed: {d.get('message') or d}")
    user = d["user"]
    return {"token": d["token"], "username": user.get("username") or user.get("email")}


def tcl_cloud_urls(username, token):
    r = httpx.post(APP_CLOUD_URL, json={"ssoId": username, "ssoToken": token},
                   headers={"user-agent": "Android", "content-type": "application/json; charset=UTF-8"}, timeout=20)
    d = r.json()
    if r.status_code != 200 or len(d.get("data", {})) == 0:
        raise RuntimeError(f"TCL cloud_url failed: {d}")
    return d["data"]


def tcl_refresh_tokens(cloud_url, username, token):
    r = httpx.post(f"{cloud_url}/v3/auth/refresh_tokens",
                   json={"userId": username, "ssoToken": token, "appId": APP_ID},
                   headers={"user-agent": "Android", "content-type": "application/json; charset=UTF-8"}, timeout=20)
    d = r.json()
    if r.status_code != 200 or not d.get("data"):
        raise RuntimeError(f"TCL refresh_tokens failed: {d}")
    data = d["data"]
    return {"saas": data.get("saas_token") or data.get("saasToken"),
            "cognito": data.get("cognito_token") or data.get("cognitoToken")}


def tcl_get_things(device_url, saas):
    ts, nonce, sig = sign_request(saas)
    headers = {"platform": "android", "appversion": "5.4.1", "thomeversion": "4.8.1",
               "accesstoken": saas, "accept-language": "en", "timestamp": ts, "nonce": nonce,
               "sign": sig, "user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = httpx.post(f"{device_url}/v3/user/get_things", json={}, headers=headers, timeout=20)
    d = r.json()
    if r.status_code != 200 or d.get("code") != 0:
        raise RuntimeError(f"TCL get_things failed: {d.get('message') or d}")
    return d.get("data") or []


def jwt_sub(jwt_token):
    import jwt as pyjwt
    try:
        return pyjwt.decode(jwt_token, options={"verify_signature": False}).get("sub", "")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not decode JWT: {e}") from e


def tcl_aws_credentials(aws_region, cognito):
    identity_pool = jwt_sub(cognito)
    r = httpx.post(f"https://cognito-identity.{aws_region}.amazonaws.com/",
                   json={"IdentityId": identity_pool,
                         "Logins": {"cognito-identity.amazonaws.com": cognito}},
                   headers={"User-Agent": "aws-sdk-android/2.22.6",
                            "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
                            "content-type": "application/x-amz-json-1.1"}, timeout=20)
    d = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"TCL cognito failed: {d}")
    return d


class Tcl:
    """Resolves the AC and exposes current state + set power."""

    def __init__(self):
        self.ac_id = None
        self.region = None
        self._aws_creds = None

    def _connect(self):
        with httpx.Client() as c:
            auth = tcl_auth()
            urls = tcl_cloud_urls(auth["username"], auth["token"])
            tokens = tcl_refresh_tokens(urls["cloud_url"], auth["username"], auth["token"])
            saas, cognito = tokens["saas"], tokens["cognito"]
            things = tcl_get_things(urls["device_url"], saas)
            if not things:
                raise RuntimeError("No TCL devices.")
            target = None
            for t in things:
                name = t.get("nick_name") or t.get("nickName") or ""
                if TCL_AC_NICKNAME and name == TCL_AC_NICKNAME:
                    target = t
                    break
            if target is None:
                target = things[0]
            self.ac_id = target.get("deviceId") or target.get("device_id")
            aws_region = urls.get("cloud_region") or "ap-southeast-1"
        self.region = aws_region
        # fetch AWS creds (still within cognito scope)
        with httpx.Client() as c:
            auth = tcl_auth()
            urls = tcl_cloud_urls(auth["username"], auth["token"])
            tokens = tcl_refresh_tokens(urls["cloud_url"], auth["username"], auth["token"])
            aws = tcl_aws_credentials(aws_region, tokens["cognito"])
        self._aws_creds = aws["Credentials"]

    def _iot_data(self):
        if self._aws_creds is None:
            self._connect()
        creds = self._aws_creds
        return boto3.client("iot-data", region_name=self.region,
                            aws_access_key_id=creds["AccessKeyId"],
                            aws_secret_access_key=creds["SecretKey"],
                            aws_session_token=creds["SessionToken"])

    def get_power_switch(self):
        """Return True/False/None for the AC's reported powerSwitch."""
        iot = self._iot_data()
        resp = iot.get_thing_shadow(thingName=self.ac_id)
        payload = resp["payload"].read().decode("utf-8")
        shadow = json.loads(payload)
        reported = (shadow.get("state") or {}).get("reported") or {}
        val = reported.get("powerSwitch")
        if val is None:
            delta = (shadow.get("state") or {}).get("delta") or {}
            val = delta.get("powerSwitch")
        if val is None:
            return None
        return int(val) == 1

    def set_power(self, on):
        iot = self._iot_data()
        desired = {"powerSwitch": 1 if on else 0}
        payload = json.dumps({"state": {"desired": desired},
                              "clientToken": f"mobile_{int(time.time())}"})
        topic = f"$aws/things/{self.ac_id}/shadow/update"
        iot.publish(topic=topic, qos=1, payload=payload)
        print(f"[tcl] published {'ON' if on else 'OFF'} to {self.ac_id}")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------
def main():
    # Preflight: required config
    missing = []
    for var in ("SISELI_USER", "SISELI_PASSWORD", "SISELI_DEVICE_ID",
                "TCL_USER", "TCL_PASSWORD"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        print(f"ERROR: missing env: {', '.join(missing)}")
        return 2

    print(f"Action for this run: '{TCL_ACTION}' (blank = auto only)")

    # 1) battery
    soc = read_battery_soc()
    print(f"Battery SOC: {soc:.1f}%")

    # 2) AC state
    tcl = Tcl()
    ac_on = tcl.get_power_switch()
    print(f"AC currently on: {ac_on}")

    notified_low = False

    # 3) low battery alert
    if soc < ALERT_THRESHOLD:
        send_notification("Low battery", f"Battery is {soc:.1f}% (< {ALERT_THRESHOLD:.0f}%).",
                          priority=4)
        notified_low = True

    # 4) manual action
    if TCL_ACTION == "on":
        if soc < ON_THRESHOLD:
            print(f"Turn-on requested but battery {soc:.1f}% < {ON_THRESHOLD:.0f}% -> refused.")
        elif ac_on is True:
            print("AC already on; no action.")
        else:
            tcl.set_power(True)
            send_notification("AC turned ON", f"Battery {soc:.1f}% >= {ON_THRESHOLD:.0f}%.")
    elif TCL_ACTION == "off":
        if ac_on is False:
            print("AC already off; no action.")
        else:
            tcl.set_power(False)
            send_notification("AC turned OFF", "Manual off requested.")

    # 5) auto turn-off
    if not notified_low and ac_on is True and soc < OFF_THRESHOLD:
        print(f"Auto: AC on but battery {soc:.1f}% < {OFF_THRESHOLD:.0f}% -> turning off.")
        tcl.set_power(False)
        send_notification("AC turned OFF automatically",
                          f"Battery reached {soc:.1f}% (< {OFF_THRESHOLD:.0f}%).")

    print("Done.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())