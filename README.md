# scraper-glv

Scrapes Rural District > Taluk > Village > Survey Number > Sub-divisions from the
Tamil Nilam GI Viewer (tngis.tn.gov.in) into one Excel workbook per village.

Output: `out/Village_map_of_<District>_<Taluk>_<Village>_<lat>_<lon>.xlsx`
with the columns `surveynumber | sub-divisions`. `-` means the server reported
no sub-divisions. lat/lon is the centre of the village boundary as the site's
map shows it.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt     # requests, cryptography, PySocks, openpyxl: no browser
cp accounts.example.json accounts.json       # fill in real site accounts
.venv/bin/python scripts/selftest.py         # offline checks: crypto, parsers, a 24 h simulation of the account scheduling
```

The scraper talks to the site's APIs directly (`tngis_api.py`, documented in `API_REFERENCE.md`): a few KB per call instead of
a browser page load. The previous Chromium version is `backup/scrape_chromium.py`.

## Run

```bash
.venv/bin/python scrape.py                                  # everything
.venv/bin/python scrape.py --district Tiruvallur            # one district
.venv/bin/python scrape.py --district X --taluk Y --village Z
.venv/bin/python scrape.py --worker 1/3                     # one of 3 parallel workers
```

Under pm2 (3 workers, logs in `logs/`):

```bash
pm2 start ecosystem.config.js
pm2 logs land-scrape-0
```

The run is resumable: every fetched row is saved to the workbook immediately,
so restarting continues from the first blank row. The only other state is in `out/.progress/`:
`account_idx[.k]` (the account each worker used last), `calls[.k]` (when every account made its calls),
`cooldowns[.k]` (accounts the server told to rest, and until when) and `lockout[.k]` (end of a login lockout).
Access/refresh tokens are in `state/tokens/` (gitignored, never inside `out/`).

## Progress and moving to another machine

`out/` (the workbooks and `out/.progress/`) is committed. The pm2 job
`land-progress-sync` runs `scripts/sync_progress.sh` every 10 minutes: it
commits `out/`, pulls with rebase and pushes. So on any machine:

```bash
git clone -b <branch> https://github.com/sivaramanRW/scraper-glv.git && cd scraper-glv   # the branch you ran, e.g. direct-api-fetch
scripts/setup.sh            # venv, pulls latest progress, creates accounts.json/.env from the examples
#  -> put the real accounts in accounts.json, the captcha API URLs/token in .env
pm2 start ecosystem.config.js && pm2 save
```

and it continues from the first unfinished row of the first unfinished village.
The account state moves with it (`out/.progress/calls*` = every call's time, `cooldowns*`, `lockout*`, `account_idx*`), so the new
machine knows which accounts are resting; it needs a correct clock (NTP), since the times are absolute. The tokens in `state/tokens/`
are gitignored: without them each account logs in once with a captcha on the new machine (or copy `state/` over with scp).
The sync script commits and pushes the branch the clone is on, not only `main`.
Run one machine at a time: two clones scraping the same villages would fight
over the same workbooks (the sync keeps the pushing machine's copy on a
conflict). Right after a clone every unfinished workbook looks freshly written,
so the workers defer those for 3 minutes before resuming them; that is normal.
Pushing needs git credentials on that machine (`gh auth login` then
`gh auth setup-git`, or an SSH remote).

## Captcha

Login captchas are read by a Qwen3-VL HTTP API fleet (the `vlm-api` service).

| env | meaning |
| --- | --- |
| `CAPTCHA_API_URLS` | comma-separated base URLs, e.g. `http://host:8091,http://host:8093`. `/v1/describe/base64` is appended. |
| `CAPTCHA_API_TOKEN` | bearer token (put it in `.env`, see `.env.example`), or |
| `CAPTCHA_API_TOKEN_FILE` | file holding `VLM_API_TOKENS=...` (the API's own `.env`) |
| `CAPTCHA_LEN` | expected length, default 6; other lengths are discarded and the captcha refreshed |
| `CAPTCHA_PROMPT` | prompt sent with the image |

Worker k starts at URL k and rotates per attempt; if one API is down the others
are tried. When `CAPTCHA_API_URLS` is empty a local Ollama vision model is used
(`OLLAMA_URL`, `CAPTCHA_MODEL`).

## Proxy

`IPS_PROXY_METHOD` in `.env` chooses how the API calls reach the site (the captcha API calls stay direct):

* `ROTATION_IP` (default): everything goes through `PROXY_URL` (empty = direct). With the Android phone below, this is
  `socks5://<phone ip>:1080` and `land-phone-rotate` changes the phone's IP every `ROTATE_EVERY_S` seconds.
* `ONLINE_PROXY`: worker k uses the k-th of `ONLINE_PROXY_URLS=http://user:pass@host:port,...` (one proxy per worker); the
  phone rotator is not started.

The exit IP is printed at each login.

### Mobile proxy (Android phone, `ROTATION_IP`)

The phone's mobile data is the proxy; the laptop's own internet is untouched.

1. Phone: Wi-Fi off, mobile data on, USB cable in, USB tethering on, USB debugging on (authorise this computer),
   Every Proxy: SOCKS proxy started, battery set to unrestricted.
2. Laptop: `sudo nmcli connection modify "<tether connection>" ipv4.never-default yes` (re-activate it) so the phone link
   never becomes the default route. `sudo apt install adb`, then `adb devices` must list the phone as `device`.
3. Check: `curl --proxy socks5h://<phone ip>:1080 https://api.ipify.org` differs from a plain `curl https://api.ipify.org`.
4. `.env`: `IPS_PROXY_METHOD=ROTATION_IP`, `PROXY_URL=socks5://<phone ip>:1080`, `ROTATE_EVERY_S=600`.
5. `pm2 start ecosystem.config.js` also starts `land-phone-rotate` (`scripts/rotate_phone_ip.py`, log `logs/rotate.log`): every
   `ROTATE_EVERY_S` it toggles mobile data over adb, waits for the proxy and checks the IPv4 exit IP changed (toggles again
   if the carrier returned the same one). Try one rotation by hand: `PROXY_URL=socks5://<phone ip>:1080 python3 scripts/rotate_phone_ip.py --once`.

Requests in flight during the ~10-30 s a rotation takes fail and are retried by the scraper (the saved tokens are refreshed,
so no new login is needed). All workers share the phone's one IP, so a login lockout hits all of them.

## Server limits handled

* Per account the site allows 50 check-areg calls per hour, 125 per 4 h, 175 per 8 h and 250 per 24 h, and only reports the
  one that trips. The script never reaches any of them: it keeps the time of every call (`calls[.k]`) and stays under
  `CALL_CAPS` (default `3720:40,14520:112,28920:157,86520:225`, each a margin below the limit; +2 min on every window).
* A worker uses its 23 accounts in turn: a visit is up to `ACCOUNT_BUDGET` (40) calls, `--delay` (5 s) apart, then the next
  account. An account with less than `ACCOUNT_MIN_VISIT` (20) calls of room is skipped, and when no account has room the
  worker sleeps until the first has. About 225 calls per account per day, so roughly 5,000 per worker.
* Sessions: each account's tokens are saved and refreshed; a login with captcha happens only when the refresh token is dead.
* A refusal the server does give ("Retry after 6h") rests that account for the time it names.
* Burst detector (HTTP 429 "unusual request pattern"): waits 60 s and slows down.
* Per-network login lockout ("Too many failed login attempts from this network. Try again in 11hr 52min"): retrying during
  it restarts the timer, so the worker sleeps the whole time without trying (`lockout[.k]`).

See the docstring at the top of `scrape.py` for the full behaviour.
