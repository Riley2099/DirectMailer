"""
DirectMailer v2.0 — Direct SMTP sender with GUI
• Per-recipient MX lookup  (DNS → recipient's own mail server)
• SOCKS5 / HTTP proxy pool with rotation + health tracking
• Bulk send with threading, delay, retry
• Attachment support, HTML + plain text
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import smtplib
import threading
import queue
import csv
import os, sys
import time
import socket
import logging
import json
import re
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate, make_msgid
from pathlib import Path

# ── optional deps ─────────────────────────────────────────────────────────────
try:
    import dns.resolver
    DNS_OK = True
except ImportError:
    DNS_OK = False

try:
    import socks          # PySocks
    SOCKS_OK = True
except ImportError:
    SOCKS_OK = False

APP_TITLE    = "DirectMailer v2.2"
CONFIG_FILE  = Path.home() / ".directmailer2_config.json"
SESSION_FILE = Path.home() / ".directmailer2_session.json"

log = logging.getLogger("DirectMailer")
log.setLevel(logging.DEBUG)


# ═════════════════════════════════════════════════════════════════════════════
#  PROXY POOL
# ═════════════════════════════════════════════════════════════════════════════

class ProxyPool:
    """Thread-safe proxy pool with rotation, health tracking, cooldown."""

    def __init__(self):
        self._proxies   : list[dict] = []   # {type,host,port,user,pass}
        self._lock       = threading.Lock()
        self._rr_index   = 0
        self._fails     : dict[int, int]   = {}   # id→fail count
        self._dead      : dict[int, float] = {}   # id→time of death
        self.mode        = "round_robin"   # round_robin | random | per_thread
        self.max_fails   = 3
        self.cooldown    = 60              # seconds before retrying dead proxy
        self.fallback    = True            # use direct if all proxies dead

    # ── loading ───────────────────────────────────────────────────────────────

    def load(self, raw_lines: list[str]):
        with self._lock:
            self._proxies.clear()
            self._fails.clear()
            self._dead.clear()
            self._rr_index = 0
        for line in raw_lines:
            p = self._parse(line.strip())
            if p:
                with self._lock:
                    self._proxies.append(p)

    @staticmethod
    def _parse(line: str) -> dict | None:
        """
        Accept formats:
          ip:port
          ip:port:user:pass
          socks5://user:pass@ip:port
          http://user:pass@ip:port
          socks5://ip:port
        """
        if not line or line.startswith("#"):
            return None
        ptype = "socks5"
        user = pass_ = None

        # URL-style
        m = re.match(
            r"^(socks5|socks4|http)://(?:([^:@]+):([^@]*)@)?([^:/]+):(\d+)",
            line, re.I)
        if m:
            ptype = m.group(1).lower()
            user  = m.group(2)
            pass_ = m.group(3)
            host  = m.group(4)
            port  = int(m.group(5))
            return {"type": ptype, "host": host, "port": port,
                    "user": user, "pass": pass_}

        # plain ip:port[:user:pass]
        parts = line.split(":")
        if len(parts) >= 2:
            try:
                host = parts[0]
                port = int(parts[1])
                if len(parts) == 4:
                    user, pass_ = parts[2], parts[3]
                return {"type": "socks5", "host": host, "port": port,
                        "user": user, "pass": pass_}
            except ValueError:
                pass
        return None

    # ── selection ─────────────────────────────────────────────────────────────

    def get(self, thread_id: int = 0) -> dict | None:
        """Return a live proxy or None (caller should use direct)."""
        with self._lock:
            now    = time.monotonic()
            active = []
            for p in self._proxies:
                pid = id(p)
                if pid in self._dead:
                    if now - self._dead[pid] > self.cooldown:
                        del self._dead[pid]         # cooled down, revive
                        self._fails[pid] = 0
                    else:
                        continue
                active.append(p)

            if not active:
                return None   # all dead

            if self.mode == "random":
                return random.choice(active)
            elif self.mode == "per_thread":
                return active[thread_id % len(active)]
            else:                                   # round_robin
                p = active[self._rr_index % len(active)]
                self._rr_index += 1
                return p

    def mark_fail(self, proxy: dict):
        if proxy is None:
            return
        with self._lock:
            pid = id(proxy)
            self._fails[pid] = self._fails.get(pid, 0) + 1
            if self._fails[pid] >= self.max_fails:
                self._dead[pid] = time.monotonic()
                log.warning("Proxy %s:%s marked dead (too many fails)",
                            proxy["host"], proxy["port"])

    def mark_ok(self, proxy: dict):
        if proxy is None:
            return
        with self._lock:
            pid = id(proxy)
            self._fails[pid] = 0
            self._dead.pop(pid, None)

    @property
    def stats(self) -> tuple[int, int]:
        """Return (active_count, dead_count)."""
        with self._lock:
            dead = len(self._dead)
            return len(self._proxies) - dead, dead

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._proxies)

    def to_raw_lines(self) -> list[str]:
        """Reconstruct raw ip:port (or scheme://user:pass@host:port) lines."""
        with self._lock:
            lines = []
            for p in self._proxies:
                if p["user"]:
                    lines.append(
                        f"{p['type']}://{p['user']}:{p['pass']}@{p['host']}:{p['port']}")
                else:
                    lines.append(f"{p['host']}:{p['port']}")
            return lines


PROXY_POOL = ProxyPool()


# ═════════════════════════════════════════════════════════════════════════════
#  PROXY CHECKER  — tests port-25 SMTP handshake through each proxy
# ═════════════════════════════════════════════════════════════════════════════

# Well-known SMTP servers that accept port-25 connections for banner testing
DEFAULT_TEST_HOSTS = [
    # Google / Gmail
    "gmail-smtp-in.l.google.com",
    "alt1.gmail-smtp-in.l.google.com",
    "alt2.gmail-smtp-in.l.google.com",
    "alt3.gmail-smtp-in.l.google.com",
    "alt4.gmail-smtp-in.l.google.com",
    "aspmx.l.google.com",
    # ProtonMail
    "mail.protonmail.ch",
    # Tutanota
    "mx1.tutanota.de",
    # Yahoo
    "mta5.am0.yahoodns.net",
    "mta6.am0.yahoodns.net",
    # Outlook / Hotmail / Microsoft
    "mx1.hotmail.com",
    "mx2.hotmail.com",
    # iCloud / Apple
    "mx1.mail.icloud.com",
    # Zoho Mail
    "mx.zoho.com",
    # AOL
    "mx-aol.mail.gm0.yahoodns.net",
]

_BANNER_220 = re.compile(rb"^220[ -]")


def check_proxy_smtp(proxy: dict,
                     test_hosts: list[str],
                     timeout: int = 10,
                     port: int = 25) -> tuple[bool, int, str, str]:
    """
    Connect through *proxy* to each test_host on *port* and read the SMTP
    greeting. Returns (live, latency_ms, mx_used, banner_or_error).

    A banner that begins with '220 ' or '220-' (per RFC 5321) counts as live;
    anything else — including a banner containing '220' as a substring — is
    treated as a bad/hostile server.
    """
    if not SOCKS_OK:
        return False, 0, "", "PySocks not installed"

    pmap = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4,
            "http": socks.HTTP}
    ptype = pmap.get(proxy.get("type", "socks5"), socks.SOCKS5)
    last_err = "All test hosts failed"

    for host in test_hosts:
        t0 = time.monotonic()
        sock = None
        try:
            sock = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            # rdns=True: resolve target hostname at the proxy, not locally
            sock.set_proxy(ptype, proxy["host"], int(proxy["port"]),
                           rdns=True,
                           username=proxy.get("user"),
                           password=proxy.get("pass"))
            sock.settimeout(timeout)
            sock.connect((host, port))

            # Read SMTP greeting. Loop until CRLF or short deadline, since
            # slow SOCKS proxies can split the banner across recv calls.
            sock.settimeout(5)
            banner = b""
            deadline = time.monotonic() + 5
            while b"\r\n" not in banner and time.monotonic() < deadline:
                try:
                    chunk = sock.recv(512)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                banner += chunk
                if len(banner) > 4096:
                    break

            try:
                sock.sendall(b"QUIT\r\n")     # be polite
            except OSError:
                pass

            latency = int((time.monotonic() - t0) * 1000)
            banner_str = banner.decode("utf-8", errors="ignore").strip()

            if _BANNER_220.match(banner):
                return True, latency, host, banner_str[:100]
            elif banner_str:
                last_err = f"No 220: {banner_str[:80]}"
            else:
                last_err = "Connected — no banner received"

        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:80]}"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    return False, 0, "", last_err


# ═════════════════════════════════════════════════════════════════════════════
#  PROXIED SMTP
# ═════════════════════════════════════════════════════════════════════════════

class ProxiedSMTP(smtplib.SMTP):
    """smtplib.SMTP that routes through a SOCKS5/HTTP proxy."""

    def __init__(self, host, port=25, proxy: dict | None = None, **kwargs):
        self._proxy = proxy
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        if self._proxy and SOCKS_OK:
            p = self._proxy
            ptype_map = {
                "socks5": socks.SOCKS5,
                "socks4": socks.SOCKS4,
                "http":   socks.HTTP,
            }
            ptype = ptype_map.get(p.get("type", "socks5"), socks.SOCKS5)
            sock = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
            # rdns=True: resolve the MX hostname at the proxy, not locally.
            # Without it, every send leaks the target domain to the local DNS.
            sock.set_proxy(ptype, p["host"], int(p["port"]),
                           rdns=True,
                           username=p.get("user"), password=p.get("pass"))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        return sock


# ═════════════════════════════════════════════════════════════════════════════
#  MX LOOKUP
# ═════════════════════════════════════════════════════════════════════════════

_mx_cache: dict[str, list[str]] = {}
_mx_lock  = threading.Lock()

def resolve_mx(domain: str) -> list[str]:
    with _mx_lock:
        if domain in _mx_cache:
            return _mx_cache[domain]

    result = _do_resolve_mx(domain)

    with _mx_lock:
        _mx_cache[domain] = result
    return result

def _do_resolve_mx(domain: str) -> list[str]:
    if DNS_OK:
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = ["8.8.8.8", "1.1.1.1", "8.8.4.4"]
            resolver.lifetime = 6
            try:
                answers = resolver.resolve(domain, "MX")
                hosts = [str(r.exchange).rstrip(".")
                         for r in sorted(answers, key=lambda x: x.preference)]
                log.debug("MX for %s: %s", domain, hosts)
                return hosts
            except dns.resolver.NoAnswer:
                # Domain exists but has no MX — check if it has an A record
                # (some domains handle mail directly on the domain itself)
                try:
                    resolver.resolve(domain, "A")
                    log.debug("No MX for %s, using domain A-record directly", domain)
                    return [domain]
                except Exception:
                    pass
            except dns.resolver.NXDOMAIN:
                log.debug("NXDOMAIN for %s — domain does not exist", domain)
                return []
            except Exception as e:
                log.debug("dns.resolver MX failed for %s: %s", domain, e)
        except Exception as e:
            log.debug("dns.resolver setup failed: %s", e)

    # Fallback: nslookup (Windows) / host (Linux) — hidden, no console window
    import subprocess
    for cmd in [["nslookup", "-type=MX", domain],
                ["host", "-t", "MX", domain]]:
        try:
            kwargs: dict = {"timeout": 8, "text": True,
                            "stderr": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            out = subprocess.check_output(cmd, **kwargs)
            hosts = []
            for line in out.splitlines():
                ll = line.lower()
                if "mail exchanger" in ll or "mail host" in ll:
                    h = line.split()[-1].rstrip(".")
                    hosts.append(h)
            if hosts:
                log.debug("MX fallback for %s: %s", domain, hosts)
                return hosts
        except Exception:
            pass

    fallback = f"mail.{domain}"
    log.debug("MX last-resort for %s: %s", domain, fallback)
    return [fallback]


# ═════════════════════════════════════════════════════════════════════════════
#  MESSAGE BUILDER
# ═════════════════════════════════════════════════════════════════════════════

def build_message(sender: str, sender_name: str, recipient: str,
                  subject: str, body: str, html_body: str,
                  attachments: list[str]) -> MIMEMultipart:
    msg = MIMEMultipart("mixed")
    display = f"{sender_name} <{sender}>" if sender_name else sender
    msg["From"]       = display
    msg["To"]         = recipient
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["X-Mailer"]   = "DirectMailer/2.0"

    # ── tag substitution ─────────────────────────────────────────────────
    _rand = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
    _date = formatdate(localtime=True)
    def _sub(text: str) -> str:
        _enc = recipient.replace("@", "%40")
        _dom = recipient.split("@")[-1]
        _name = sender_name or sender.split("@")[0]
        def _repl(m):
            t = m.group(0).upper()
            if t == "{{EMAIL_ENC}}": return _enc
            if t == "{{EMAIL}}":     return recipient
            if t == "{{SENDER}}":    return sender
            if t == "{{NAME}}":      return _name
            if t == "{{RANDOM}}":    return _rand
            if t == "{{DATE}}":      return _date
            if t == "{{DOMAIN}}":    return _dom
            return m.group(0)
        return re.sub(r"\{\{(?:EMAIL_ENC|EMAIL|SENDER|NAME|RANDOM|DATE|DOMAIN)\}\}",
                      _repl, text, flags=re.IGNORECASE)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(_sub(body), "plain", "utf-8"))
    if html_body.strip():
        alt.attach(MIMEText(_sub(html_body), "html", "utf-8"))
    msg.attach(alt)

    for path in attachments:
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(path))
        msg.attach(part)

    return msg


# ═════════════════════════════════════════════════════════════════════════════
#  DIRECT / PROXIED SEND
# ═════════════════════════════════════════════════════════════════════════════

def direct_send(sender: str, recipient: str, msg: MIMEMultipart,
                timeout: int = 30, helo_name: str = "",
                proxy: dict | None = None,
                retries: int = 1) -> tuple[bool, str]:
    """
    Deliver msg to recipient's MX server.
    If proxy is given, route through it.
    """
    domain  = recipient.split("@")[-1]
    mx_list = resolve_mx(domain)
    my_host = helo_name or socket.getfqdn()
    last_err = "No MX"

    proxy_tag = f" via proxy {proxy['host']}:{proxy['port']}" if proxy else " direct"

    for mx in mx_list:
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(2)
            try:
                log.debug("Connecting %s:25%s (attempt %d)",
                          mx, proxy_tag, attempt + 1)
                with ProxiedSMTP(mx, 25, proxy=proxy,
                                 local_hostname=my_host,
                                 timeout=timeout) as smtp:
                    smtp.ehlo(my_host)
                    if smtp.has_extn("STARTTLS"):
                        try:
                            import ssl
                            ctx = ssl.create_default_context()
                            ctx.check_hostname = False
                            ctx.verify_mode    = ssl.CERT_NONE
                            smtp.starttls(context=ctx)
                            smtp.ehlo(my_host)
                        except Exception:
                            pass
                    smtp.sendmail(sender, [recipient], msg.as_bytes())
                return True, f"OK via {mx}{proxy_tag}"

            except smtplib.SMTPRecipientsRefused as e:
                last_err = f"Recipient refused: {e}"
                break           # no point retrying a refusal
            except smtplib.SMTPSenderRefused as e:
                last_err = f"Sender refused: {e}"
                break
            except smtplib.SMTPException as e:
                last_err = f"SMTP {mx}: {e}"
            except (socket.timeout, ConnectionRefusedError, OSError) as e:
                last_err = f"Conn {mx}{proxy_tag}: {e}"

    return False, last_err


def send_with_proxy_pool(sender: str, recipient: str, msg: MIMEMultipart,
                         timeout: int, helo: str, retries: int,
                         thread_id: int = 0) -> tuple[bool, str]:
    """
    Auto-failover across live proxies with zero sleep between switches.
    Tries up to MAX_PROXY_ATTEMPTS different proxies per email before giving up.
    Falls back to direct only if fallback is enabled and all proxies are exhausted.
    """
    if PROXY_POOL.count == 0:
        return direct_send(sender, recipient, msg, timeout, helo, None, retries)

    MAX_PROXY_ATTEMPTS = min(max(PROXY_POOL.count, 1), 8)
    tried_ids: set[int] = set()
    last_err  = "No live proxy found"

    for attempt in range(MAX_PROXY_ATTEMPTS):
        proxy = PROXY_POOL.get(thread_id + attempt)

        # Skip if pool returned same proxy we already tried
        if proxy is None or id(proxy) in tried_ids:
            break
        tried_ids.add(id(proxy))

        # No retry per-proxy — just try once and instantly move to next if dead
        ok_, status = direct_send(sender, recipient, msg, timeout, helo,
                                   proxy, 0)
        if ok_:
            PROXY_POOL.mark_ok(proxy)
            return True, status

        # Proxy failed — mark it and immediately try the next live one
        PROXY_POOL.mark_fail(proxy)
        last_err = status
        log.debug("Proxy %s:%s failed for %s, trying next",
                  proxy["host"], proxy["port"], recipient)

    # All proxy attempts exhausted
    if PROXY_POOL.fallback:
        log.warning("All proxies failed for %s — falling back to direct", recipient)
        return direct_send(sender, recipient, msg, timeout, helo, None, retries)

    return False, f"All proxies failed: {last_err}"


# ═════════════════════════════════════════════════════════════════════════════
#  LOGGING → GUI QUEUE
# ═════════════════════════════════════════════════════════════════════════════

class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


# ═════════════════════════════════════════════════════════════════════════════
#  GUI
# ═════════════════════════════════════════════════════════════════════════════

DARK   = "#1e1e2e"
PANEL  = "#313244"
BORDER = "#45475a"
FG     = "#cdd6f4"
BLUE   = "#89b4fa"
GREEN  = "#a6e3a1"
RED    = "#f38ba8"
ORANGE = "#fab387"
GRAY   = "#585b70"


class DirectMailerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("960x760")
        self.minsize(860, 640)
        self.resizable(True, True)

        self._attachments    : list[str]  = []
        self._recipients     : list[str]  = []
        self._pending_proxies: list[dict] = []
        self._sent_set       : set[str]   = set()
        self._failed_set     : set[str]   = set()
        self._log_queue   : queue.Queue = queue.Queue()
        self._running        = False
        self._checking       = False
        self._paused         = False
        self._pause_event    = threading.Event()
        self._pause_event.set()          # set = not paused; clear = paused
        self._chk_paused     = False
        self._chk_pause_event = threading.Event()
        self._chk_pause_event.set()
        self._stats          = {"sent": 0, "failed": 0, "total": 0}
        self._send_start_ts  = 0.0

        handler = QueueHandler(self._log_queue)
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                              "%H:%M:%S"))
        log.addHandler(handler)

        self._build_ui()
        self._load_config()
        self._restore_session()

        # ICO icon
        _ico = Path(__file__).parent / "sb.ico"
        if _ico.exists():
            try:
                self.iconbitmap(str(_ico))
            except Exception:
                pass

        self._poll_log_queue()
        self._poll_proxy_stats()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── style ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.configure(bg=DARK)
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",
                    background=DARK, foreground=FG,
                    fieldbackground=PANEL, bordercolor=BORDER,
                    troughcolor=PANEL, selectbackground=BLUE,
                    selectforeground=DARK, font=("Consolas", 10))
        s.configure("TNotebook",        background=DARK, borderwidth=0)
        s.configure("TNotebook.Tab",    background=PANEL, foreground=FG,
                    padding=[12, 4])
        s.map("TNotebook.Tab",
              background=[("selected", BLUE)],
              foreground=[("selected", DARK)])
        s.configure("TFrame",           background=DARK)
        s.configure("TLabel",           background=DARK, foreground=FG)
        s.configure("TButton",          background=PANEL, foreground=FG,
                    borderwidth=1, relief="flat", padding=[8, 4])
        s.map("TButton",
              background=[("active", BORDER), ("pressed", GRAY)])
        s.configure("Accent.TButton",   background=BLUE, foreground=DARK)
        s.map("Accent.TButton",
              background=[("active", "#74c7ec"), ("pressed", "#89dceb")])
        s.configure("Danger.TButton",   background=RED,  foreground=DARK)
        s.map("Danger.TButton",
              background=[("active", "#eba0ac")])
        s.configure("TEntry",           fieldbackground=PANEL, foreground=FG,
                    insertcolor=FG, borderwidth=1)
        s.configure("TCombobox",        fieldbackground=PANEL, foreground=FG)
        s.configure("Horizontal.TProgressbar",
                    troughcolor=PANEL, background=GREEN,
                    bordercolor=BORDER, lightcolor=GREEN, darkcolor=GREEN)
        s.configure("TLabelframe",      background=DARK,
                    foreground=BLUE, bordercolor=BORDER)
        s.configure("TLabelframe.Label",background=DARK, foreground=BLUE)
        s.configure("TCheckbutton",     background=DARK, foreground=FG)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        self._tab_compose  = ttk.Frame(nb)
        self._tab_bulk     = ttk.Frame(nb)
        self._tab_proxies  = ttk.Frame(nb)
        self._tab_settings = ttk.Frame(nb)
        self._tab_logs     = ttk.Frame(nb)

        nb.add(self._tab_compose,  text="  Compose  ")
        nb.add(self._tab_bulk,     text="  Bulk Send  ")
        nb.add(self._tab_proxies,  text="  Proxies  ")
        nb.add(self._tab_settings, text="  Settings  ")
        nb.add(self._tab_logs,     text="  Logs  ")

        self._build_compose_tab()
        self._build_bulk_tab()
        self._build_proxies_tab()
        self._build_settings_tab()
        self._build_logs_tab()

        # Status bar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 6))
        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self._status_var,
                  foreground=GREEN).pack(side="left")
        self._stat_var = tk.StringVar(value="Sent: 0  Failed: 0")
        ttk.Label(bar, textvariable=self._stat_var,
                  foreground=RED).pack(side="right")
        self._proxy_stat_var = tk.StringVar(value="Proxies: none loaded")
        ttk.Label(bar, textvariable=self._proxy_stat_var,
                  foreground=ORANGE).pack(side="right", padx=20)

    # ── Compose tab ────────────────────────────────────────────────────────

    def _build_compose_tab(self):
        f = self._tab_compose
        f.columnconfigure(1, weight=1)

        fields = [
            ("From Address:", "from_addr", "you@domain.com"),
            ("From Name:",    "from_name", "Your Name"),
            ("To Address:",   "to_addr",   "recipient@proton.me"),
            ("Subject:",      "subject",   "Hello"),
        ]
        self._compose_vars = {}
        for r, (lbl, key, ph) in enumerate(fields):
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="e",
                                        padx=(12, 6), pady=5)
            var = tk.StringVar()
            e = ttk.Entry(f, textvariable=var, width=62)
            e.grid(row=r, column=1, sticky="ew", padx=(0, 12), pady=5)
            e.insert(0, ph)
            e.bind("<FocusIn>",
                   lambda ev, en=e, p=ph: en.delete(0, "end")
                   if en.get() == p else None)
            self._compose_vars[key] = var

        ttk.Label(f, text="Plain Body:").grid(row=4, column=0, sticky="ne",
                                              padx=(12, 6), pady=5)
        self._body_text = scrolledtext.ScrolledText(
            f, height=8, bg=PANEL, fg=FG, insertbackground=FG,
            font=("Consolas", 10), wrap="word", relief="flat")
        self._body_text.grid(row=4, column=1, sticky="nsew",
                             padx=(0, 12), pady=5)
        f.rowconfigure(4, weight=2)

        ttk.Label(f, text="HTML Body\n(optional):").grid(
            row=5, column=0, sticky="ne", padx=(12, 6), pady=5)
        self._html_text = scrolledtext.ScrolledText(
            f, height=4, bg=PANEL, fg=FG, insertbackground=FG,
            font=("Consolas", 10), wrap="word", relief="flat")
        self._html_text.grid(row=5, column=1, sticky="nsew",
                             padx=(0, 12), pady=5)
        f.rowconfigure(5, weight=1)

        att = ttk.LabelFrame(f, text=" Attachments ")
        att.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        att.columnconfigure(0, weight=1)
        self._att_list = tk.Listbox(att, bg=PANEL, fg=FG, height=3,
                                    selectbackground=GRAY,
                                    font=("Consolas", 9), relief="flat")
        self._att_list.grid(row=0, column=0, sticky="ew", padx=6, pady=4)
        br = ttk.Frame(att)
        br.grid(row=0, column=1, padx=6)
        for lbl, cmd in [("Add",    self._add_attachment),
                         ("Remove", self._remove_attachment),
                         ("Clear",  self._clear_attachments)]:
            ttk.Button(br, text=lbl, command=cmd).pack(fill="x", pady=2)

        ttk.Button(f, text="  Send Now  ", style="Accent.TButton",
                   command=self._send_single).grid(
            row=7, column=1, sticky="e", padx=12, pady=8)

    # ── Bulk tab ───────────────────────────────────────────────────────────

    def _build_bulk_tab(self):
        f = self._tab_bulk
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)   # recipient list expands
        f.rowconfigure(2, weight=2)   # live log expands more

        # ── Recipients load ───────────────────────────────────────────────
        top = ttk.LabelFrame(f, text=" Recipients ")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="CSV / TXT file:").grid(
            row=0, column=0, padx=8, pady=6, sticky="e")
        self._csv_var = tk.StringVar()
        ttk.Entry(top, textvariable=self._csv_var).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Browse",
                   command=self._browse_csv).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="Load",
                   command=self._load_csv).grid(row=0, column=3, padx=(0, 8))

        ttk.Label(top, text="Paste (one per line):").grid(
            row=1, column=0, padx=8, pady=4, sticky="ne")
        self._paste_text = scrolledtext.ScrolledText(
            top, height=3, bg=PANEL, fg=FG, insertbackground=FG,
            font=("Consolas", 9), wrap="none", relief="flat")
        self._paste_text.grid(row=1, column=1, columnspan=2,
                              sticky="ew", padx=4, pady=4)
        ttk.Button(top, text="Add Pasted",
                   command=self._add_pasted).grid(
            row=1, column=3, sticky="se", padx=(0, 8), pady=4)

        # ── Recipient list ────────────────────────────────────────────────
        mid = ttk.LabelFrame(f, text=" Loaded Recipients ")
        mid.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        self._recip_list = tk.Listbox(
            mid, bg=PANEL, fg=FG, height=5, selectbackground=GRAY,
            font=("Consolas", 9), relief="flat")
        rsb = ttk.Scrollbar(mid, orient="vertical",
                            command=self._recip_list.yview)
        self._recip_list.configure(yscrollcommand=rsb.set)
        self._recip_list.grid(row=0, column=0, sticky="nsew",
                              padx=(6, 0), pady=4)
        rsb.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=4)
        rbar = ttk.Frame(mid)
        rbar.grid(row=1, column=0, sticky="ew", padx=6, pady=4)
        self._recip_count_var = tk.StringVar(value="0 recipients")
        ttk.Label(rbar, textvariable=self._recip_count_var,
                  foreground=ORANGE).pack(side="left")
        ttk.Button(rbar, text="Clear List",
                   command=self._clear_recipients).pack(side="right")

        # ── Live send log ─────────────────────────────────────────────────
        live_f = ttk.LabelFrame(f, text=" Live Send Results ")
        live_f.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        live_f.columnconfigure(0, weight=1)
        live_f.rowconfigure(1, weight=1)

        # "Currently sending" line at top
        cur_row = ttk.Frame(live_f)
        cur_row.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 0))
        ttk.Label(cur_row, text="Now:", foreground=GRAY).pack(side="left")
        self._cur_email_var = tk.StringVar(value="—")
        tk.Label(cur_row, textvariable=self._cur_email_var,
                 bg=DARK, fg=BLUE,
                 font=("Consolas", 10, "bold")).pack(side="left", padx=6)
        self._rate_var = tk.StringVar(value="")
        tk.Label(cur_row, textvariable=self._rate_var,
                 bg=DARK, fg=GRAY,
                 font=("Consolas", 9)).pack(side="right", padx=6)

        self._live_log = tk.Text(
            live_f, bg="#0d1117", fg=FG,
            font=("Consolas", 9), state="disabled",
            relief="flat", wrap="none", height=8)
        live_sb = ttk.Scrollbar(live_f, orient="vertical",
                                command=self._live_log.yview)
        self._live_log.configure(yscrollcommand=live_sb.set)
        self._live_log.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=4)
        live_sb.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=4)

        self._live_log.tag_config("ok",      foreground=GREEN)
        self._live_log.tag_config("fail",    foreground=RED)
        self._live_log.tag_config("sending", foreground=BLUE)

        # ── Progress bar + stats ──────────────────────────────────────────
        prog_f = ttk.LabelFrame(f, text=" Progress ")
        prog_f.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 0))
        prog_f.columnconfigure(0, weight=1)
        self._progress = ttk.Progressbar(prog_f, mode="determinate",
                                         style="Horizontal.TProgressbar")
        self._progress.grid(row=0, column=0, columnspan=2,
                            sticky="ew", padx=8, pady=6)
        self._prog_label = tk.StringVar(value="Idle")
        ttk.Label(prog_f, textvariable=self._prog_label,
                  foreground=GREEN).grid(row=1, column=0, sticky="w",
                                         padx=8, pady=(0, 6))

        # ── Control buttons ───────────────────────────────────────────────
        btn_row = ttk.Frame(f)
        btn_row.grid(row=4, column=0, sticky="ew", padx=12, pady=6)

        ttk.Button(btn_row, text="  Start Bulk Send  ",
                   style="Accent.TButton",
                   command=self._start_bulk).pack(side="left", padx=4)

        self._pause_send_btn = ttk.Button(
            btn_row, text="  Pause  ",
            command=self._toggle_pause_send)
        self._pause_send_btn.pack(side="left", padx=4)

        ttk.Button(btn_row, text="  Stop  ",
                   style="Danger.TButton",
                   command=self._stop_bulk).pack(side="left", padx=4)

        ttk.Button(btn_row, text="Clear Log",
                   command=self._clear_live_log).pack(side="right", padx=4)

    # ── Proxies tab ────────────────────────────────────────────────────────

    def _build_proxies_tab(self):
        f = self._tab_proxies
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)   # results treeview expands

        # ── Row 0: Load / Paste ───────────────────────────────────────────
        top = ttk.LabelFrame(f, text=" 1. Load Proxies (goes to Pending) ")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="File:").grid(row=0, column=0, padx=8, pady=5, sticky="e")
        self._proxy_file_var = tk.StringVar()
        ttk.Entry(top, textvariable=self._proxy_file_var).grid(
            row=0, column=1, sticky="ew", padx=4, pady=5)
        ttk.Button(top, text="Browse",
                   command=self._browse_proxy_file).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="Load File",
                   style="Accent.TButton",
                   command=self._load_proxy_file_to_pending).grid(
            row=0, column=3, padx=(0, 8))

        ttk.Label(top,
                  text="Paste (one per line) — ip:port  |  ip:port:user:pass  |  socks5://user:pass@ip:port",
                  foreground=GRAY, font=("Consolas", 8)).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=8)
        self._proxy_paste = scrolledtext.ScrolledText(
            top, height=4, bg=PANEL, fg=FG, insertbackground=FG,
            font=("Consolas", 9), wrap="none", relief="flat")
        self._proxy_paste.grid(row=2, column=0, columnspan=4,
                               sticky="ew", padx=6, pady=4)
        pr = ttk.Frame(top)
        pr.grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))
        ttk.Button(pr, text="Add Pasted to Pending",
                   command=self._add_pasted_to_pending).pack(side="left", padx=4)
        self._pending_count_var = tk.StringVar(value="Pending: 0")
        ttk.Label(pr, textvariable=self._pending_count_var,
                  foreground=ORANGE,
                  font=("Consolas", 9, "bold")).pack(side="left", padx=12)
        ttk.Button(pr, text="Clear Pending",
                   style="Danger.TButton",
                   command=self._clear_pending).pack(side="left", padx=4)

        # ── Row 1: Check controls ─────────────────────────────────────────
        chk = ttk.LabelFrame(f,
                             text=" 2. Check Port-25 Compatibility ")
        chk.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        chk.columnconfigure(5, weight=1)

        ttk.Label(chk, text="Test hosts (comma-separated):").grid(
            row=0, column=0, padx=8, pady=6, sticky="e")
        self._test_hosts_var = tk.StringVar(
            value="mail.protonmail.ch,mx1.tutanota.de")
        ttk.Entry(chk, textvariable=self._test_hosts_var,
                  width=40).grid(row=0, column=1, columnspan=3,
                                 sticky="ew", padx=4)

        ttk.Label(chk, text="Timeout (s):").grid(
            row=0, column=4, padx=(12, 4), sticky="e")
        self._check_timeout_var = tk.StringVar(value="10")
        ttk.Entry(chk, textvariable=self._check_timeout_var,
                  width=5).grid(row=0, column=5, sticky="w")

        ttk.Label(chk, text="Threads:").grid(
            row=1, column=0, padx=8, pady=6, sticky="e")
        self._check_threads_var = tk.StringVar(value="10")
        ttk.Entry(chk, textvariable=self._check_threads_var,
                  width=5).grid(row=1, column=1, sticky="w", padx=4)

        self._check_progress = ttk.Progressbar(
            chk, mode="determinate",
            style="Horizontal.TProgressbar", length=220)
        self._check_progress.grid(row=1, column=2, columnspan=3,
                                  padx=8, pady=6, sticky="ew")

        self._check_status_var = tk.StringVar(value="Idle")
        ttk.Label(chk, textvariable=self._check_status_var,
                  foreground=BLUE,
                  font=("Consolas", 9)).grid(row=1, column=5,
                                              padx=8, sticky="w")

        btn_chk = ttk.Frame(chk)
        btn_chk.grid(row=2, column=0, columnspan=6,
                     sticky="w", padx=6, pady=(0, 6))
        ttk.Button(btn_chk, text="  Check All Pending  ",
                   style="Accent.TButton",
                   command=self._start_proxy_check).pack(side="left", padx=4)
        self._pause_chk_btn = ttk.Button(
            btn_chk, text="  Pause  ",
            command=self._toggle_pause_check)
        self._pause_chk_btn.pack(side="left", padx=4)
        ttk.Button(btn_chk, text="Stop",
                   style="Danger.TButton",
                   command=self._stop_proxy_check).pack(side="left", padx=4)
        ttk.Button(btn_chk, text="Clear Results",
                   command=self._clear_check_results).pack(side="left", padx=12)
        ttk.Button(btn_chk, text="  ⚕ Heal Pool  ",
                   style="Accent.TButton",
                   command=self._heal_pool).pack(side="left", padx=12)

        # ── Row 2: Results treeview ───────────────────────────────────────
        res_f = ttk.LabelFrame(f, text=" 3. Results  (Live proxies auto-push to Active Pool) ")
        res_f.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        res_f.columnconfigure(0, weight=1)
        res_f.rowconfigure(0, weight=1)

        cols = ("proxy", "type", "status", "latency", "via", "banner")
        self._result_tree = ttk.Treeview(
            res_f, columns=cols, show="headings", height=10)

        hdrs = [("proxy",   "Proxy",         160),
                ("type",    "Type",           60),
                ("status",  "Status",         60),
                ("latency", "Latency",        70),
                ("via",     "Connected Via",  180),
                ("banner",  "SMTP Banner",    260)]
        for col, hdr, w in hdrs:
            self._result_tree.heading(col, text=hdr)
            self._result_tree.column(col, width=w, minwidth=40)

        self._result_tree.tag_configure("live", foreground=GREEN)
        self._result_tree.tag_configure("dead", foreground=RED)

        vsb = ttk.Scrollbar(res_f, orient="vertical",
                            command=self._result_tree.yview)
        hsb = ttk.Scrollbar(res_f, orient="horizontal",
                            command=self._result_tree.xview)
        self._result_tree.configure(yscrollcommand=vsb.set,
                                    xscrollcommand=hsb.set)
        self._result_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Summary under tree
        summary_r = ttk.Frame(res_f)
        summary_r.grid(row=2, column=0, columnspan=2,
                       sticky="ew", padx=6, pady=(2, 6))
        self._res_live_var = tk.StringVar(value="Live: 0")
        self._res_dead_var = tk.StringVar(value="Dead: 0")
        ttk.Label(summary_r, textvariable=self._res_live_var,
                  foreground=GREEN,
                  font=("Consolas", 9, "bold")).pack(side="left", padx=8)
        ttk.Label(summary_r, textvariable=self._res_dead_var,
                  foreground=RED,
                  font=("Consolas", 9, "bold")).pack(side="left", padx=4)

        # ── Row 3: Active pool options ────────────────────────────────────
        pool_f = ttk.LabelFrame(f, text=" 4. Active Pool Options ")
        pool_f.grid(row=3, column=0, sticky="ew", padx=12, pady=(4, 8))
        pool_f.columnconfigure(5, weight=1)

        self._px_active_var = tk.StringVar(value="0")
        self._px_dead_var   = tk.StringVar(value="0")
        ttk.Label(pool_f, text="Pool active:").grid(
            row=0, column=0, padx=8, pady=6, sticky="e")
        ttk.Label(pool_f, textvariable=self._px_active_var,
                  foreground=GREEN,
                  font=("Consolas", 10, "bold")).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(pool_f, text="Dead/cooling:").grid(
            row=0, column=2, padx=8, sticky="e")
        ttk.Label(pool_f, textvariable=self._px_dead_var,
                  foreground=RED,
                  font=("Consolas", 10, "bold")).grid(
            row=0, column=3, sticky="w", padx=4)
        ttk.Button(pool_f, text="Clear Active Pool",
                   style="Danger.TButton",
                   command=self._clear_active_pool).grid(
            row=0, column=4, padx=12, pady=6)

        ttk.Label(pool_f, text="Rotation:").grid(
            row=1, column=0, padx=8, pady=6, sticky="e")
        self._rotation_var = tk.StringVar(value="round_robin")
        cb = ttk.Combobox(pool_f, textvariable=self._rotation_var,
                          values=["round_robin", "random", "per_thread"],
                          state="readonly", width=14)
        cb.grid(row=1, column=1, padx=4, sticky="w")
        cb.bind("<<ComboboxSelected>>", self._apply_proxy_options)

        ttk.Label(pool_f, text="Max fails:").grid(
            row=1, column=2, padx=8, sticky="e")
        self._max_fails_var = tk.StringVar(value="3")
        ttk.Entry(pool_f, textvariable=self._max_fails_var,
                  width=5).grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(pool_f, text="Cooldown (s):").grid(
            row=1, column=4, padx=8, sticky="e")
        self._cooldown_var = tk.StringVar(value="60")
        ttk.Entry(pool_f, textvariable=self._cooldown_var,
                  width=5).grid(row=1, column=5, sticky="w", padx=4)

        self._fallback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(pool_f,
                        text="Fall back to direct send when all proxies dead",
                        variable=self._fallback_var,
                        command=self._apply_proxy_options).grid(
            row=2, column=0, columnspan=6, padx=8, pady=(0, 6), sticky="w")

        if not SOCKS_OK:
            ttk.Label(pool_f,
                      text="PySocks not installed — pip install PySocks",
                      foreground=RED).grid(row=3, column=0, columnspan=6,
                                           padx=8, pady=(0, 6), sticky="w")

    # ── Settings tab ───────────────────────────────────────────────────────

    def _build_settings_tab(self):
        f = self._tab_settings
        f.columnconfigure(1, weight=1)

        fields = [
            ("HELO Hostname:",         "helo",    socket.getfqdn(),
             "Sent in EHLO — use your VPS hostname"),
            ("SMTP Port:",             "port",    "25",
             "25 for direct delivery"),
            ("Connection Timeout (s):", "timeout", "30",  "Per MX attempt"),
            ("Delay Between Sends (s):", "delay",  "1.0", "Float OK (0.5, 2.0 …)"),
            ("Max Threads:",            "threads", "3",
             "Parallel sender threads"),
            ("Retry Failed:",           "retry",   "1",
             "Extra attempts per recipient on failure"),
        ]
        self._setting_vars = {}
        for r, (lbl, key, default, tip) in enumerate(fields):
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="e",
                                        padx=(16, 8), pady=8)
            var = tk.StringVar(value=default)
            ttk.Entry(f, textvariable=var, width=28).grid(
                row=r, column=1, sticky="w", pady=8)
            ttk.Label(f, text=tip, foreground=GRAY,
                      font=("Consolas", 8)).grid(
                row=r, column=2, sticky="w", padx=12)
            self._setting_vars[key] = var

        sep = ttk.Separator(f, orient="horizontal")
        sep.grid(row=len(fields), column=0, columnspan=3,
                 sticky="ew", pady=12, padx=16)

        # Lib status
        lines = [
            (f"dnspython:  {'installed — full MX lookup' if DNS_OK else 'NOT installed  (pip install dnspython)'}",
             GREEN if DNS_OK else RED),
            (f"PySocks:    {'installed — proxy support active' if SOCKS_OK else 'NOT installed  (pip install PySocks)'}",
             GREEN if SOCKS_OK else RED),
        ]
        for i, (txt, col) in enumerate(lines):
            ttk.Label(f, text=txt, foreground=col,
                      font=("Consolas", 9)).grid(
                row=len(fields)+1+i, column=0, columnspan=3,
                sticky="w", padx=16, pady=2)

        btn_row = ttk.Frame(f)
        btn_row.grid(row=len(fields)+4, column=0, columnspan=3,
                     sticky="w", padx=16, pady=12)
        ttk.Button(btn_row, text="Save Settings",
                   style="Accent.TButton",
                   command=self._save_config).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Reset Defaults",
                   command=self._reset_settings).pack(side="left", padx=4)

    # ── Logs tab ───────────────────────────────────────────────────────────

    def _build_logs_tab(self):
        f = self._tab_logs
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self._log_box = scrolledtext.ScrolledText(
            f, bg="#11111b", fg=FG, insertbackground=FG,
            font=("Consolas", 9), state="disabled",
            wrap="none", relief="flat")
        self._log_box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self._log_box.tag_config("ERROR",   foreground=RED)
        self._log_box.tag_config("WARNING", foreground=ORANGE)
        self._log_box.tag_config("INFO",    foreground=GREEN)
        self._log_box.tag_config("DEBUG",   foreground=GRAY)

        br = ttk.Frame(f)
        br.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        ttk.Button(br, text="Clear", command=self._clear_logs).pack(
            side="left", padx=4)
        ttk.Button(br, text="Save to File", command=self._save_logs).pack(
            side="left", padx=4)

    # ── Compose actions ────────────────────────────────────────────────────

    def _add_attachment(self):
        for f in filedialog.askopenfilenames(title="Select attachments"):
            if f not in self._attachments:
                self._attachments.append(f)
                self._att_list.insert("end", os.path.basename(f))

    def _remove_attachment(self):
        for i in reversed(self._att_list.curselection()):
            self._attachments.pop(i)
            self._att_list.delete(i)

    def _clear_attachments(self):
        self._attachments.clear()
        self._att_list.delete(0, "end")

    def _send_single(self):
        sender    = self._compose_vars["from_addr"].get().strip()
        recipient = self._compose_vars["to_addr"].get().strip()
        subject   = self._compose_vars["subject"].get().strip()
        name      = self._compose_vars["from_name"].get().strip()
        body      = self._body_text.get("1.0", "end-1c")
        html      = self._html_text.get("1.0", "end-1c")

        if not _valid(sender) or not _valid(recipient):
            messagebox.showerror("Invalid Email",
                                 "Enter valid From and To addresses.")
            return
        msg = build_message(sender, name, recipient, subject,
                            body, html, self._attachments)
        self._status_var.set("Sending…")
        threading.Thread(target=self._do_send,
                         args=(sender, recipient, msg, True, 0),
                         daemon=True).start()

    def _do_send(self, sender, recipient, msg,
                 single=False, thread_id=0):
        helo    = self._setting_vars["helo"].get().strip()
        timeout = int(self._setting_vars["timeout"].get() or 30)
        retries = int(self._setting_vars["retry"].get() or 1)

        ok_, status = send_with_proxy_pool(sender, recipient, msg,
                                           timeout, helo, retries,
                                           thread_id)
        if ok_:
            log.info("OK   → %s  (%s)", recipient, status)
            self._stats["sent"] += 1
        else:
            log.error("FAIL → %s  (%s)", recipient, status)
            self._stats["failed"] += 1

        self._update_stat_bar()
        if single:
            self.after(0, lambda: self._status_var.set(
                "Sent OK" if ok_ else f"Failed: {status}"))

    # ── Bulk actions ───────────────────────────────────────────────────────

    def _browse_csv(self):
        f = filedialog.askopenfilename(
            filetypes=[("CSV/TXT", "*.csv *.txt"), ("All", "*.*")])
        if f:
            self._csv_var.set(f)

    def _load_csv(self):
        path = self._csv_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("Not found", path)
            return
        added = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            if path.lower().endswith(".txt"):
                for line in fh:
                    e = line.strip()
                    if _valid(e) and e not in self._recipients:
                        self._recipients.append(e)
                        self._recip_list.insert("end", e)
                        added += 1
            else:
                for row in csv.reader(fh):
                    if row:
                        e = row[0].strip()
                        if _valid(e) and e not in self._recipients:
                            self._recipients.append(e)
                            self._recip_list.insert("end", e)
                            added += 1
        self._refresh_recip_count()
        log.info("Loaded %d recipients from %s", added, path)

    def _add_pasted(self):
        added = 0
        for line in self._paste_text.get("1.0", "end-1c").splitlines():
            e = line.strip().strip(",;")
            if _valid(e) and e not in self._recipients:
                self._recipients.append(e)
                self._recip_list.insert("end", e)
                added += 1
        self._refresh_recip_count()
        log.info("Added %d pasted recipients", added)

    def _clear_recipients(self):
        self._recipients.clear()
        self._sent_set.clear()
        self._failed_set.clear()
        self._recip_list.delete(0, "end")
        self._refresh_recip_count()

    def _refresh_recip_count(self):
        n = len(self._recipients)
        self._recip_count_var.set(f"{n} recipient{'s' if n!=1 else ''} loaded")

    def _start_bulk(self):
        if self._running:
            messagebox.showinfo("Running", "Bulk send already in progress.")
            return
        if not self._recipients:
            messagebox.showerror("No recipients", "Load recipients first.")
            return
        sender = self._compose_vars["from_addr"].get().strip()
        if not _valid(sender):
            messagebox.showerror("Invalid sender",
                                 "Set a valid From address on the Compose tab.")
            return

        self._running = True
        self._stats   = {"sent": 0, "failed": 0,
                         "total": len(self._recipients)}
        self._progress["maximum"] = self._stats["total"]
        self._progress["value"]   = 0

        threading.Thread(target=self._bulk_worker,
                         args=(sender,
                               self._compose_vars["from_name"].get().strip(),
                               self._compose_vars["subject"].get().strip(),
                               self._body_text.get("1.0", "end-1c"),
                               self._html_text.get("1.0", "end-1c"),
                               list(self._recipients)),
                         daemon=True).start()

    def _bulk_worker(self, sender, name, subject, body, html, recipients):
        delay   = float(self._setting_vars["delay"].get() or 1.0)
        threads = max(1, int(self._setting_vars["threads"].get() or 3))
        timeout = int(self._setting_vars["timeout"].get() or 30)
        helo    = self._setting_vars["helo"].get().strip()
        retries = int(self._setting_vars["retry"].get() or 1)

        q: queue.Queue = queue.Queue()
        for r in recipients:
            q.put(r)

        done  = [0]
        lock  = threading.Lock()
        self._send_start_ts = time.time()

        def worker(tid):
            while self._running:
                # ── Pause gate ─────────────────────────────────────────────
                if not self._pause_event.is_set():
                    self.after(0, lambda: self._status_var.set("PAUSED"))
                    self._pause_event.wait()
                    if not self._running:
                        break
                    self.after(0, lambda: self._status_var.set("Sending…"))

                try:
                    recip = q.get_nowait()
                except queue.Empty:
                    break

                # Show "now sending" in live log header
                self.after(0, self._cur_email_var.set, recip)

                msg = build_message(sender, name, recip, subject,
                                    body, html, self._attachments)
                ok_, status = send_with_proxy_pool(
                    sender, recip, msg, timeout, helo, retries, tid)

                ts = time.strftime("%H:%M:%S")
                if ok_:
                    log.info("OK   → %s  (%s)", recip, status)
                    with lock:
                        self._stats["sent"] += 1
                        self._sent_set.add(recip)
                    line = f"✓  {ts}  OK    {recip:<38}  {status}\n"
                    self.after(0, self._append_live_log, line, "ok")
                else:
                    log.error("FAIL → %s  (%s)", recip, status)
                    with lock:
                        self._stats["failed"] += 1
                        self._failed_set.add(recip)
                    line = f"✗  {ts}  FAIL  {recip:<38}  {status}\n"
                    self.after(0, self._append_live_log, line, "fail")

                self._update_stat_bar()
                time.sleep(delay)

                with lock:
                    done[0] += 1
                    if done[0] % 10 == 0:   # autosave every 10 emails
                        self.after(0, self._save_session)
                self.after(0, self._tick_progress, done[0])

        thread_list = [threading.Thread(target=worker, args=(i,),
                                        daemon=True)
                       for i in range(threads)]
        for t in thread_list:
            t.start()
        for t in thread_list:
            t.join()

        self._running = False
        self.after(0, self._cur_email_var.set, "—")
        self.after(0, self._bulk_done)

    def _tick_progress(self, done):
        self._progress["value"] = done
        total   = max(self._stats["total"], 1)
        pct     = int(done / total * 100)
        elapsed = max(time.time() - self._send_start_ts, 1)
        rate    = done / elapsed * 60
        bar_w   = 20
        filled  = int(pct / 100 * bar_w)
        bar     = "█" * filled + "░" * (bar_w - filled)
        self._prog_label.set(
            f"[{bar}] {pct:3d}%  "
            f"{done}/{self._stats['total']}  │  "
            f"✓ {self._stats['sent']}  ✗ {self._stats['failed']}  │  "
            f"{rate:.1f}/min")
        self._rate_var.set(f"{rate:.1f} emails/min")

    def _bulk_done(self):
        self._prog_label.set(
            f"Complete — Sent: {self._stats['sent']}  "
            f"Failed: {self._stats['failed']}")
        self._status_var.set("Bulk send complete")
        log.info("Bulk done. Sent=%d  Failed=%d",
                 self._stats["sent"], self._stats["failed"])

    def _stop_bulk(self):
        self._running = False
        # Unblock pause so workers can exit cleanly
        self._pause_event.set()
        self._paused = False
        self._pause_send_btn.configure(text="  Pause  ")
        self._status_var.set("Stopping…")

    def _toggle_pause_send(self):
        if not self._running:
            return
        if self._paused:
            # Resume
            self._paused = False
            self._pause_event.set()
            self._pause_send_btn.configure(text="  Pause  ")
            self._status_var.set("Sending…")
            log.info("Bulk send resumed")
        else:
            # Pause
            self._paused = True
            self._pause_event.clear()
            self._pause_send_btn.configure(text="  Resume  ")
            self._status_var.set("PAUSED")
            log.info("Bulk send paused")

    def _toggle_pause_check(self):
        if not self._checking:
            return
        if self._chk_paused:
            self._chk_paused = False
            self._chk_pause_event.set()
            self._pause_chk_btn.configure(text="  Pause  ")
            self._check_status_var.set("Checking…")
            log.info("Proxy check resumed")
        else:
            self._chk_paused = True
            self._chk_pause_event.clear()
            self._pause_chk_btn.configure(text="  Resume  ")
            self._check_status_var.set("PAUSED")
            log.info("Proxy check paused")

    def _append_live_log(self, line: str, tag: str):
        self._live_log.configure(state="normal")
        self._live_log.insert("end", line, tag)
        self._live_log.see("end")
        self._live_log.configure(state="disabled")

    def _clear_live_log(self):
        self._live_log.configure(state="normal")
        self._live_log.delete("1.0", "end")
        self._live_log.configure(state="disabled")

    # ── Proxy actions ──────────────────────────────────────────────────────

    def _browse_proxy_file(self):
        f = filedialog.askopenfilename(
            filetypes=[("Text/CSV", "*.txt *.csv"), ("All", "*.*")])
        if f:
            self._proxy_file_var.set(f)

    def _load_proxy_file_to_pending(self):
        path = self._proxy_file_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("Not found", path)
            return
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        added = 0
        for line in lines:
            p = ProxyPool._parse(line.strip())
            if p and p not in self._pending_proxies:
                self._pending_proxies.append(p)
                added += 1
        self._refresh_pending_count()
        log.info("Loaded %d proxies to pending from %s", added, path)

    def _add_pasted_to_pending(self):
        raw = self._proxy_paste.get("1.0", "end-1c").splitlines()
        added = 0
        for line in raw:
            p = ProxyPool._parse(line.strip())
            if p and p not in self._pending_proxies:
                self._pending_proxies.append(p)
                added += 1
        self._refresh_pending_count()
        log.info("Added %d proxies to pending from paste", added)

    def _clear_pending(self):
        self._pending_proxies.clear()
        self._refresh_pending_count()

    def _refresh_pending_count(self):
        self._pending_count_var.set(f"Pending: {len(self._pending_proxies)}")

    # ── Proxy checker ──────────────────────────────────────────────────────

    def _start_proxy_check(self):
        if self._checking:
            messagebox.showinfo("Running", "Check already in progress.")
            return
        if not self._pending_proxies:
            messagebox.showerror("No proxies", "Load proxies to pending first.")
            return
        if not SOCKS_OK:
            messagebox.showerror("PySocks missing",
                                 "Run:  pip install PySocks  then restart.")
            return

        hosts_raw = self._test_hosts_var.get().strip()
        test_hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
        if not test_hosts:
            test_hosts = DEFAULT_TEST_HOSTS

        threads = max(1, int(self._check_threads_var.get() or 10))
        timeout = max(1, int(self._check_timeout_var.get() or 10))

        self._checking = True
        self._check_progress["maximum"] = len(self._pending_proxies)
        self._check_progress["value"]   = 0
        self._check_status_var.set("Checking…")

        threading.Thread(
            target=self._check_worker,
            args=(list(self._pending_proxies), test_hosts, timeout, threads),
            daemon=True
        ).start()

    def _stop_proxy_check(self):
        self._checking = False
        self._chk_pause_event.set()   # unblock if paused
        self._chk_paused = False
        self._pause_chk_btn.configure(text="  Pause  ")
        self._check_status_var.set("Stopped")

    def _check_worker(self, proxies, test_hosts, timeout, n_threads):
        q: queue.Queue = queue.Queue()
        for p in proxies:
            q.put(p)

        live_count = [0]
        dead_count = [0]
        done_count = [0]
        lock = threading.Lock()

        def worker():
            while self._checking:
                # Pause gate
                if not self._chk_pause_event.is_set():
                    self._chk_pause_event.wait()
                    if not self._checking:
                        break

                try:
                    proxy = q.get_nowait()
                except queue.Empty:
                    break

                live, latency, via, banner = check_proxy_smtp(
                    proxy, test_hosts, timeout)

                tag    = "live" if live else "dead"
                label  = "LIVE" if live  else "DEAD"
                lat_s  = f"{latency} ms" if latency else "—"
                row = (f"{proxy['host']}:{proxy['port']}",
                       proxy.get("type", "socks5"),
                       label, lat_s, via or "—", banner)

                with lock:
                    if live:
                        live_count[0] += 1
                        PROXY_POOL._proxies.append(proxy)
                        PROXY_POOL._fails[id(proxy)] = 0
                    else:
                        dead_count[0] += 1
                    done_count[0] += 1

                self.after(0, self._add_check_result, row, tag,
                           done_count[0], live_count[0], dead_count[0])

        thread_list = [threading.Thread(target=worker, daemon=True)
                       for _ in range(n_threads)]
        for t in thread_list:
            t.start()
        for t in thread_list:
            t.join()

        self._checking = False
        total = live_count[0] + dead_count[0]
        self.after(0, self._check_done, live_count[0], dead_count[0], total)

    def _add_check_result(self, row, tag, done, live, dead):
        self._result_tree.insert("", "end", values=row, tags=(tag,))
        self._result_tree.yview_moveto(1)
        self._check_progress["value"] = done
        self._res_live_var.set(f"Live: {live}")
        self._res_dead_var.set(f"Dead: {dead}")
        self._check_status_var.set(
            f"Checked {done} / {self._check_progress['maximum']}")

    def _check_done(self, live, dead, total):
        self._check_status_var.set(
            f"Done — {live} live  {dead} dead  out of {total}")
        a, d = PROXY_POOL.stats
        log.info("Proxy check complete: %d live pushed to pool, %d dead", live, dead)
        self._pending_proxies.clear()
        self._refresh_pending_count()

    def _clear_check_results(self):
        for item in self._result_tree.get_children():
            self._result_tree.delete(item)
        self._res_live_var.set("Live: 0")
        self._res_dead_var.set("Dead: 0")
        self._check_status_var.set("Idle")
        self._check_progress["value"] = 0

    def _clear_active_pool(self):
        PROXY_POOL.load([])
        log.info("Active proxy pool cleared")

    def _heal_pool(self):
        """Re-test dead proxies and revive any that now pass the 220 check."""
        with PROXY_POOL._lock:
            dead_ids     = set(PROXY_POOL._dead.keys())
            dead_proxies = [p for p in PROXY_POOL._proxies
                            if id(p) in dead_ids]

        if not dead_proxies:
            messagebox.showinfo("Heal Pool",
                                "No dead proxies to heal — pool is fully active.")
            return
        if not SOCKS_OK:
            messagebox.showerror("PySocks missing",
                                 "pip install PySocks  then restart.")
            return

        hosts   = [h.strip() for h in
                   self._test_hosts_var.get().split(",") if h.strip()]
        timeout = max(1, int(self._check_timeout_var.get() or 10))
        log.info("⚕ Heal: retesting %d dead proxies…", len(dead_proxies))

        def _run():
            revived = 0
            for proxy in dead_proxies:
                live, latency, via, banner = check_proxy_smtp(
                    proxy, hosts or DEFAULT_TEST_HOSTS, timeout)
                if live:
                    PROXY_POOL.mark_ok(proxy)
                    revived += 1
                    log.info("HEALED  %s:%s  (%dms  %s)",
                             proxy["host"], proxy["port"], latency, via)
                else:
                    log.debug("Still dead  %s:%s  %s",
                              proxy["host"], proxy["port"], banner[:60])
            log.info("⚕ Heal done: %d/%d revived",
                     revived, len(dead_proxies))

        threading.Thread(target=_run, daemon=True).start()

    def _apply_proxy_options(self, *_):
        PROXY_POOL.mode     = self._rotation_var.get()
        PROXY_POOL.fallback = self._fallback_var.get()
        try:
            PROXY_POOL.max_fails = int(self._max_fails_var.get())
            PROXY_POOL.cooldown  = int(self._cooldown_var.get())
        except ValueError:
            pass

    # ── Proxy stats poll ───────────────────────────────────────────────────

    def _poll_proxy_stats(self):
        a, d = PROXY_POOL.stats
        self._px_active_var.set(str(a))
        self._px_dead_var.set(str(d))
        if PROXY_POOL.count == 0:
            self._proxy_stat_var.set("Proxies: none loaded")
        else:
            self._proxy_stat_var.set(
                f"Proxies: {a} active / {d} dead")
        self.after(2000, self._poll_proxy_stats)

    # ── Config ─────────────────────────────────────────────────────────────

    def _save_config(self):
        data = {k: v.get() for k, v in self._setting_vars.items()}
        data.update({k: v.get() for k, v in self._compose_vars.items()})
        data["rotation"] = self._rotation_var.get()
        data["fallback"] = self._fallback_var.get()
        data["max_fails"] = self._max_fails_var.get()
        data["cooldown"]  = self._cooldown_var.get()
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
        messagebox.showinfo("Saved", f"Saved to {CONFIG_FILE}")

    def _load_config(self):
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text())
            for k, v in self._setting_vars.items():
                if k in data:
                    v.set(data[k])
            for k, v in self._compose_vars.items():
                if k in data:
                    v.set(data[k])
            if "rotation"  in data: self._rotation_var.set(data["rotation"])
            if "fallback"  in data: self._fallback_var.set(data["fallback"])
            if "max_fails" in data: self._max_fails_var.set(data["max_fails"])
            if "cooldown"  in data: self._cooldown_var.set(data["cooldown"])
        except Exception as e:
            log.warning("Config load error: %s", e)

    def _reset_settings(self):
        defaults = {"helo": socket.getfqdn(), "port": "25",
                    "timeout": "30", "delay": "1.0",
                    "threads": "3", "retry": "1"}
        for k, v in defaults.items():
            self._setting_vars[k].set(v)

    # ── Logs ───────────────────────────────────────────────────────────────

    def _poll_log_queue(self):
        while not self._log_queue.empty():
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            lvl = "INFO"
            for l in ("ERROR", "WARNING", "DEBUG"):
                if l in msg:
                    lvl = l
                    break
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n", lvl)
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        self.after(200, self._poll_log_queue)

    def _clear_logs(self):
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _save_logs(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if p:
            Path(p).write_text(self._log_box.get("1.0", "end"),
                               encoding="utf-8")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _update_stat_bar(self):
        self._stat_var.set(
            f"Sent: {self._stats['sent']}  Failed: {self._stats['failed']}")

    # ── Session persistence ────────────────────────────────────────────────

    def _save_session(self):
        """Persist all mailer state to SESSION_FILE so it survives termination."""
        try:
            body = self._body_text.get("1.0", "end-1c")
            html = self._html_text.get("1.0", "end-1c")
        except Exception:
            body = html = ""
        try:
            data = {
                "recipients" : list(self._recipients),
                "sent"       : list(self._sent_set),
                "failed"     : list(self._failed_set),
                "body"       : body,
                "html"       : html,
                "attachments": list(self._attachments),
                "proxies"    : PROXY_POOL.to_raw_lines(),
            }
            SESSION_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.debug("Session save failed: %s", e)

    def _restore_session(self):
        """On startup, offer to restore a previously saved session."""
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text())
        except Exception:
            SESSION_FILE.unlink(missing_ok=True)
            return

        recipients = data.get("recipients", [])
        sent       = set(data.get("sent", []))
        unsent     = [r for r in recipients if r not in sent]

        if not recipients:
            SESSION_FILE.unlink(missing_ok=True)
            return

        total  = len(recipients)
        done   = len(sent)
        failed = len(data.get("failed", []))
        msg = (
            f"A previous session was found:\n\n"
            f"  • {total} total recipients\n"
            f"  • {done} already sent\n"
            f"  • {failed} failed\n"
            f"  • {len(unsent)} remaining\n\n"
            f"Restore this session?"
        )
        if not messagebox.askyesno("Restore Session", msg, parent=self):
            SESSION_FILE.unlink(missing_ok=True)
            return

        # Restore body / html
        body = data.get("body", "")
        html = data.get("html", "")
        if body:
            self._body_text.delete("1.0", "end")
            self._body_text.insert("1.0", body)
        if html:
            self._html_text.delete("1.0", "end")
            self._html_text.insert("1.0", html)

        # Restore attachments (skip missing files)
        for att in data.get("attachments", []):
            if att not in self._attachments and os.path.isfile(att):
                self._attachments.append(att)
                self._att_list.insert("end", os.path.basename(att))

        # Restore unsent recipients only
        for r in unsent:
            if r not in self._recipients:
                self._recipients.append(r)
                self._recip_list.insert("end", r)
        self._refresh_recip_count()

        # Restore tracking sets
        self._sent_set   = sent
        self._failed_set = set(data.get("failed", []))

        # Restore proxy pool
        proxy_lines = data.get("proxies", [])
        if proxy_lines:
            PROXY_POOL.load(proxy_lines)
            log.info("Session restored %d proxies to pool", len(proxy_lines))

        log.info("Session restored — %d unsent recipients, %d already sent",
                 len(unsent), done)

    def _on_close(self):
        self._running  = False
        self._checking = False
        self._pause_event.set()       # unblock any waiting threads
        self._chk_pause_event.set()
        self._save_session()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────

def _valid(addr: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr))


if __name__ == "__main__":
    app = DirectMailerApp()
    app.mainloop()
