"""Scrape Rural District > Taluk > Village > Survey Number > Sub Division from Tamil Nilam GI Viewer.

Usage:
    pip install playwright openpyxl && playwright install chromium   (or uses /usr/bin/google-chrome)
    python scrape.py                         # everything (very slow if rate limit is on)
    python scrape.py --district Ariyalur     # one district
    python scrape.py --district Ariyalur --taluk Andimadam --village Aiyyur
    python scrape.py --worker 1/3            # one of 3 parallel workers (see below)

Parallel workers: --worker k/N gives this process the accounts k, k+N, k+2N, ... and, inside every taluk, the villages
whose position in the listing is k, k+N, ... so N workers never share an account or a workbook. Each worker keeps its
own account pointer in out/.progress/account_idx[.k]. The accounts you have decide how many workers are useful:
each worker burns ~8 accounts/hour and an account rests 3 h after its 50 calls, so ~24 accounts per worker.
Exhausted accounts are remembered (with the server's "retry after" time) and skipped without a login; if every
account of a worker is cooling down it sleeps until the first one is free again.
A workbook that another process saved in the last few minutes is left alone and revisited later, so a stray
duplicate worker cannot corrupt a village.

Login: the script fills mobile/password from accounts.json, waits for the captcha canvas to render, saves it as
captcha.png (captcha.<k>.png per worker),
and asks you to type the captcha in the terminal.
When an account's rate limit is used up it logs in with the next account in accounts.json
(new captcha each time) and continues; the account index is remembered in out/.progress/account_idx.

Output: out/Village_map_of_<District>_<Taluk>_<Village>_<lat>_<long>.xlsx   (columns: surveynumber | sub-divisions)
        lat/lon = centre of the village boundary's bounding box, i.e. the point the site's map centres on when the
        village is selected (same /land/get-geom call + fitBounds the site does). One call per village.
        "-" in the sub-divisions column means the server reported no sub-divisions for that survey number.
Resumable / crash-safe, Excel only (no side files): the workbook is created with every survey number as soon as the
        village starts and is re-saved (atomically) after every fetched row. A blank sub-divisions cell = not fetched
        yet, so on restart the script opens the workbook and continues with the blank rows. A village is finished when
        no cell is blank. The only other state is out/.progress/account_idx (which account is in use).
        out/ is committed to git: scripts/sync_progress.sh (pm2 land-progress-sync, every 10 min) pushes it, so a fresh clone
        on another machine (scripts/setup.sh) resumes exactly where the last machine stopped. Run one machine at a time.
        Listing APIs never give up (retry + re-login), transient check-areg errors are retried (never stored as
        "no sub-divisions"), and any unexpected exception restarts the browser and resumes from the same row.
        Older files without lat/lon in the name are backfilled: the centre is fetched once and the file is renamed.
Captcha: read by the Qwen3-VL HTTP API fleet (env CAPTCHA_API_URLS = comma-separated base URLs such as
http://192.168.52.115:8091,http://192.168.52.115:8093 ; bearer token from env CAPTCHA_API_TOKEN, or read at start-up from
CAPTCHA_API_TOKEN_FILE = the API's own .env with VLM_API_TOKENS=...). Each request POSTs
{"images_base64": [<png>], "prompt": CAPTCHA_PROMPT, "max_new_tokens": 16, "temperature": 0} to <url>/v1/describe/base64
and takes the "text" field. Worker k starts at API k and consecutive attempts rotate through the list; if one API is down the
others are tried in the same attempt. An answer whose length != CAPTCHA_LEN (default 6, 0 = no check) is discarded and a fresh
captcha is requested instead of submitting a known-bad guess. If CAPTCHA_API_URLS is empty the old Ollama path is used
(env CAPTCHA_MODEL / OLLAMA_URL, comma-separated models alternate per attempt).
A wrong captcha is simply retried with a fresh captcha on the SAME account, forever, until the login succeeds
(manual input is only asked for if the reader returns nothing AND you are in a real terminal).
MAX_LOGIN_TRIES (env, default 0 = unlimited) can cap the retries per account, after which the account is skipped.
Two server-side limits are honoured:
  * 50 check-areg calls per account per hour: when `remaining` reaches 0 the script switches account; only after
    every account has been used does it wait for the window to reset.
  * a burst detector ("Unusual request pattern detected ... slow down", HTTP 429) that trips when calls come faster
    than roughly one every few seconds. Measured: 1 call / 5 s never trips it. Default --delay is 4 s; on a 429 the
    script waits BURST_WAIT and increases the delay by 1 s (up to 10 s) for the rest of the run.
Proxy: PROXY_URL (env, http://user:pass@host:port) sends ALL browser traffic to the site through that proxy; the captcha
API calls (LAN) stay direct. The exit IP is printed at every login. With a rotating proxy (PROXY_ROTATION_S, default
120 s) a per-network login lockout is only waited out for one rotation + margin instead of the hours the server asks for.
"""
import argparse, base64, json, os, re, sys, time, traceback, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, Error as PWError
from openpyxl import Workbook, load_workbook

HERE = Path(__file__).parent
for _line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:  # .env = defaults only
    if _line.strip() and not _line.lstrip().startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1); os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))
URL = "https://tngis.tn.gov.in/apps/gi_viewer/map-viewer/index.html"
API = "https://tngis.tn.gov.in/apps/generic_api/v2/"
CHROME = "/usr/bin/google-chrome"

ap = argparse.ArgumentParser()
ap.add_argument("--district"); ap.add_argument("--taluk"); ap.add_argument("--village")
ap.add_argument("--out", default=str(HERE / "out"))
ap.add_argument("--delay", type=float, default=4.0, help="seconds between sub-division calls (burst limiter trips below ~3s)")
ap.add_argument("--worker", default="0/1", help="k/N: this is worker k of N parallel workers (accounts and villages are split)")
args = ap.parse_args()
OUT = Path(args.out); (OUT / ".progress").mkdir(parents=True, exist_ok=True)


def safe(s): return re.sub(r"[^\w.-]+", "_", s.strip()).strip("_")
def match(name, q): return not q or q.lower() in name.lower()


ACCOUNTS = json.load(open(HERE / "accounts.json"))
WK, NWK = (int(x) for x in args.worker.split("/"))
assert 0 <= WK < NWK, "--worker must be k/N with 0 <= k < N"
ACCOUNT_POOL = list(range(len(ACCOUNTS)))[WK::NWK]
if WK: ACCOUNT_POOL.reverse()  # extra workers start from the far end of their share, away from where a lone run left off
assert ACCOUNT_POOL, "more workers than accounts"
IDX_FILE = OUT / ".progress" / ("account_idx" if WK == 0 else f"account_idx.{WK}")
ACTIVE_WINDOW = 180  # s: a workbook saved more recently than this is being written by another process
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VLM_MODELS = [m.strip() for m in os.environ.get("CAPTCHA_MODEL", "qwen2.5vl:7b").split(",") if m.strip()]
# Qwen3-VL API fleet (vlm-api on :8091/:8093/:8094). Base URLs; "/v1/describe/base64" is appended unless already present.
CAPTCHA_APIS = [u.strip().rstrip("/") for u in os.environ.get("CAPTCHA_API_URLS", "").split(",") if u.strip()]


def _captcha_token():
    """CAPTCHA_API_TOKEN, else the first token of VLM_API_TOKENS=... in CAPTCHA_API_TOKEN_FILE (the API's own .env)."""
    if os.environ.get("CAPTCHA_API_TOKEN"): return os.environ["CAPTCHA_API_TOKEN"]
    f = os.environ.get("CAPTCHA_API_TOKEN_FILE")
    if f and os.path.exists(f):
        for line in Path(f).read_text().splitlines():
            if line.startswith("VLM_API_TOKENS="): return line.split("=", 1)[1].split(",")[0].strip()
        return Path(f).read_text().strip().split(",")[0]  # plain token file
    return ""


CAPTCHA_API_TOKEN = _captcha_token()
CAPTCHA_LEN = int(os.environ.get("CAPTCHA_LEN", "6"))  # expected captcha length; 0 disables the check
CAPTCHA_PROMPT = os.environ.get("CAPTCHA_PROMPT", "This is a CAPTCHA image containing exactly 6 characters (letters and digits, "
                                "case-sensitive) with distracting lines. Reply with ONLY the 6 characters, nothing else.")
CAPTCHA_API_TIMEOUT = int(os.environ.get("CAPTCHA_API_TIMEOUT", "60"))  # s; cold model load is ~2-3 s, a queued GPU can be slower
MAX_LOGIN_TRIES = int(os.environ.get("MAX_LOGIN_TRIES", "0"))  # 0 = retry the same account forever
PROXY_URL = os.environ.get("PROXY_URL", "").strip()  # http://user:pass@host:port -> every browser request goes through it
PROXY_ROTATION_S = int(os.environ.get("PROXY_ROTATION_S", "120"))  # the proxy's auto IP rotation period
IP_ECHO = "https://api.ipify.org"  # fetched through the browser after each login page load to log the exit IP


def proxy_settings():
    """Playwright proxy dict from PROXY_URL (credentials split out: chromium needs them separately), or None."""
    if not PROXY_URL: return None
    from urllib.parse import urlparse, unquote
    u = urlparse(PROXY_URL if "://" in PROXY_URL else "http://" + PROXY_URL)
    px = {"server": f"{u.scheme}://{u.hostname}:{u.port or 80}", "bypass": "localhost,127.0.0.1"}
    if u.username: px["username"] = unquote(u.username); px["password"] = unquote(u.password or "")
    return px


PROXY = proxy_settings()
LOGIN_RETRY_DELAY = 3  # seconds between captcha attempts on the same account
PAGE_TIMEOUT = 120_000  # ms to wait for the (often slow) site to show the login form
SLOW_SITE_WAIT = 20  # seconds to wait before retrying when the page did not load
CRASH_WAIT = 30  # seconds before relaunching the browser after an unexpected exception
BURST_WAIT = 60  # seconds to pause after a 429 "unusual request pattern" (block was measured to clear within a minute)
MAX_DELAY = 10.0
# check-areg messages that mean "try again / log in again", NOT "this survey number has no sub-divisions"
TRANSIENT = ("session", "login", "unauthor", "not ready", "decrypt", "expired", "token", "failed to check")


def save_wb(wb, f):
    """Atomic save: a crash mid-write can never leave a truncated workbook behind."""
    tmp = f.with_name(f"{f.stem}.{os.getpid()}.tmp.xlsx"); wb.save(tmp); os.replace(tmp, f)  # pid: two processes never share a tmp
COOLDOWN_H = 3.0  # hours an account rests after its 50 calls (server says "Retry after 3h"); parsed from the message when present
INTERACTIVE = sys.stdin.isatty() and not os.environ.get("NO_MANUAL_CAPTCHA")


def read_captcha_api(path, attempt=0):
    """Ask the Qwen3-VL API fleet for the captcha text. Returns '' if every API failed or the answer looks wrong."""
    img = base64.b64encode(Path(path).read_bytes()).decode()
    body = json.dumps({"images_base64": [img], "prompt": CAPTCHA_PROMPT, "max_new_tokens": 16, "temperature": 0}).encode()
    hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {CAPTCHA_API_TOKEN}"}
    start = (WK + attempt) % len(CAPTCHA_APIS)  # worker k prefers API k; later attempts rotate
    for i in range(len(CAPTCHA_APIS)):
        base = CAPTCHA_APIS[(start + i) % len(CAPTCHA_APIS)]
        url = base if base.endswith("/describe/base64") else base + "/v1/describe/base64"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, hdr), timeout=CAPTCHA_API_TIMEOUT) as r:
                d = json.load(r)
            txt = re.sub(r"\W+", "", str(d.get("text", "")))
            if CAPTCHA_LEN and len(txt) != CAPTCHA_LEN:
                print(f"  captcha API {base}: answer {txt!r} is not {CAPTCHA_LEN} chars; asking for a fresh captcha"); return ""
            return txt
        except Exception as e:
            print(f"  captcha API {base} error: {str(e).splitlines()[0][:120]}")
    return ""


def read_captcha(path, attempt=0):
    """Captcha text via the API fleet (CAPTCHA_API_URLS) or, if none is configured, a local Ollama model. '' = unreadable."""
    if CAPTCHA_APIS: return read_captcha_api(path, attempt)
    model = VLM_MODELS[attempt % len(VLM_MODELS)]
    try:
        img = base64.b64encode(Path(path).read_bytes()).decode()
        body = json.dumps({"model": model, "stream": False, "options": {"temperature": 0}, "messages": [{
            "role": "user", "images": [img],
            "content": "Read the captcha text in this image exactly (case-sensitive). Reply with ONLY the characters."}]}).encode()
        req = urllib.request.Request(f"{OLLAMA}/api/chat", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return re.sub(r"\W+", "", json.load(r)["message"]["content"])
    except Exception as e:
        print("  VLM captcha error:", e); return ""


CAPTCHA_FILE = HERE / (f"captcha.{WK}.png" if NWK > 1 else "captcha.png")  # one file per worker: no cross-worker mix-ups
CAPTCHA_RENDER_TIMEOUT = 25  # s to wait for the captcha canvas to be drawn (one refresh click halfway)
# dark pixels on the captcha canvas + its opacity (loadCaptcha dims it to 0.4 while /auth/captcha is in flight)
CAPTCHA_INK_JS = """()=>{const c=document.querySelector('#publicCaptchaImage'); if(!c) return null;
  const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data; let n=0;
  for(let i=0;i<d.length;i+=4){ if(d[i]<120&&d[i+1]<120&&d[i+2]<120) n++; }
  return {ink:n, opacity:parseFloat(getComputedStyle(c).opacity)}}"""


def wait_captcha(pg, timeout=CAPTCHA_RENDER_TIMEOUT):
    """True once the captcha canvas is drawn: opacity back to 1, a plausible amount of ink, and unchanged between two polls.
    Clicks the refresh button once if nothing has appeared after half the timeout."""
    t0, last, clicked = time.time(), None, False
    while time.time() - t0 < timeout:
        try: st = pg.evaluate(CAPTCHA_INK_JS)
        except PWError: st = None
        ink = (st or {}).get("ink", 0)
        if st and st["opacity"] >= 0.99 and 300 <= ink <= 20000 and ink == last: return True
        last = ink
        if not clicked and time.time() - t0 > timeout / 2:
            clicked = True
            try: pg.click("#publicRefreshCaptcha", timeout=2000); print("  captcha canvas still blank; clicked refresh")
            except PWError: pass
        time.sleep(0.4)
    return False


def login_error(pg):
    """Text of the login form's error box ('' if hidden)."""
    try:
        return pg.evaluate("()=>{const e=document.querySelector('#publicLoginError');"
                           "return e && !e.classList.contains('d-none') ? e.innerText.trim() : ''}") or ""
    except PWError: return ""


LOCKOUT_MARGIN = 90  # s added to the server's "try again in" so the first retry is not itself refused


def lockout_seconds(msg):
    """Seconds to wait if msg is a per-network lockout ('... Try again in 4hr 12min.'), else 0.
    A lockout without a parsable time waits 15 min. Behind a rotating proxy the lockout is per exit IP, so only one
    rotation (+ margin) is waited: the next attempt comes from a new IP."""
    m = msg.lower()
    if "too many" not in m and "try again in" not in m: return 0
    if PROXY: return PROXY_ROTATION_S + 30
    t = re.search(r"try again in\s*(?:(\d+)\s*h(?:ou)?rs?)?\s*(?:(\d+)\s*min)?", m)
    h, mi = (int(t.group(1) or 0), int(t.group(2) or 0)) if t else (0, 0)
    return (h * 3600 + mi * 60 or 900) + LOCKOUT_MARGIN


def exit_ip(ctx):
    """Public IP the browser's requests arrive from (via the proxy when one is set), or '?' if the echo failed."""
    pg = ctx.new_page()
    try:
        pg.goto(IP_ECHO, wait_until="domcontentloaded", timeout=30_000); return pg.inner_text("body").strip()[:45] or "?"
    except PWError as e: return f"? ({str(e).splitlines()[0][:60]})"
    finally: pg.close()


class Session:
    """Holds the current browser context/page and the account it is logged in with."""
    def __init__(self, browser):
        self.b, self.ctx, self.pg = browser, None, None
        self.pool, self.pos, self.exhausted = ACCOUNT_POOL, 0, {}  # exhausted: global account idx -> epoch when usable again
        try: saved = int(IDX_FILE.read_text()) if IDX_FILE.exists() else self.pool[0]
        except ValueError: saved = self.pool[0]
        # resume at the saved account, or (after a lone run is split into workers) at the next one of ours past it
        self.pos = self.pool.index(saved) if saved in self.pool else next((i for i, g in enumerate(self.pool) if g > saved), 0)
        self.delay = args.delay  # seconds between check-areg calls; grows when the burst limiter trips

    @property
    def idx(self): return self.pool[self.pos]  # global index into ACCOUNTS

    def skip_account(self, why):
        """Give up on the current account for this login round and move to the next one."""
        self.skipped += 1
        print(f"  skipping account #{self.idx}: {why}")
        if self.skipped >= len(self.pool):
            print(f"  could not log in with any of this worker's {len(self.pool)} accounts, waiting 10 min")
            time.sleep(600); self.skipped = 0
        self.pos = (self.pos + 1) % len(self.pool)

    def login(self):
        fails = 0
        self.skipped = 0
        while True:
            acc = ACCOUNTS[self.idx]
            if self.ctx: self.ctx.close()
            self.ctx = self.b.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=3)
            self.pg = pg = self.ctx.new_page()
            print(f"Logging in with account #{self.idx} ({acc['mobile']})" + (f" via proxy, exit IP {exit_ip(self.ctx)}" if PROXY else ""))
            try:
                # the site is often slow and a map page never goes network-idle; wait for the login UI instead
                pg.goto(URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                pg.get_by_text("Existing User").click(timeout=PAGE_TIMEOUT)
                pg.fill("#publicIdentifier", acc["mobile"]); pg.fill("#publicPassword", acc["password"])
                # the captcha is a <canvas> filled asynchronously from /auth/captcha; a screenshot taken too early is blank
                if not wait_captcha(pg):
                    print(f"  captcha never rendered; reloading the page in {SLOW_SITE_WAIT}s"); time.sleep(SLOW_SITE_WAIT); continue
                pg.locator("#publicCaptchaImage").screenshot(path=str(CAPTCHA_FILE))  # just the canvas, no refresh button
            except PWError as e:
                print(f"  page not ready ({str(e).splitlines()[0][:120]}); site slow? retrying in {SLOW_SITE_WAIT}s")
                time.sleep(SLOW_SITE_WAIT); continue
            cap = read_captcha(CAPTCHA_FILE, fails)
            if cap: print(f"  VLM captcha: {cap} (attempt {fails + 1})")
            elif INTERACTIVE:
                try: cap = input("Open captcha.png, type the captcha text (blank to retry with a new captcha): ").strip()
                except EOFError: cap = ""
            if cap:
                try:
                    pg.fill("#publicCaptchaInput", cap)
                    pg.click("#publicLoginBtn")
                    pg.wait_for_timeout(8000)
                except PWError as e:
                    print(f"  login page error ({str(e).splitlines()[0][:120]}); retrying"); time.sleep(SLOW_SITE_WAIT); continue
                if not pg.query_selector("#publicLoginBtn"):
                    IDX_FILE.write_text(str(self.idx))
                    print(f"Logged in (after {fails + 1} attempt(s)).")
                    return
                err = login_error(pg)
                lock = lockout_seconds(err)
                if lock:  # "Too many failed login attempts from this network. Try again in 4hr 12min." (HTTP 429, per IP)
                    print(f"  network lockout: {err!r}; sleeping {lock / 60:.0f} min before the next attempt")
                    time.sleep(lock); continue
                print(f"  login failed ({err or 'no error shown, wrong captcha?'}), retrying same account with a new captcha")
            else:
                print("  captcha unreadable (reader returned nothing / rejected the answer), retrying with a new captcha")
            fails += 1
            if MAX_LOGIN_TRIES and fails >= MAX_LOGIN_TRIES:
                self.skip_account(f"{fails} failed logins"); fails = 0; continue
            time.sleep(LOGIN_RETRY_DELAY); continue

    def next_account(self, hours=COOLDOWN_H):
        """Current account hit its quota: remember when it frees up, move to the next usable one, log in."""
        self.exhausted[self.idx] = time.time() + hours * 3600
        for _ in range(len(self.pool)):
            self.pos = (self.pos + 1) % len(self.pool)
            if self.exhausted.get(self.idx, 0) <= time.time(): break
        else:  # every account of this worker is cooling down: sleep until the first one is free
            self.pos = min(range(len(self.pool)), key=lambda i: self.exhausted[self.pool[i]])
            wait = max(0, self.exhausted[self.idx] - time.time()) + 15
            print(f"  all {len(self.pool)} accounts of this worker are cooling down; sleeping {wait / 60:.0f} min until #{self.idx} is free")
            time.sleep(wait)
        self.login()


def api(S, path, **params):
    """GET a listing endpoint (districts/taluks/villages/survey numbers). Never gives up: a failure here must not
    look like an empty list, or a whole village/taluk would be skipped or saved empty and marked done forever."""
    q = "&".join(f"{k}={v}" for k, v in params.items())
    attempt = 0
    while True:
        try:
            txt = S.pg.evaluate("u=>fetch(u,{headers:{'x-app-name':'demo','x-requested-with':'XMLHttpRequest'}}).then(r=>r.text())", f"{API}{path}?{q}")
            d = json.loads(txt)
            if d.get("success") == 1: return d["data"]
            print(f"  api {path}: unexpected response {str(d)[:150]}")
        except Exception as e:
            print(f"  api {path} error: {str(e).splitlines()[0][:120]}")
        attempt += 1
        if attempt % 5 == 0:
            print("  api keeps failing; logging in again"); S.login()
        time.sleep(min(60, 5 * attempt))


SEEN_MSGS = set()

# The same request the page's fetchGeometry()/postGetGeom() make when a village is selected; returns the centre of the
# boundary's bounding box, which is where map.fitBounds() puts the centre of the screen.
GEOM_JS = """async a=>{
  if (_mapSessionPromise) await _mapSessionPromise;
  if (!_mapSessionKey || !_mapSessionId || !_mapCsrfToken) return JSON.stringify({ok:false,message:'session not ready'});
  const payload={case:'revenue_village',code_type:'revenue',district_code:a[0],taluk_code:a[1],village_code:a[2],
                 survey_number:null,sub_division_number:null};
  const enc=landEncryption.encrypt(JSON.stringify(payload),_mapSessionKey);
  const r=await fetch(`${GI_API_BASE}/land/get-geom`,{method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json','X-Secure-Request':'true','X-Session-ID':_mapSessionId,'X-CSRF-Token':_mapCsrfToken},
      body:JSON.stringify({payload:enc})});
  const raw=await r.json();
  const resp=raw&&raw.payload?JSON.parse(landEncryption.decrypt(raw.payload,_mapSessionKey)):raw;
  if(!resp||!resp.success) return JSON.stringify({ok:false,message:resp&&resp.message,rate_limited:resp&&resp.rate_limited});
  const feats=toGeoJsonFeatures(resp.data); const b=boundsOfFeatures(feats);
  if(!b) return JSON.stringify({ok:false,message:'no geometry'});
  let c=[(b[0][0]+b[1][0])/2,(b[0][1]+b[1][1])/2];
  if(Math.abs(c[0])>180||Math.abs(c[1])>90) c=merc3857ToLngLat(c);   // defensive: data arrived in Web Mercator
  return JSON.stringify({ok:true,lon:c[0],lat:c[1],features:feats.length});
}"""

# Same request the page's fetchCheckAregSecure() makes, but returning the HTTP status and decrypted body as-is
# (the page's wrapper hides a 429 behind a generic "Failed to check Areg.").
AREG_JS = """async a=>{
  if (_mapSessionPromise) await _mapSessionPromise;
  if (!_mapSessionKey || !_mapSessionId || !_mapCsrfToken) return JSON.stringify({status:0,body:{success:0,message:'session not ready'}});
  const enc=landEncryption.encrypt(JSON.stringify({district_code:a[0],taluk_code:a[1],village_code:a[2],survey_number:a[3],
                                                   sub_division_number:'jjj',area_type:'rural'}),_mapSessionKey);
  const r=await fetch(`${GI_API_BASE}/land/check-areg`,{method:'POST',credentials:'include',
      headers:{'Content-Type':'application/json','X-Secure-Request':'true','X-Session-ID':_mapSessionId,'X-CSRF-Token':_mapCsrfToken},
      body:JSON.stringify({payload:enc})});
  const txt=await r.text(); let body={};
  try{ const j=JSON.parse(txt); body=j&&j.payload?JSON.parse(landEncryption.decrypt(j.payload,_mapSessionKey)):j; }catch(e){ body={success:0,message:'bad response: '+txt.slice(0,80)}; }
  return JSON.stringify({status:r.status, body:body});
}"""


def village_center(S, d, t, v):
    """(lat, lon) of the village centre as the site's map shows it, or None if it could not be fetched."""
    for attempt in range(3):
        try:
            r = json.loads(S.pg.evaluate(GEOM_JS, [d, t, v]))
        except Exception as e:
            print("  get-geom error:", e); time.sleep(5); continue
        if r.get("ok"): return round(r["lat"], 6), round(r["lon"], 6)
        msg = str(r.get("message", "")).lower()
        if "slow down" in msg or "unusual" in msg:
            print(f"  burst limiter tripped on get-geom; waiting {BURST_WAIT}s"); time.sleep(BURST_WAIT); continue
        if r.get("rate_limited") or "limit" in msg:
            print(f"  get-geom rate limited ({r.get('message')}); switching account"); S.next_account(cooldown_hours(msg)); continue
        print(f"  get-geom failed for village {v}: {r.get('message')}"); time.sleep(3)
    return None


COORDS_RE = r"_-?\d+\.\d+_-?\d+\.\d+\.xlsx$"  # "_<lat>_<long>.xlsx"


def out_name(dn, tn, vn, center):
    stem = f"Village_map_of_{safe(dn)}_{safe(tn)}_{safe(vn)}"
    return OUT / (f"{stem}_{center[0]}_{center[1]}.xlsx" if center else f"{stem}.xlsx")


def has_coords(f): return re.search(COORDS_RE, f.name) is not None


def find_file(stem):
    """The village's workbook, with or without coordinates in the name, or None. A regex (not a glob) so that
    'Village_map_of_X_Y_Peria' can never match 'Village_map_of_X_Y_Peria_Obulapuram_<lat>_<long>.xlsx'."""
    pat = re.compile("^" + re.escape(stem) + "(" + COORDS_RE + "|\\.xlsx$)")
    return next((f for f in sorted(OUT.glob(f"{stem}*.xlsx")) if pat.match(f.name)), None)


def subdivs(S, d, t, v, s):
    """Returns (list of sub-division numbers, rate_limit_status or {})."""
    fails = 0
    while True:
        try:
            r = json.loads(S.pg.evaluate(AREG_JS, [d, t, v, s]))
        except Exception as e:
            fails += 1; print(f"  check-areg error ({str(e).splitlines()[0][:120]}), retrying in 10s")
            if fails % 3 == 0: S.login()
            time.sleep(10); continue
        status, r = r.get("status"), r.get("body") or {}
        rl = r.get("rate_limit_status") or {}
        if status == 200 and r.get("success") == 2:
            return [x["subdiv_no"] for x in r.get("data", [])], rl
        msg = str(r.get("message", "")).lower()
        if status == 429 or r.get("rate_limited") or "limit" in msg or "too many" in msg:
            if "slow down" in msg or "unusual" in msg:  # burst detector: wait it out, then go slower
                S.delay = min(S.delay + 1, MAX_DELAY)
                print(f"  burst limiter tripped ({str(r.get('message', ''))[:60]}); waiting {BURST_WAIT}s, delay now {S.delay}s")
                time.sleep(BURST_WAIT); continue
            print(f"  hourly limit reached ({r.get('message')}); switching account"); S.next_account(cooldown_hours(msg)); continue
        if (status is not None and status != 200) or any(k in msg for k in TRANSIENT):  # 5xx, dead session...
            fails += 1; print(f"  check-areg transient failure (HTTP {status}: {r.get('message')}), retrying")
            if fails % 3 == 0: S.login()
            time.sleep(10); continue
        key = (r.get("success"), msg)
        if key not in SEEN_MSGS:  # log each distinct non-data response once so empty results are not silent
            SEEN_MSGS.add(key); print(f"  note: check-areg returned no sub-divisions for survey {s}: {str(r)[:200]}")
        return [], rl  # no sub-divisions for this survey number


def cooldown_hours(msg):
    """'Request limit of 50 per 1h exhausted. Retry after 3h.' -> 3.0 ; default COOLDOWN_H."""
    m = re.search(r"retry after\s*(\d+(?:\.\d+)?)\s*h", msg, re.I)
    return float(m.group(1)) if m else COOLDOWN_H


def pending_rows(ws):
    """[(survey_number, row)] whose sub-divisions cell is still blank = not fetched yet."""
    return [(str(ws.cell(r, 1).value), r) for r in range(2, ws.max_row + 1) if ws.cell(r, 2).value in (None, "")]


def village(S, dc, dn, tc, tn, vc, vn):
    """Scrape (or finish) one village. Returns False if another process is writing its workbook right now."""
    stem = f"Village_map_of_{safe(dn)}_{safe(tn)}_{safe(vn)}"
    for junk in OUT.glob(f"{stem}*.tmp.xlsx"):
        if time.time() - junk.stat().st_mtime > ACTIVE_WINDOW: junk.unlink()  # left over from a crash mid-save
    f = find_file(stem)
    if f:
        if time.time() - f.stat().st_mtime < ACTIVE_WINDOW:
            wb = load_workbook(f, read_only=True)
            busy = bool(pending_rows(wb.active)); wb.close()
            if busy:
                print(f"{dn} / {tn} / {vn}: workbook saved {int(time.time() - f.stat().st_mtime)}s ago by another process; will revisit"); return False
        wb = load_workbook(f); ws = wb.active
        if not pending_rows(ws) and has_coords(f): return True  # finished
    if f is None or not has_coords(f):  # need the centre (new village, or old file without coordinates)
        center = village_center(S, dc, tc, vc); time.sleep(S.delay)
        if f is None:
            surveys = [x["survey_number"] for x in api(S, "admin_master_survey_number", district_code=dc, taluk_code=tc,
                       revenue_village_code=vc, area_type="rural", data_type="cadastral", request_type="survey_number")]
            f = out_name(dn, tn, vn, center)
            wb = Workbook(); ws = wb.active; ws.title = "data"
            ws.append(["surveynumber", "sub-divisions"])
            for s in surveys: ws.append([s, None])
            save_wb(wb, f)
        elif center:
            new = out_name(dn, tn, vn, center); f.rename(new); f = new
            print(f"{dn} / {tn} / {vn}: added centre {center} to file name")
    todo = pending_rows(ws)
    total = ws.max_row - 1
    print(f"{dn} / {tn} / {vn}: {total} survey numbers ({total - len(todo)} done) -> {f.name}")
    for i, (s, row) in enumerate(todo):
        subs, rl = subdivs(S, dc, tc, vc, s)
        ws.cell(row, 2).value = ", ".join(subs) or "-"   # never leave a fetched row blank
        save_wb(wb, f)
        if i % 25 == 0: print(f"  {total - len(todo) + i}/{total} rate_limit={rl}")
        if rl and rl.get("remaining") == 0:
            print("  limit used up on this account, switching"); S.next_account()
        time.sleep(S.delay)
    print("  finished", f.name)
    return True


def run(S):
    print(f"worker {WK}/{NWK}: accounts {ACCOUNT_POOL}")
    deferred = []
    for dc, dn in [(x["district_code"], x["district_english_name"]) for x in api(S, "admin_master_district", request_type="district")
                   if match(x["district_english_name"], args.district)]:
        for tx in api(S, "admin_master_taluk", district_code=dc, request_type="taluk"):
            tc, tn = tx["taluk_code"], tx["taluk_english_name"]
            if not match(tn, args.taluk): continue
            for vi, vx in enumerate(api(S, "admin_master_village", district_code=dc, taluk_code=tc, request_type="revenue_village")):
                if vi % NWK != WK: continue  # another worker's village
                vc, vn = vx["village_code"], vx["village_english_name"]
                if not match(vn, args.village): continue
                if not village(S, dc, dn, tc, tn, vc, vn): deferred.append((dc, dn, tc, tn, vc, vn))
            if deferred:  # villages another process was writing: try them again after every taluk
                deferred = [v for v in deferred if not village(S, *v)]
    while deferred:
        print(f"{len(deferred)} village(s) were being written by another process; re-checking in 5 min")
        time.sleep(300)
        deferred = [v for v in deferred if not village(S, *v)]


def main():
    """Crash-recovery wrapper: whatever goes wrong, relaunch the browser, log in and resume from the saved progress."""
    while True:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, headless=True, args=["--no-sandbox"],
                                      proxy=PROXY)  # None = direct; otherwise every request of every context goes through it
                S = Session(b)
                S.login()
                run(S)
                b.close()
            print("All done."); return
        except KeyboardInterrupt:
            print("interrupted; progress is saved in the workbooks, just start again to resume"); return
        except Exception:
            traceback.print_exc()
            print(f"  unexpected error; relaunching browser and resuming in {CRASH_WAIT}s"); time.sleep(CRASH_WAIT)


if __name__ == "__main__":
    main()
