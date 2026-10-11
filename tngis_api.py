"""Direct client for the Tamil Nilam GI Viewer APIs (tngis.tn.gov.in), see API_REFERENCE.md: no browser involved.

* public listings: plain GET calls (districts, taluks, villages, survey numbers)
* secure calls: the body and the reply are AES-256-CBC encrypted with a key derived (PBKDF2-HMAC-SHA512, 1500 rounds) from
  the session key that GET /session-key hands out; works anonymously (captcha, login) and with the access_token cookie
* login: /auth/captcha -> the captcha text -> /auth/login; afterwards /auth/refresh renews the access token without a captcha
"""
import base64, hashlib, json, os, re, time
from pathlib import Path
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PUBLIC = "https://tngis.tn.gov.in/apps/generic_api/v2"
GI_API = "https://tngis.tn.gov.in/apps/gi_viewer_api/gi_mvc/api/v1"
ORIGIN = "https://tngis.tn.gov.in"
REFERER = ORIGIN + "/apps/gi_viewer/map-viewer/index.html"
LOGIN_REFERER = ORIGIN + "/apps/gi_viewer/index.html"
DOMAIN = "tngis.tn.gov.in"
PBKDF2_ITER = 1500
LOCKOUT_MARGIN = 90  # s added to the server's "try again in" so the first retry is not itself refused


class AuthError(Exception):
    """Tokens are dead or the login failed; log in (again)."""


class Locked(Exception):
    """Per-network login lockout ('Too many failed login attempts ... Try again in 4hr 12min'); .seconds = how long to wait."""
    def __init__(self, seconds, msg=""):
        super().__init__(msg); self.seconds = seconds


def _derive(passphrase, salt): return hashlib.pbkdf2_hmac("sha512", passphrase.encode(), salt, PBKDF2_ITER, 32)


def encrypt(text, passphrase):
    salt, iv = os.urandom(16), os.urandom(16)
    pad = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(_derive(passphrase, salt)), modes.CBC(iv)).encryptor()
    ct = enc.update(pad.update(text.encode()) + pad.finalize()) + enc.finalize()
    env = {"ciphertext": base64.b64encode(ct).decode(), "iv": iv.hex(), "salt": salt.hex(), "iterations": PBKDF2_ITER}
    return base64.b64encode(json.dumps(env).encode()).decode()


def decrypt(b64, passphrase):
    env = json.loads(base64.b64decode(b64))
    dec = Cipher(algorithms.AES(_derive(passphrase, bytes.fromhex(env["salt"]))), modes.CBC(bytes.fromhex(env["iv"]))).decryptor()
    unpad = padding.PKCS7(128).unpadder()
    return (unpad.update(dec.update(base64.b64decode(env["ciphertext"])) + dec.finalize()) + unpad.finalize()).decode()


def jwt_exp(token):
    """exp claim (unix seconds) of a JWT, 0 if unreadable."""
    try:
        body = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("exp", 0)
    except Exception:
        return 0


def parse_wait(msg):
    """Seconds named in '... Try again in 4hr 12min' / 'Retry after 2h 23m' / 'Retry after 6h' (0 if none)."""
    m = str(msg).lower()
    t = re.search(r"(?:retry|try again)\s*(?:after|in)\s*(.*)", m)  # not the "per 4h" of "Request limit of 125 per 4h exhausted"
    m = t.group(1) if t else m
    h = re.search(r"(\d+(?:\.\d+)?)\s*h", m); mi = re.search(r"(\d+)\s*m(?:in)?\b", m)
    return int(float(h.group(1)) * 3600 if h else 0) + (int(mi.group(1)) * 60 if mi else 0)


def lockout_seconds(msg):
    """Seconds to wait if msg is a per-network lockout, else 0 (a lockout without a time waits 15 min)."""
    m = str(msg).lower()
    if "too many" not in m and "try again in" not in m: return 0
    return (parse_wait(m) or 900) + LOCKOUT_MARGIN


class Api:
    def __init__(self, proxy=None, token_file=None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
                               "X-Requested-With": "XMLHttpRequest",
                               "Referer": REFERER})  # without a Referer GET /session-key answers 403 "Access denied"
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
            self.s.headers["Connection"] = "close"  # some proxies drop a connection after one request; a fresh one per call is safe
        self.token_file = Path(token_file) if token_file else None
        self.refresh_token, self.exp, self.sess = None, 0, None
        if self.token_file and self.token_file.exists():
            try:
                d = json.loads(self.token_file.read_text())
                self._set_cookie("access_token", d["access_token"]); self._set_cookie("refresh_token", d["refresh_token"])
                self.refresh_token, self.exp = d["refresh_token"], jwt_exp(d["access_token"])
            except Exception:
                pass

    def _set_cookie(self, name, value): self.s.cookies.set(name, value, domain=DOMAIN, path="/")

    def _cookie(self, name):
        vals = [c.value for c in self.s.cookies if c.name == name and c.value]
        return (max(vals, key=jwt_exp) if name == "access_token" else vals[-1]) if vals else None

    def adopt_cookies(self):
        """Take the tokens the server just set (login or refresh) and save them."""
        access = self._cookie("access_token")
        if not access: raise AuthError("the server did not set an access_token cookie")
        self.refresh_token = self._cookie("refresh_token") or self.refresh_token
        self.exp, self.sess = jwt_exp(access), None  # a session key belongs to the access token: fetch a new one
        if self.token_file:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(json.dumps({"access_token": access, "refresh_token": self.refresh_token}))
            os.chmod(self.token_file, 0o600)

    @property
    def has_tokens(self): return bool(self.refresh_token)

    def refresh(self):
        """POST /auth/refresh with the refresh_token cookie; raises AuthError when it is expired or revoked."""
        if not self.refresh_token: raise AuthError("no refresh token")
        r = self.s.post(f"{GI_API}/auth/refresh", json={}, timeout=40, headers={"Origin": ORIGIN, "Referer": REFERER})
        if not r.ok: raise AuthError(f"refresh failed: HTTP {r.status_code}")
        self.adopt_cookies()

    def maybe_refresh(self):
        if self.refresh_token and time.time() > self.exp - 120: self.refresh()

    def _session_key(self):
        if self.sess and time.time() < self.sess["until"]: return self.sess
        r = self.s.get(f"{GI_API}/session-key", timeout=40)
        try:
            d = r.json()
            self.sess = {"key": d["sessionKey"], "id": d["sessionId"], "csrf": d["csrfToken"], "until": time.time() + d.get("expiresIn", 600) - 90}
        except Exception:
            raise AuthError(f"session-key failed (HTTP {r.status_code}): {r.text[:120]}")
        return self.sess

    def public(self, path, **params):
        """GET a listing endpoint. Never gives up: a failure must not look like an empty list, or a whole village/taluk
        would be skipped or saved empty and marked done forever."""
        attempt = 0
        while True:
            try:
                d = self.s.get(f"{PUBLIC}/{path}", params=params, headers={"X-APP-NAME": "demo"}, timeout=40).json()
                if d.get("success") == 1: return d["data"]
                print(f"  api {path}: unexpected response {str(d)[:150]}")
            except Exception as e:
                print(f"  api {path} error: {str(e).splitlines()[0][:120]}")
            attempt += 1
            time.sleep(min(60, 5 * attempt))

    def secure(self, path, params, headers=None, _retry=True):
        """Encrypted POST. Returns (HTTP status, decrypted reply dict)."""
        self.maybe_refresh()
        ss = self._session_key()
        r = self.s.post(f"{GI_API}/{path}", timeout=60, json={"payload": encrypt(json.dumps(params), ss["key"])},
                        headers={"X-Secure-Request": "true", "X-Session-ID": ss["id"], "X-CSRF-Token": ss["csrf"],
                                 "Origin": ORIGIN, "Referer": REFERER, **(headers or {})})
        try:
            j = r.json()
        except ValueError:
            return r.status_code, {"message": f"bad response: {r.text[:100]}"}
        try:
            body = json.loads(decrypt(j["payload"], ss["key"])) if isinstance(j, dict) and "payload" in j else j
        except Exception:
            self.sess = None; return r.status_code, {"message": "could not decrypt the reply (session key changed?)"}
        if not isinstance(body, dict): return r.status_code, {"message": str(body)[:150]}
        if "session" in str(body.get("error", "")).lower(): self.sess = None
        if _retry and self.refresh_token and "token" in str(body.get("error", "")).lower():  # expired mid-run: refresh once
            self.refresh(); return self.secure(path, params, headers, _retry=False)
        return r.status_code, body

    def login(self, identifier, password, read_captcha, attempts=4):
        """Log in like the page does. read_captcha(png_bytes, n) -> text ('' = unreadable). Returns None on success,
        raises Locked on a network lockout, AuthError otherwise (OTP accounts included: no OTP can be typed unattended)."""
        hdr = {"Origin": ORIGIN, "Referer": LOGIN_REFERER}
        self.s.cookies.clear(); self.refresh_token, self.exp, self.sess = None, 0, None
        ms = lambda: int(time.time() * 1000)
        unreadable = submitted = 0
        while submitted < attempts:
            status, cap = self.secure("auth/captcha", {"timestamp": ms()}, headers=hdr)
            if not cap.get("captchaId") or not cap.get("image"):
                m = cap.get("message") or cap.get("error") or str(cap)[:150]
                if lockout_seconds(m): raise Locked(lockout_seconds(m), m)
                raise AuthError(f"no captcha (HTTP {status}): {m}")
            text = read_captcha(base64.b64decode(cap["image"].split(",", 1)[-1]), unreadable + submitted)
            if not text:  # nothing was submitted, so the server counts no failed login
                unreadable += 1
                if unreadable >= 8: raise AuthError("the captcha reader keeps failing")
                continue
            submitted += 1
            status, res = self.secure("auth/login", {
                "identifier": identifier, "password": password, "captchaId": cap["captchaId"],
                "captchaHash": hashlib.sha256((text + cap["captchaId"]).encode()).hexdigest(),
                "loginType": "public", "timestamp": ms()}, headers=hdr)
            if res.get("status") == 2 and res.get("mfaRequired"): raise AuthError("this account asks for an OTP (mfaRequired)")
            if str(res.get("status")) == "1": self.adopt_cookies(); return
            m = res.get("message") or res.get("error") or str(res)[:150]
            if lockout_seconds(m): raise Locked(lockout_seconds(m), m)
            print(f"  login failed (attempt {submitted}/{attempts}): {m}")
        raise AuthError(f"login failed {attempts} times")
