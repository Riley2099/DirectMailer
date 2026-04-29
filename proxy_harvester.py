#!/usr/bin/env python3
"""
Proxy Harvester - Continuously fetches and validates SOCKS5 proxies for IONOS MX compatibility.
Run in background: python proxy_harvester.py

Proxies that pass validation are appended to liveproxies.txt
"""
import re
import socket
import time
import threading
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import socks
    SOCKS_OK = True
except ImportError:
    print("[!] PySocks required. Run: pip install PySocks")
    exit(1)

# === Configuration ===
IONOS_MX_SERVERS = [
    "mxint01.1and1.com",
    "mxint02.1and1.com",
]
SMTP_PORT = 25
TIMEOUT = 15
THREADS = 40
OUTPUT_FILE = "liveproxies.txt"
LOG_FILE = "harvester_log.txt"

# Proxy sources to scrape
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
    "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt",
    "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt",
    "https://spys.me/socks.txt",
    "https://www.proxy-list.download/api/v1/get?type=socks5",
]

_BANNER_220 = re.compile(rb"^220[ -]")

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_existing_proxies():
    """Load already validated proxies to avoid duplicates."""
    if not os.path.exists(OUTPUT_FILE):
        return set()
    with open(OUTPUT_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip() and not line.startswith("#"))

def fetch_proxies_from_url(url):
    """Fetch proxy list from a URL."""
    proxies = []
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Extract ip:port pattern
                match = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)", line)
                if match:
                    proxies.append(f"{match.group(1)}:{match.group(2)}")
    except Exception as e:
        log(f"  Error fetching {url}: {e}")
    return proxies

def parse_proxy(line):
    """Parse proxy string to dict."""
    line = line.strip()
    if not line:
        return None

    proxy_type = socks.SOCKS5
    user, pw = None, None

    if "://" in line:
        scheme, rest = line.split("://", 1)
        if scheme.lower() == "socks4":
            proxy_type = socks.SOCKS4
        elif scheme.lower() == "http":
            proxy_type = socks.HTTP
        if "@" in rest:
            creds, hostport = rest.rsplit("@", 1)
            user, pw = creds.split(":", 1)
        else:
            hostport = rest
        host, port = hostport.rsplit(":", 1)
    else:
        parts = line.split(":")
        if len(parts) == 2:
            host, port = parts
        elif len(parts) == 4:
            host, port, user, pw = parts
        else:
            return None

    try:
        port = int(port)
    except ValueError:
        return None

    return {"host": host, "port": port, "user": user, "pw": pw, "type": proxy_type, "raw": line}

def check_proxy_ionos(proxy):
    """Test if proxy can connect to IONOS MX on port 25."""
    for mx in IONOS_MX_SERVERS:
        s = None
        try:
            s = socks.socksocket()
            s.set_proxy(proxy["type"], proxy["host"], proxy["port"],
                        rdns=True, username=proxy["user"], password=proxy["pw"])
            s.settimeout(TIMEOUT)
            t0 = time.monotonic()
            s.connect((mx, SMTP_PORT))

            s.settimeout(8)
            data = b""
            deadline = time.monotonic() + 8
            while b"\r\n" not in data and time.monotonic() < deadline:
                try:
                    chunk = s.recv(1024)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                data += chunk
                if len(data) > 4096:
                    break

            try:
                s.sendall(b"QUIT\r\n")
            except OSError:
                pass

            latency = int((time.monotonic() - t0) * 1000)
            if _BANNER_220.match(data):
                banner = data.decode("utf-8", errors="ignore").strip()[:80]
                return (True, latency, mx, banner)
        except Exception:
            pass
        finally:
            if s:
                try:
                    s.close()
                except OSError:
                    pass

    return (False, 0, None, None)

def harvest_cycle(existing):
    """Run one harvest cycle - fetch and validate proxies."""
    log("=" * 60)
    log("Starting new harvest cycle...")

    # Fetch from all sources
    all_proxies = set()
    for url in PROXY_SOURCES:
        log(f"  Fetching: {url[:60]}...")
        proxies = fetch_proxies_from_url(url)
        all_proxies.update(proxies)
        log(f"    Got {len(proxies)} proxies")
        time.sleep(1)  # Be nice to servers

    # Filter out already validated
    new_proxies = [p for p in all_proxies if p not in existing]
    log(f"Total fetched: {len(all_proxies)}, New to test: {len(new_proxies)}")

    if not new_proxies:
        log("No new proxies to test.")
        return 0

    # Parse and validate
    parsed = [p for p in (parse_proxy(line) for line in new_proxies) if p]
    log(f"Testing {len(parsed)} proxies against IONOS MX servers...")

    live_proxies = []
    dead_count = 0

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(check_proxy_ionos, p): p for p in parsed}
        for i, future in enumerate(as_completed(futures), 1):
            proxy = futures[future]
            ok, latency, mx, banner = future.result()
            if ok:
                live_proxies.append(proxy["raw"])
                existing.add(proxy["raw"])
                log(f"  [LIVE] {proxy['raw']:<40} {latency}ms via {mx}")
            else:
                dead_count += 1

            if i % 100 == 0:
                log(f"  Progress: {i}/{len(parsed)} | Live: {len(live_proxies)} | Dead: {dead_count}")

    # Append live proxies to output file
    if live_proxies:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            for p in live_proxies:
                f.write(p + "\n")
        log(f"Appended {len(live_proxies)} new proxies to {OUTPUT_FILE}")

    log(f"Cycle complete: {len(live_proxies)} live / {dead_count} dead")
    return len(live_proxies)

def main():
    log("=" * 60)
    log("IONOS Proxy Harvester Started")
    log(f"Output file: {OUTPUT_FILE}")
    log(f"IONOS MX servers: {', '.join(IONOS_MX_SERVERS)}")
    log("=" * 60)

    existing = load_existing_proxies()
    log(f"Loaded {len(existing)} existing validated proxies")

    total_harvested = 0
    cycle = 0

    while True:
        cycle += 1
        log(f"\n{'='*60}")
        log(f"CYCLE {cycle}")

        try:
            new_count = harvest_cycle(existing)
            total_harvested += new_count

            current_total = len(existing)
            log(f"\nTotal validated proxies: {current_total}")
            log(f"Session harvested: {total_harvested}")

            if current_total >= 400:
                log(f"\n*** TARGET REACHED: {current_total} proxies! ***")
                log("You can stop the script now or let it continue for more.")

        except KeyboardInterrupt:
            log("\nStopped by user.")
            break
        except Exception as e:
            log(f"Error in cycle: {e}")

        # Wait before next cycle (proxy lists update every 5-15 minutes)
        wait_time = random.randint(300, 600)  # 5-10 minutes
        log(f"\nWaiting {wait_time//60} minutes before next cycle...")
        log("Press Ctrl+C to stop.\n")

        try:
            time.sleep(wait_time)
        except KeyboardInterrupt:
            log("\nStopped by user.")
            break

    log(f"\nFinal count: {len(existing)} validated proxies in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
