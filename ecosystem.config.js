// pm2 start ecosystem.config.js            -> all 3 workers
// pm2 start ecosystem.config.js --only land-scrape-1,land-scrape-2
// pm2 logs land-scrape-0   |   pm2 stop all
// Each worker gets a third of the accounts and a third of the villages (see --worker in scrape.py).
const path = require("path");
const fs = require("fs");
const WORKERS = 3;
// .env (never committed) holds the machine-specific settings; a real environment variable wins over it.
const dotenv = {};
try {
  for (const line of fs.readFileSync(path.join(__dirname, ".env"), "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m && !line.trim().startsWith("#")) dotenv[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
} catch (_) {}
const cfg = (k, d = "") => process.env[k] || dotenv[k] || d;
const worker = (k) => ({
  name: `land-scrape-${k}`,
  script: "scrape.py",
  interpreter: path.join(__dirname, ".venv/bin/python"), // virtualenv with playwright + openpyxl
  interpreter_args: "-u",                                   // unbuffered so pm2 logs show progress live
  cwd: __dirname,
  args: `--worker ${k}/${WORKERS}`,                         // add e.g. " --district Ariyalur" to restrict
  autorestart: true,
  stop_exit_codes: [0],                                     // finished normally -> don't restart
  restart_delay: 30000,
  max_restarts: 1000,                                       // the script resumes itself; pm2 is only the safety net
  min_uptime: "30s",
  env: {
    // Captcha reader: the Qwen3-VL API fleet (~/Documents/vlm-api, pm2 qwen-vlm-api-*). Comma-separated base URLs;
    // worker k starts at URL k and rotates on each attempt, so the 3 scrapers spread over the 3 API workers.
    CAPTCHA_API_URLS: cfg("CAPTCHA_API_URLS", "http://192.168.52.115:8091,http://192.168.52.115:8093,http://192.168.52.115:8094"),
    CAPTCHA_API_TOKEN: cfg("CAPTCHA_API_TOKEN"),                       // bearer token (set in .env) ...
    CAPTCHA_API_TOKEN_FILE: cfg("CAPTCHA_API_TOKEN_FILE"),             // ... or a file with VLM_API_TOKENS=... to read it from
    CAPTCHA_LEN: cfg("CAPTCHA_LEN", "6"),                   // answers of another length are discarded and the captcha refreshed
    // Fallback only when CAPTCHA_API_URLS is empty: local Ollama vision model(s)
    OLLAMA_URL: cfg("OLLAMA_URL", "http://localhost:11434"),
    CAPTCHA_MODEL: cfg("CAPTCHA_MODEL", "qwen2.5vl:7b"),
    NO_MANUAL_CAPTCHA: "1",                                 // never block on a terminal prompt under pm2
    // Optional proxy for all browser traffic to the site (http://user:pass@host:port); captcha API calls stay direct.
    PROXY_URL: cfg("PROXY_URL"),
    PROXY_ROTATION_S: cfg("PROXY_ROTATION_S", "120"),       // the proxy's auto IP rotation period: lockouts are only waited this long
  },
  out_file: `logs/out-${k}.log`,
  error_file: `logs/err-${k}.log`,
  time: true,
});
// Every 10 min: commit out/ and push it, so another clone of this repo can resume the run (scripts/sync_progress.sh).
const progressSync = {
  name: "land-progress-sync",
  script: "scripts/sync_progress.sh",
  interpreter: "bash",
  cwd: __dirname,
  cron_restart: "*/10 * * * *",
  autorestart: false,
  out_file: "logs/sync.log",
  error_file: "logs/sync.log",
  time: true,
};
module.exports = { apps: [...Array.from({ length: WORKERS }, (_, k) => worker(k)), progressSync] };
