#!/usr/bin/env python3
"""Offline checks (no network, no account used): crypto round trip, parsers, and a 24 h simulation of one worker's account
scheduling against a fake server that enforces the site's four limits (50/1h, 125/4h, 175/8h, 250/24h).
    .venv/bin/python scripts/selftest.py"""
import sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.argv = ["scrape.py", "--worker", "0/3", "--out", tempfile.mkdtemp(), "--delay", "5"]
import tngis_api as ta

# --- crypto / parsers
assert ta.decrypt(ta.encrypt('{"a": 1}', "sessionkey"), "sessionkey") == '{"a": 1}'
assert ta.parse_wait("Request limit exhausted. Try again after 2h 23m") == 2 * 3600 + 23 * 60
assert ta.parse_wait("Request limit of 125 per 4h exhausted. Retry after 6h.") == 6 * 3600
assert ta.lockout_seconds("Too many failed login attempts from this network. Try again in 11hr 52min.") == 11 * 3600 + 52 * 60 + 90
assert ta.lockout_seconds("Invalid or missing CAPTCHA. Please try again.") == 0
print("crypto + parsers ok")

# --- virtual clock: sleep() advances time instantly
clock = [1_800_000_000.0]
time.time = lambda: clock[0]
def vsleep(s): clock[0] += max(0, s)
time.sleep = vsleep
import scrape as s

LIMITS = [(3600, 50), (4 * 3600, 125), (8 * 3600, 175), (24 * 3600, 250)]
stats = {"refused": 0, "calls": {}, "logins": 0}

class FakeApi:
    def __init__(self, proxy=None, token_file=None):
        self.tokens = False
        self.stamps = stats["calls"].setdefault(int(Path(token_file).stem), []) if token_file else []  # token_file None = the public client
    has_tokens = property(lambda self: self.tokens)
    def maybe_refresh(self): pass
    def refresh(self): pass
    def login(self, mobile, password, read_captcha): self.tokens = True; stats["logins"] += 1
    def secure(self, path, params, headers=None):
        now = clock[0]; n = [sum(1 for t in self.stamps if now - t < w) for w, _ in LIMITS]
        if any(c >= cap for c, (_, cap) in zip(n, LIMITS)):
            stats["refused"] += 1
            return 429, {"success": 0, "rate_limited": True, "message": "Request limit exhausted. Try again after 3h", "rate_limit_status": {"retry_after_seconds": 10800}}
        self.stamps.append(now)
        return 200, {"success": 2, "data": [{"subdiv_no": "1"}], "rate_limit_status": {"limit": 50, "count": n[0] + 1, "remaining": 49 - n[0], "window_hours": 1}}

s.Api = FakeApi
s.exit_ip = lambda api: "1.2.3.4"
P = s.Pool()
start, i = clock[0], 0
visits = []
orig_visit = P.visit
def visit():
    orig_visit(); visits.append((clock[0], P.idx, P.left))
P.visit = visit
import contextlib, io
log = io.StringIO()
with contextlib.redirect_stdout(log):  # the scraper's own log lines (they name accounts) are not needed here
    while clock[0] < start + 24 * 3600:
        s.subdivs(P, "d", "t", "v", str(i)); i += 1

total = sum(len(v) for v in stats["calls"].values())
per_visit_max = max(q for _, _, q in visits)
print(f"24 h simulated: {total} calls over {len(stats['calls'])} accounts, {len(visits)} visits, {stats['logins']} logins, "
      f"max quota/visit {per_visit_max}, refused by the fake server: {stats['refused']}")
assert stats["refused"] == 0, "the scheduler hit a server limit"
assert per_visit_max <= s.ACCOUNT_BUDGET
for idx, st in stats["calls"].items():  # true rolling check against the margins (CALL_CAPS), not just the server limits
    st = sorted(st)
    for j, t in enumerate(st):
        for win, cap in s.CAPS:
            assert sum(1 for u in st[:j + 1] if t - u < win) <= cap, (idx, win, cap)
# the 5 s gap between consecutive calls
allc = sorted(t for v in stats["calls"].values() for t in v)
gaps = [b - a for a, b in zip(allc, allc[1:])]
assert min(gaps) >= 4.99, min(gaps)
# a restart must remember the calls and rests
P2 = s.Pool(); assert P2.calls == {k: v for k, v in P.calls.items()}
print("scheduler ok; min gap %.1fs; busiest account made %d calls in 24 h" % (min(gaps), max(len(v) for v in stats["calls"].values())))
