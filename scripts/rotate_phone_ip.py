#!/usr/bin/env python3
"""Rotate the mobile exit IP of the Android phone that serves PROXY_URL (Every Proxy over USB tethering).

Every ROTATE_EVERY_S seconds (default 300) the phone's mobile data is switched off and on through adb, so the carrier
hands out a new address. The script then waits until the SOCKS proxy works again and checks that the IPv4 exit IP really
changed (the carrier may give the same one back: it then toggles again, up to ROTATE_TRIES times).

Needs on the phone: USB debugging on, this computer authorised (`adb devices` must say "device"), Every Proxy running with
USB tethering enabled. Needs on this machine: adb (sudo apt install adb), curl.

env (a real environment variable wins; ecosystem.config.js passes them from .env):
    PROXY_URL         socks5://phone_ip:1080  - the phone's proxy (same value scrape.py uses)
    ROTATE_EVERY_S    seconds between rotations, default 300
    ROTATE_MODE       data (default: svc data disable/enable, keeps the USB tether up) | airplane
    ROTATE_OFF_S      seconds to stay offline, default 5
    ROTATE_TRIES      toggles per rotation until the IP differs, default 4
    ROTATE_BACK_S     seconds to wait for the proxy to come back after a toggle, default 90
    ADB, ADB_SERIAL   adb binary (default adb) and device serial when several phones are attached
Usage:  python scripts/rotate_phone_ip.py            # forever
        python scripts/rotate_phone_ip.py --once     # one rotation, print old -> new IP and exit
"""
import os, subprocess, sys, time
from urllib.parse import urlparse

PROXY_URL = os.environ.get("PROXY_URL", "").strip()
EVERY = int(os.environ.get("ROTATE_EVERY_S", "300"))
MODE = os.environ.get("ROTATE_MODE", "data")
OFF_S = int(os.environ.get("ROTATE_OFF_S", "5"))
TRIES = int(os.environ.get("ROTATE_TRIES", "4"))
BACK_S = int(os.environ.get("ROTATE_BACK_S", "90"))
ADB = os.environ.get("ADB", "adb")
SERIAL = os.environ.get("ADB_SERIAL", "")


def log(msg): print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


def proxy_arg():
    u = urlparse(PROXY_URL if "://" in PROXY_URL else "socks5://" + PROXY_URL)
    if not u.hostname: sys.exit("PROXY_URL is not set (socks5://<phone ip>:1080)")
    return f"socks5h://{u.hostname}:{u.port or 1080}"  # h = DNS is resolved by the phone, like a browser would


def exit_ip(host="api.ipify.org"):
    """Public IP seen through the phone's proxy (api.ipify.org = IPv4, api64.ipify.org = whichever family), or None."""
    r = subprocess.run(["curl", "-s", "-m", "15", "--proxy", proxy_arg(), f"https://{host}"], capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def adb(*args):
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode: raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def toggle():
    if MODE == "airplane":
        adb("shell", "cmd", "connectivity", "airplane-mode", "enable"); time.sleep(OFF_S)
        adb("shell", "cmd", "connectivity", "airplane-mode", "disable")
    else:
        adb("shell", "svc", "data", "disable"); time.sleep(OFF_S)
        adb("shell", "svc", "data", "enable")


def wait_back():
    """Poll until the proxy answers again; returns the new IPv4 exit IP or None on timeout."""
    end = time.time() + BACK_S
    while time.time() < end:
        ip = exit_ip()
        if ip: return ip
        time.sleep(3)


def rotate(old):
    for i in range(1, TRIES + 1):
        toggle()
        new = wait_back()
        if new and new != old:
            log(f"rotated {old} -> {new} (v6 {exit_ip('api64.ipify.org') or '?'}) after {i} toggle(s)"); return new
        log(f"toggle {i}/{TRIES}: " + ("proxy did not come back" if not new else f"same IP {new}"))
    log(f"could not get a new IP after {TRIES} toggles; keeping {new or old}"); return new or old


def main():
    state = adb("get-state")  # raises if adb is missing the phone / unauthorised
    if state != "device": sys.exit(f"adb state is '{state}', expected 'device'")
    ip = exit_ip() or wait_back()
    log(f"start: mode={MODE} every={EVERY}s proxy={proxy_arg()} exit IP {ip}")
    while True:
        try: ip = rotate(ip)
        except RuntimeError as e: log(f"adb error: {e}")
        if "--once" in sys.argv: return
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
