#!/usr/bin/env python3
"""
Validate proxies against IONOS MX servers (mxint01.1and1.com, mxint02.1and1.com)
Tests SMTP port 25 connectivity and checks for 220 banner response.
"""
import re
import socket
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_BANNER_220 = re.compile(rb"^220[ -]")

try:
    import socks
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False
    print("[!] PySocks not installed. Run: pip install PySocks")
    exit(1)

# IONOS MX servers
TEST_HOSTS = [
    "mxint01.1and1.com",
    "mxint02.1and1.com",
]
SMTP_PORT = 25
TIMEOUT = 15
THREADS = 50

PROXY_FILE = "proxies_socks5_fresh.txt"
OUTPUT_FILE = "liveproxies_ionos_validated.txt"


def parse_proxy(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    proxy_type = socks.SOCKS5
    user, pw = None, None

    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower()
        if scheme == "socks4":
            proxy_type = socks.SOCKS4
        elif scheme == "http":
            proxy_type = socks.HTTP
        if "@" in rest:
            creds, hostport = rest.rsplit("@", 1)
            user, pw = creds.split(":", 1)
        else:
            hostport = rest
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            return None
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


def check_proxy(proxy):
    """Test proxy against IONOS MX servers."""
    for test_host in TEST_HOSTS:
        s = None
        try:
            s = socks.socksocket()
            s.set_proxy(proxy["type"], proxy["host"], proxy["port"],
                        rdns=True,
                        username=proxy["user"], password=proxy["pw"])
            s.settimeout(TIMEOUT)
            t0 = time.monotonic()
            s.connect((test_host, SMTP_PORT))

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

            banner = data.decode("utf-8", errors="ignore").strip()
            latency = int((time.monotonic() - t0) * 1000)
            if _BANNER_220.match(data):
                return (proxy["raw"], True, latency, banner[:100], test_host)
        except Exception as e:
            pass
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    return (proxy["raw"], False, None, None, None)


def main():
    print(f"[*] IONOS MX Proxy Validator")
    print(f"[*] Testing against: {', '.join(TEST_HOSTS)}")
    print(f"[*] Port: {SMTP_PORT}, Timeout: {TIMEOUT}s, Threads: {THREADS}\n")

    with open(PROXY_FILE, "r") as f:
        lines = f.readlines()

    proxies = [p for p in (parse_proxy(l) for l in lines) if p]
    print(f"[*] Loaded {len(proxies)} proxies from {PROXY_FILE}\n")

    live = []
    dead_count = 0
    lock = threading.Lock()
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(check_proxy, p): p for p in proxies}
        for i, future in enumerate(as_completed(futures), 1):
            raw, ok, latency, banner, via = future.result()
            if ok:
                with lock:
                    live.append(raw)
                print(f"  [LIVE] {raw:<45} {latency:>5}ms via {via}")
                print(f"         Banner: {banner}")
            else:
                dead_count += 1

            # Progress update every 50 proxies
            if i % 50 == 0:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                print(f"  ... Progress: {i}/{len(proxies)} checked, {len(live)} live, {dead_count} dead ({rate:.1f}/sec)")

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"[+] RESULTS: {len(live)} LIVE / {dead_count} DEAD out of {len(proxies)}")
    print(f"[+] Time: {elapsed:.1f} seconds")
    print(f"{'='*60}")

    if live:
        with open(OUTPUT_FILE, "w") as f:
            f.write("# IONOS-validated proxies (port 25 compatible)\n")
            f.write(f"# Tested against: {', '.join(TEST_HOSTS)}\n")
            f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("\n".join(live) + "\n")
        print(f"[+] Saved {len(live)} validated proxies to: {OUTPUT_FILE}")
    else:
        print(f"[-] No proxies passed validation.")


if __name__ == "__main__":
    main()
