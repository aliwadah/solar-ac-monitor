"""
Solar -> AC control server.

Reads the battery SOC from solar.siseli.com, serves a mobile web UI, and
automates the AC:

  * Button on the page: if battery >= ON_THRESHOLD, turn the AC on.
  * Background monitor (every POLL_INTERVAL sec):
      - if the AC was turned on but battery drops below OFF_THRESHOLD -> turn it off
      - if battery drops below ALERT_THRESHOLD -> send a ntfy.sh notification

Run:  python solar_app.py   (then open http://<this-pc-ip>:8000 on your phone)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import threading
import time

import boto3
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SISELI_USER = os.getenv("SISELI_USER", "")
SISELI_PASSWORD = os.getenv("SISELI_PASSWORD", "")
SISELI_DEVICE_ID = os.getenv("SISELI_DEVICE_ID", "")

ON_THRESHOLD = float(os.getenv("ON_THRESHOLD", "50"))
OFF_THRESHOLD = float(os.getenv("OFF_THRESHOLD", "50"))
ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "30"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

TCL_USER = os.getenv("TCL_USER", "")
TCL_PASSWORD = os.getenv("TCL_PASSWORD", "")
TCL_AC_NICKNAME = os.getenv("TCL_AC_NICKNAME", "").strip()

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

VERBOSE = os.getenv("VERBOSE", "true").lower() in ("1", "true", "yes")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("solar-ac")
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# State (thread-safe via a lock)
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.soc = None            # last battery %
        self.soc_updated = None    # iso time of last read
        self.ac_on = None          # best-known AC state (True/False/None)
        self.last_ac_action = None # what we last told the AC to do ("on"/"off")
        self.last_error = None
        self.last_alert_under_30 = False  # avoid spamming the alert


state = State()


# ---------------------------------------------------------------------------
# ntfy.sh notification
# ---------------------------------------------------------------------------
def send_notification(title: str, body: str, priority: int = 3) -> None:
    if not NTFY_TOPIC:
        log.warning("ntfy topic not set; skipping notification")
        return
    try:
        httpx.post(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": str(priority),
                "Tags": "battery",
                "X-Title": title,
                "Priority: min/2 low/3 default/4 high": "",
            },
            timeout=20,
        )
        log.info("Notification sent: %s", title)
    except Exception as e:  # noqa: BLE001
        log.warning("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# Siseli (battery) API  -- IOT-Open signed auth
# ---------------------------------------------------------------------------
SISELI_APP_ID = os.getenv("SISELI_APP_ID", "rBrTRfAPXz")
_SISELI_APP_SECRET_ENC = os.getenv("SISELI_APP_SECRET_ENC", "I4D0KRr2339z3pQ/at91V9BpFAOe54DaTafwSm6suIQ=")


def _decrypt_app_secret(app_id, enc):
    m = hashlib.md5(app_id.encode("utf-8")).hexdigest()
    from Crypto.Cipher import AES
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


def _siseli_login():
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


def _siseli_battery_soc(token):
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
    raise RuntimeError("No battery SOC attribute found in device state.")


def read_battery_soc():
    token = _siseli_login()
    return _siseli_battery_soc(token)


# ---------------------------------------------------------------------------
# TCL (AC) API
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
    pw = md5_hex(TCL_PASSWORD)
    payload = {"equipment": 2, "password": pw, "osType": 1, "username": TCL_USER,
               "clientVersion": "4.8.1", "osVersion": "6.0",
               "deviceModel": "AndroidAndroid SDK built for x86", "captchaRule": 2, "channel": "app"}
    headers = {"th_platform": "android", "th_version": "4.8.1", "th_appbulid": "830",
               "user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = httpx.post(APP_LOGIN_URL, json=payload, headers=headers, timeout=20)
    d = r.json()
    if r.status_code != 200 or d.get("status") != 1:
        raise RuntimeError(f"TCL login failed: {d.get('message') or d}")
    user = d["user"]
    return {"token": d["token"], "username": user.get("username") or user.get("email"),
            "country_abbr": user.get("country_abbr")}


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


class TclClient:
    """Resolves the TCL account + AC device, then publishes shadow commands."""

    def __init__(self):
        self.ac_id = None
        self.region = None

    def _resolve(self):
        """Authenticate and remember the AC device id + AWS region."""
        if self.ac_id:
            return
        auth = tcl_auth()
        urls = tcl_cloud_urls(auth["username"], auth["token"])
        things = tcl_get_things(urls["device_url"], tcl_refresh_tokens(urls["cloud_url"], auth["username"], auth["token"])["saas"])
        if not things:
            raise RuntimeError("No AC devices found on TCL account.")
        target = None
        for t in things:
            name = t.get("nick_name") or t.get("nickName") or ""
            if TCL_AC_NICKNAME and name == TCL_AC_NICKNAME:
                target = t
                break
        if target is None:
            target = things[0]
        self.ac_id = target.get("deviceId") or target.get("device_id")
        self.region = urls.get("cloud_region") or "ap-southeast-1"

    def set_power(self, on: bool):
        self._resolve()
        with httpx.Client() as http:
            auth = tcl_auth()
            urls = tcl_cloud_urls(auth["username"], auth["token"])
            tokens = tcl_refresh_tokens(urls["cloud_url"], auth["username"], auth["token"])
            saas = tokens["saas"]
            cognito = tokens["cognito"]
            if not saas or not cognito:
                raise RuntimeError("Could not obtain SaaS/Cognito tokens.")
            # region for cognito + iot
            aws_region = urls.get("cloud_region") or self.region
            identity_pool = jwt_sub(cognito)
            creds_resp = httpx.post(
                f"https://cognito-identity.{aws_region}.amazonaws.com/",
                json={"IdentityId": identity_pool,
                      "Logins": {"cognito-identity.amazonaws.com": cognito}},
                headers={"User-Agent": "aws-sdk-android/2.22.6",
                         "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
                         "content-type": "application/x-amz-json-1.1"}, timeout=20)
            aws = creds_resp.json()
            if creds_resp.status_code != 200:
                raise RuntimeError(f"TCL cognito failed: {aws}")
            creds = aws["Credentials"]

        iot = boto3.client("iot-data", region_name=aws_region,
                           aws_access_key_id=creds["AccessKeyId"],
                           aws_secret_access_key=creds["SecretKey"],
                           aws_session_token=creds["SessionToken"])
        desired = {"powerSwitch": 1 if on else 0}
        payload = json.dumps({"state": {"desired": desired},
                              "clientToken": f"mobile_{int(time.time())}"})
        topic = f"$aws/things/{self.ac_id}/shadow/update"
        iot.publish(topic=topic, qos=1, payload=payload)
        log.info("Sent AC %s (%s)", "ON" if on else "OFF", self.ac_id)


# ---------------------------------------------------------------------------
# Background monitor
# ---------------------------------------------------------------------------
def monitor_loop():
    log.info("Monitor started (poll every %ss)", POLL_INTERVAL)
    while True:
        try:
            soc = read_battery_soc()
            with state.lock:
                state.soc = soc
                state.soc_updated = time.strftime("%Y-%m-%d %H:%M:%S")
                state.last_error = None

            # low battery notification (once per crossing)
            if soc < ALERT_THRESHOLD:
                if not state.last_alert_under_30:
                    send_notification(f"Low battery: {soc:.0f}%",
                                      f"Battery dropped below {ALERT_THRESHOLD:.0f}% ({soc:.1f}%). "
                                      "Consider conserving power.")
                    state.last_alert_under_30 = True
            else:
                state.last_alert_under_30 = False

            # auto turn OFF if AC was on and battery dropped
            if state.ac_on is True and soc < OFF_THRESHOLD:
                log.info("Battery %.1f%% below %.0f%% -> turning AC off", soc, OFF_THRESHOLD)
                TclClient().set_power(False)
                state.ac_on = False
                state.last_ac_action = "off"
                send_notification("AC turned off automatically",
                                  f"Battery reached {soc:.1f}% (< {OFF_THRESHOLD:.0f}%), so the AC was stopped.")

        except Exception as e:  # noqa: BLE001
            with state.lock:
                state.last_error = f"{e}"
            log.warning("monitor error: %s", e)
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Flask app / UI
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(app.root_path, "index.html")


@app.route("/api/status")
def api_status():
    soc = state.soc
    ac_on = state.ac_on
    ac_online = None
    err = state.last_error
    try:
        soc = read_battery_soc()
        with state.lock:
            state.soc = soc
            state.soc_updated = time.strftime("%Y-%m-%d %H:%M:%S")
            state.last_error = None
    except Exception as e:  # noqa: BLE001
        err = f"{e}"
        with state.lock:
            state.last_error = err
    with state.lock:
        return jsonify({
            "soc": soc,
            "soc_updated": state.soc_updated,
            "ac_on": ac_on,
            "ac_id": None,
            "last_ac_action": state.last_ac_action,
            "error": err,
            "on_threshold": ON_THRESHOLD,
            "off_threshold": OFF_THRESHOLD,
            "alert_threshold": ALERT_THRESHOLD,
            "ac_online": ac_online,
        })


@app.route("/api/turn_on", methods=["POST"])
def api_turn_on():
    """Button: check battery; if >= ON_THRESHOLD turn AC on."""
    try:
        soc = read_battery_soc()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"Battery read failed: {e}"}), 500
    with state.lock:
        state.soc = soc
        state.soc_updated = time.strftime("%Y-%m-%d %H:%M:%S")
    if soc < ON_THRESHOLD:
        return jsonify({"ok": False, "message": f"Battery {soc:.0f}% is below {ON_THRESHOLD:.0f}% — AC not turned on."})
    try:
        TclClient().set_power(True)
        with state.lock:
            state.ac_on = True
            state.last_ac_action = "on"
        return jsonify({"ok": True, "message": f"Battery {soc:.0f}% >= {ON_THRESHOLD:.0f}% — AC turned ON."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"AC control failed: {e}"}), 500


@app.route("/api/turn_off", methods=["POST"])
def api_turn_off():
    try:
        TclClient().set_power(False)
        with state.lock:
            state.ac_on = False
            state.last_ac_action = "off"
        return jsonify({"ok": True, "message": "AC turned OFF."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"AC control failed: {e}"}), 500


def start_monitor_if_needed():
    # Guard against starting multiple loops under gunicorn's multiple workers.
    if os.getenv("START_MONITOR", "true").lower() in ("1", "true", "yes"):
        with getattr(state, "monitor_lock", None) or threading.Lock():
            if not getattr(state, "monitor_started", False):
                threading.Thread(target=monitor_loop, daemon=True).start()
                state.monitor_started = True


start_monitor_if_needed()


if __name__ == "__main__":
    import socket
    host = socket.gethostbyname(socket.gethostname())
    log.info("Open on your phone: http://%s:8000", host)
    serve(app, host="0.0.0.0", port=8000, threads=8)