"""Scrape Rural District > Taluk > Village > Survey Number > Sub Division from Tamil Nilam GI Viewer.

Usage:
    pip install playwright openpyxl && playwright install chromium   (or uses /usr/bin/google-chrome)
    python scrape.py                         # everything (very slow if rate limit is on)
    python scrape.py --district Ariyalur     # one district
    python scrape.py --district Ariyalur --taluk Andimadam --village Aiyyur

Login: the script fills mobile/password from accounts.json, saves captcha.png,
and asks you to type the captcha in the terminal.
When an account's rate limit is used up it logs in with the next account in accounts.json
(new captcha each time) and continues; the account index is remembered in out/.progress/account_idx.

Output: out/Village_map_of_<District>_<Taluk>_<Village>.xlsx   (survey number | sub-divisions)
Resumable: finished villages are skipped, partial progress is kept in out/.progress/.
Captcha: read by a local Qwen vision model via Ollama (default qwen2.5vl:7b, env CAPTCHA_MODEL / OLLAMA_URL);
after VLM_TRIES failed attempts on an account (or if Ollama is down) you are asked to type it.
The server's check-areg rate limit is honoured: when `remaining` reaches 0 the script switches account.
Only after every account has been used does it wait for the limit window to reset.
"""
import argparse, base64, json, os, re, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
from openpyxl import Workbook

HERE = Path(__file__).parent
URL = "https://tngis.tn.gov.in/apps/gi_viewer/map-viewer/index.html"
API = "https://tngis.tn.gov.in/apps/generic_api/v2/"
CHROME = "/usr/bin/google-chrome"

ap = argparse.ArgumentParser()
ap.add_argument("--district"); ap.add_argument("--taluk"); ap.add_argument("--village")
ap.add_argument("--out", default=str(HERE / "out"))
ap.add_argument("--delay", type=float, default=0.3, help="seconds between sub-division calls")
args = ap.parse_args()
OUT = Path(args.out); (OUT / ".progress").mkdir(parents=True, exist_ok=True)


def safe(s): return re.sub(r"[^\w.-]+", "_", s.strip()).strip("_")
def match(name, q): return not q or q.lower() in name.lower()


ACCOUNTS = json.load(open(HERE / "accounts.json"))
IDX_FILE = OUT / ".progress" / "account_idx"
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VLM_MODEL = os.environ.get("CAPTCHA_MODEL", "qwen2.5vl:7b")
VLM_TRIES = 3
RESET_WAIT = 600  # seconds to wait when every account has hit its limit


def read_captcha(path):
    """Ask a local Qwen vision model (Ollama) for the captcha text. Returns '' if unreadable."""
    try:
        img = base64.b64encode(Path(path).read_bytes()).decode()
        body = json.dumps({"model": VLM_MODEL, "stream": False, "options": {"temperature": 0}, "messages": [{
            "role": "user", "images": [img],
            "content": "Read the captcha text in this image exactly (case-sensitive). Reply with ONLY the characters."}]}).encode()
        req = urllib.request.Request(f"{OLLAMA}/api/chat", body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return re.sub(r"\W+", "", json.load(r)["message"]["content"])
    except Exception as e:
        print("  VLM captcha error:", e); return ""


class Session:
    """Holds the current browser context/page and the account it is logged in with."""
    def __init__(self, browser):
        self.b, self.ctx, self.pg = browser, None, None
        self.idx = int(IDX_FILE.read_text()) % len(ACCOUNTS) if IDX_FILE.exists() else 0
        self.used = 0  # accounts exhausted since last successful reset wait

    def login(self):
        fails = 0
        while True:
            acc = ACCOUNTS[self.idx]
            if self.ctx: self.ctx.close()
            self.ctx = self.b.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=3)
            self.pg = pg = self.ctx.new_page()
            print(f"Logging in with account #{self.idx} ({acc['mobile']})")
            pg.goto(URL, wait_until="networkidle")
            pg.get_by_text("Existing User").click()
            pg.fill("#publicIdentifier", acc["mobile"]); pg.fill("#publicPassword", acc["password"])
            pg.wait_for_timeout(1500)
            box = pg.locator("#publicCaptchaInput").bounding_box()
            # the captcha image sits immediately left of the input; crop to it (full page is too big for the VLM)
            clip = {"x": max(box["x"] - 205, 0), "y": box["y"] - 4, "width": 205, "height": box["height"] + 8} if box else None
            pg.screenshot(path=str(HERE / "captcha.png"), clip=clip)
            cap = read_captcha(HERE / "captcha.png") if fails < VLM_TRIES else ""
            if cap: print(f"  VLM captcha: {cap}")
            else:
                try: cap = input("Open captcha.png, type the captcha text (blank to skip this account): ").strip()
                except EOFError: cap = ""; print("  no terminal for manual captcha (pm2?), skipping account"); time.sleep(30)
            if not cap:
                self.idx = (self.idx + 1) % len(ACCOUNTS); fails = 0; continue
            pg.fill("#publicCaptchaInput", cap)
            pg.click("#publicLoginBtn")
            pg.wait_for_timeout(8000)
            if pg.query_selector("#publicLoginBtn"):
                fails += 1; print("  login failed (wrong captcha?), retrying same account"); continue
            IDX_FILE.write_text(str(self.idx))
            print("Logged in.")
            return

    def next_account(self):
        """Current account is rate limited: move on to the next one."""
        self.used += 1
        if self.used >= len(ACCOUNTS):
            print(f"  all {len(ACCOUNTS)} accounts rate limited, waiting {RESET_WAIT // 60} min")
            time.sleep(RESET_WAIT); self.used = 0
        self.idx = (self.idx + 1) % len(ACCOUNTS)
        self.login()


def api(S, path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    for attempt in range(5):
        try:
            txt = S.pg.evaluate("u=>fetch(u,{headers:{'x-app-name':'demo','x-requested-with':'XMLHttpRequest'}}).then(r=>r.text())", f"{API}{path}?{q}")
            d = json.loads(txt)
            if d.get("success") == 1: return d["data"]
        except Exception as e:
            print("  api retry:", e)
        time.sleep(2 * (attempt + 1))
    return []


def subdivs(S, d, t, v, s):
    """Returns (list of sub-division numbers, rate_limit_status or None)."""
    js = """async a=>{const r=await fetchCheckAregSecure({district_code:a[0],taluk_code:a[1],village_code:a[2],
            survey_number:a[3],sub_division_number:'jjj',area_type:'rural'});return JSON.stringify(r)}"""
    while True:
        try:
            r = json.loads(S.pg.evaluate(js, [d, t, v, s]))
        except Exception as e:
            print("  check-areg error, retrying in 10s:", e); time.sleep(10); continue
        rl = r.get("rate_limit_status") or {}
        if r.get("success") == 2:
            return [x["subdiv_no"] for x in r.get("data", [])], rl
        msg = str(r.get("message", "")).lower()
        if "limit" in msg or "too many" in msg:
            print(f"  rate limited ({r.get('message')}); switching account"); S.next_account(); continue
        return [], rl  # no sub-divisions for this survey number


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, headless=True, args=["--no-sandbox"])
        S = Session(b)
        S.login()
        for dc, dn in [(x["district_code"], x["district_english_name"]) for x in api(S, "admin_master_district", request_type="district")
                       if match(x["district_english_name"], args.district)]:
            for tx in api(S, "admin_master_taluk", district_code=dc, request_type="taluk"):
                tc, tn = tx["taluk_code"], tx["taluk_english_name"]
                if not match(tn, args.taluk): continue
                for vx in api(S, "admin_master_village", district_code=dc, taluk_code=tc, request_type="revenue_village"):
                    vc, vn = vx["village_code"], vx["village_english_name"]
                    if not match(vn, args.village): continue
                    f = OUT / f"Village_map_of_{safe(dn)}_{safe(tn)}_{safe(vn)}.xlsx"
                    if f.exists(): continue
                    prog = OUT / ".progress" / (f.stem + ".json")
                    done = json.load(open(prog)) if prog.exists() else {}
                    surveys = [x["survey_number"] for x in api(S, "admin_master_survey_number", district_code=dc, taluk_code=tc,
                               revenue_village_code=vc, area_type="rural", data_type="cadastral", request_type="survey_number")]
                    print(f"{dn} / {tn} / {vn}: {len(surveys)} survey numbers ({len(done)} done)")
                    for i, s in enumerate(surveys):
                        if s in done: continue
                        done[s], rl = subdivs(S, dc, tc, vc, s)
                        json.dump(done, open(prog, "w"))
                        if i % 25 == 0: print(f"  {i}/{len(surveys)} rate_limit={rl}")
                        if rl and rl.get("remaining") == 0:
                            print("  limit used up on this account, switching"); S.next_account()
                        time.sleep(args.delay)
                    wb = Workbook(); ws = wb.active; ws.title = "data"
                    ws.append(["surveynumber", "sub-divisions"])
                    for s in surveys: ws.append([s, ", ".join(done.get(s, []))])
                    wb.save(f); prog.unlink(missing_ok=True)
                    print("  saved", f.name)
        b.close()


if __name__ == "__main__":
    main()
