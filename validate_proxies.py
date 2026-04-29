#!/usr/bin/env python3
"""
Proxy validator using the same SMTP port-25 check logic as DirectMailer.
Tests each proxy by connecting through it to well-known SMTP servers on port 25
and checking for a 220 banner response.
"""
import re
import socket
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_BANNER_220 = re.compile(r"^220[ -]")

try:
    import socks
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False
    print("[!] PySocks not installed. Install with: pip install PySocks")
    print("[!] Falling back to direct TCP connect test (port reachability only).")

# SMTP test targets (same as DirectMailer's check_proxy_smtp)
TEST_HOSTS = [
    # Google / Gmail
    "gmail-smtp-in.l.google.com",
    "alt1.gmail-smtp-in.l.google.com",
    "alt2.gmail-smtp-in.l.google.com",
    "alt3.gmail-smtp-in.l.google.com",
    "alt4.gmail-smtp-in.l.google.com",
    "aspmx.l.google.com",
    # Yahoo
    "mta5.am0.yahoodns.net",
    "mta6.am0.yahoodns.net",
    "mta7.am0.yahoodns.net",
    # Outlook / Hotmail / Microsoft
    "mx1.hotmail.com",
    "mx2.hotmail.com",
    "mx3.hotmail.com",
    "mx4.hotmail.com",
    # iCloud / Apple
    "mx1.mail.icloud.com",
    "mx2.mail.icloud.com",
    # ProtonMail
    "mail.protonmail.ch",
    # Tutanota
    "mx1.tutanota.de",
    # Zoho Mail
    "mx.zoho.com",
    # AOL
    "mx-aol.mail.gm0.yahoodns.net",
]
SMTP_PORT = 25
TIMEOUT = 6
THREADS = 40

PROXY_FILE = "proxies_deduped.txt"
OUTPUT_FILE = "proxies_validated.txt"


def parse_proxy(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # Handle socks5://user:pass@ip:port or socks5://ip:port
    proxy_type = socks.SOCKS5 if SOCKS_AVAILABLE else None
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower()
        if scheme == "socks4":
            proxy_type = socks.SOCKS4 if SOCKS_AVAILABLE else None
        elif scheme == "http":
            proxy_type = socks.HTTP if SOCKS_AVAILABLE else None
        if "@" in rest:
            creds, hostport = rest.rsplit("@", 1)
            user, pw = creds.split(":", 1)
        else:
            hostport = rest
            user, pw = None, None
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
        else:
            return None
    else:
        parts = line.split(":")
        if len(parts) == 2:
            host, port = parts
            user, pw = None, None
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
    """
    Try to connect through proxy to each test SMTP host on port 25.
    Returns (proxy_raw, True, latency_ms, banner, via_host) on success,
    or (proxy_raw, False, None, None, None) on failure.
    """
    if not SOCKS_AVAILABLE:
        # Fallback: just test if proxy port is reachable
        try:
            t0 = time.monotonic()
            s = socket.create_connection((proxy["host"], proxy["port"]), timeout=TIMEOUT)
            s.close()
            latency = int((time.monotonic() - t0) * 1000)
            return (proxy["raw"], True, latency, "TCP-OPEN (PySocks not installed)", proxy["host"])
        except Exception:
            return (proxy["raw"], False, None, None, None)

    for test_host in TEST_HOSTS:
        s = None
        try:
            s = socks.socksocket()
            # rdns=True: resolve test host at the proxy, not locally
            s.set_proxy(proxy["type"], proxy["host"], proxy["port"],
                        rdns=True,
                        username=proxy["user"], password=proxy["pw"])
            s.settimeout(TIMEOUT)
            t0 = time.monotonic()
            s.connect((test_host, SMTP_PORT))

            # Loop recv until CRLF or short deadline — slow SOCKS proxies
            # can split the SMTP greeting across multiple recv calls.
            s.settimeout(5)
            data = b""
            deadline = time.monotonic() + 5
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
            if _BANNER_220.match(banner):
                return (proxy["raw"], True, latency, banner[:80], test_host)
        except Exception:
            pass
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    return (proxy["raw"], False, None, None, None)


def main():
    with open(PROXY_FILE, "r") as f:
        lines = f.readlines()

    proxies = [p for p in (parse_proxy(l) for l in lines) if p]
    print(f"[*] Loaded {len(proxies)} proxies from {PROXY_FILE}")
    print(f"[*] Testing with {THREADS} threads, timeout={TIMEOUT}s, SMTP port 25")
    print(f"[*] Test hosts: {', '.join(TEST_HOSTS)}\n")

    live = []
    dead_count = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(check_proxy, p): p for p in proxies}
        for i, future in enumerate(as_completed(futures), 1):
            raw, ok, latency, banner, via = future.result()
            if ok:
                with lock:
                    live.append(raw)
                print(f"  [LIVE] {raw:<35} {latency:>5}ms  via {via}  | {banner}")
            else:
                dead_count += 1
                if dead_count % 20 == 0:
                    print(f"  ... {dead_count} dead so far, {len(live)} live ...")

    print(f"\n[+] Results: {len(live)} LIVE / {dead_count} DEAD out of {len(proxies)} tested")

    with open(OUTPUT_FILE, "w") as f:
        f.write("\n".join(live) + "\n")

    print(f"[+] Saved {len(live)} validated proxies to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
