#!/usr/bin/env python3
"""
DirectMailer CLI — Direct SMTP sender for Linux / Kali VPS
Same engine as the GUI version, runs in terminal.
"""

import smtplib
import socket
import os
import sys
import csv
import time
import threading
import queue
import re
import argparse
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate, make_msgid
from pathlib import Path

# ── colour helpers ────────────────────────────────────────────────────────────
class C:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"

def ok(msg):    print(f"{C.GREEN}[+]{C.RESET} {msg}")
def err(msg):   print(f"{C.RED}[-]{C.RESET} {msg}")
def info(msg):  print(f"{C.CYAN}[*]{C.RESET} {msg}")
def warn(msg):  print(f"{C.YELLOW}[!]{C.RESET} {msg}")

# ── optional DNS ──────────────────────────────────────────────────────────────
try:
    import dns.resolver
    DNS_OK = True
except ImportError:
    DNS_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  MX LOOKUP
# ─────────────────────────────────────────────────────────────────────────────

def resolve_mx(domain: str) -> list:
    if DNS_OK:
        try:
            answers = dns.resolver.resolve(domain, "MX")
            return [str(r.exchange).rstrip(".")
                    for r in sorted(answers, key=lambda x: x.preference)]
        except Exception:
            pass
    # Fallback: host / nslookup
    for cmd in [["host", "-t", "MX", domain],
                ["nslookup", "-type=MX", domain]]:
        try:
            import subprocess
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                          timeout=10, text=True)
            hosts = []
            for line in out.splitlines():
                line_l = line.lower()
                if "mail exchanger" in line_l or "mail host" in line_l:
                    parts = line.split()
                    hosts.append(parts[-1].rstrip("."))
            if hosts:
                return hosts
        except Exception:
            pass
    return [f"mail.{domain}"]


# ─────────────────────────────────────────────────────────────────────────────
#  MESSAGE BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_message(sender, sender_name, recipient,
                  subject, body, html_body, attachments):
    msg = MIMEMultipart("mixed")
    display = f"{sender_name} <{sender}>" if sender_name else sender
    msg["From"]       = display
    msg["To"]         = recipient
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["X-Mailer"]   = "DirectMailer-CLI/1.0"

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain", "utf-8"))
    if html_body and html_body.strip():
        alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    for path in attachments:
        if not os.path.isfile(path):
            warn(f"Attachment not found, skipping: {path}")
            continue
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(path))
        msg.attach(part)

    return msg


# ─────────────────────────────────────────────────────────────────────────────
#  DIRECT SMTP DELIVERY
# ─────────────────────────────────────────────────────────────────────────────

def direct_send(sender, recipient, msg,
                timeout=30, helo_name="", port=25, retries=1):
    domain  = recipient.split("@")[-1]
    mx_list = resolve_mx(domain)
    my_host = helo_name or socket.getfqdn()

    for attempt in range(retries + 1):
        if attempt:
            info(f"  Retry {attempt} for {recipient}…")
            time.sleep(2)
        for mx in mx_list:
            try:
                with smtplib.SMTP(mx, port, local_hostname=my_host,
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
                return True, f"Delivered via {mx}"
            except smtplib.SMTPRecipientsRefused as e:
                last_err = f"Recipient refused: {e}"
            except smtplib.SMTPSenderRefused as e:
                last_err = f"Sender refused: {e}"
            except smtplib.SMTPException as e:
                last_err = f"SMTP error ({mx}): {e}"
            except (socket.timeout, ConnectionRefusedError, OSError) as e:
                last_err = f"Connection ({mx}): {e}"

    return False, last_err


# ─────────────────────────────────────────────────────────────────────────────
#  BULK ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def bulk_send(sender, sender_name, subject, body, html_body,
              attachments, recipients, delay=1.0, threads=3,
              timeout=30, helo="", port=25, retries=1, log_file=None):

    total   = len(recipients)
    sent    = [0]
    failed  = [0]
    lock    = threading.Lock()
    q: queue.Queue = queue.Queue()
    failed_list = []

    for r in recipients:
        q.put(r)

    log_fh = open(log_file, "w") if log_file else None

    def log_result(recipient, success, status):
        line = f"{'OK  ' if success else 'FAIL'}  {recipient}  {status}"
        if success:
            ok(line)
        else:
            err(line)
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

    def worker():
        while True:
            try:
                recip = q.get_nowait()
            except queue.Empty:
                break
            msg = build_message(sender, sender_name, recip,
                                subject, body, html_body, attachments)
            success, status = direct_send(sender, recip, msg,
                                          timeout=timeout, helo_name=helo,
                                          port=port, retries=retries)
            with lock:
                if success:
                    sent[0] += 1
                else:
                    failed[0] += 1
                    failed_list.append(recip)
                done = sent[0] + failed[0]
                pct  = int(done / total * 100)
                bar  = ("█" * (pct // 5)).ljust(20)
                print(f"\r  [{bar}] {pct:3d}%  {done}/{total}  "
                      f"{C.GREEN}✓{sent[0]}{C.RESET}  "
                      f"{C.RED}✗{failed[0]}{C.RESET}    ",
                      end="", flush=True)

            log_result(recip, success, status)
            time.sleep(delay)

    sem = threading.Semaphore(threads)

    def guarded_worker():
        sem.acquire()
        try:
            worker()
        finally:
            sem.release()

    thread_list = [threading.Thread(target=guarded_worker, daemon=True)
                   for _ in range(threads)]
    for t in thread_list:
        t.start()
    for t in thread_list:
        t.join()

    print()  # newline after progress bar
    if log_fh:
        log_fh.close()
    return sent[0], failed[0], failed_list


# ─────────────────────────────────────────────────────────────────────────────
#  INTERACTIVE MENU
# ─────────────────────────────────────────────────────────────────────────────

def banner():
    print(f"""
{C.BOLD}{C.BLUE}
  ╔══════════════════════════════════════╗
  ║       DirectMailer CLI v1.0          ║
  ║   Direct SMTP · No relay · No auth   ║
  ╚══════════════════════════════════════╝
{C.RESET}""")

def prompt(label, default=""):
    suffix = f" [{default}]" if default else ""
    val = input(f"  {C.CYAN}{label}{suffix}:{C.RESET} ").strip()
    return val if val else default

def prompt_multiline(label):
    print(f"  {C.CYAN}{label}{C.RESET} (type END on a new line to finish):")
    lines = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines)

def load_recipients(path):
    recipients = []
    path = path.strip().strip("'\"")
    if not os.path.isfile(path):
        err(f"File not found: {path}")
        return []
    ext = Path(path).suffix.lower()
    with open(path, newline="", encoding="utf-8-sig") as f:
        if ext == ".csv":
            for row in csv.reader(f):
                if row:
                    e = row[0].strip()
                    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
                        recipients.append(e)
        else:
            for line in f:
                e = line.strip().strip(",;")
                if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
                    recipients.append(e)
    return recipients


def interactive_mode():
    banner()

    if not DNS_OK:
        warn("dnspython not installed. MX resolution uses fallback.")
        warn("  Run: pip3 install dnspython")
        print()

    print(f"{C.BOLD}── Sender ───────────────────────────────{C.RESET}")
    sender      = prompt("From address")
    sender_name = prompt("From name (optional)")
    print()

    print(f"{C.BOLD}── Delivery settings ────────────────────{C.RESET}")
    helo    = prompt("EHLO hostname", socket.getfqdn())
    timeout = int(prompt("Connection timeout (s)", "30"))
    retries = int(prompt("Retries on fail", "1"))
    port    = int(prompt("SMTP port", "25"))
    print()

    print(f"{C.BOLD}── Mode ─────────────────────────────────{C.RESET}")
    print("  1) Single send")
    print("  2) Bulk send (CSV / TXT file)")
    print("  3) Bulk send (paste list)")
    mode = prompt("Choose", "1")
    print()

    recipients = []
    if mode == "1":
        recipients = [prompt("To address")]
    elif mode == "2":
        path = prompt("Path to CSV / TXT file")
        recipients = load_recipients(path)
        if not recipients:
            err("No valid recipients loaded.")
            return
        ok(f"Loaded {len(recipients)} recipients")
    elif mode == "3":
        print(f"  {C.CYAN}Paste emails (one per line), blank line to finish:{C.RESET}")
        while True:
            line = input().strip().strip(",;")
            if not line:
                break
            if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", line):
                recipients.append(line)
        ok(f"Added {len(recipients)} recipients")
    print()

    print(f"{C.BOLD}── Message ───────────────────────────────{C.RESET}")
    subject   = prompt("Subject")
    body      = prompt_multiline("Plain text body")
    use_html  = prompt("Add HTML version? (y/n)", "n").lower() == "y"
    html_body = prompt_multiline("HTML body") if use_html else ""
    print()

    print(f"{C.BOLD}── Attachments ──────────────────────────{C.RESET}")
    attachments = []
    while True:
        att = prompt("Attachment path (leave blank to skip/finish)")
        if not att:
            break
        if os.path.isfile(att):
            attachments.append(att)
            ok(f"Added: {att}")
        else:
            err(f"Not found: {att}")
    print()

    # Bulk options
    delay   = 1.0
    threads = 1
    log_file = None
    if len(recipients) > 1:
        print(f"{C.BOLD}── Bulk options ─────────────────────────{C.RESET}")
        delay    = float(prompt("Delay between sends (s)", "1.0"))
        threads  = int(prompt("Parallel threads", "3"))
        log_file = prompt("Log file path (optional)")
        log_file = log_file if log_file else None
        print()

    # Confirm
    print(f"{C.BOLD}── Summary ───────────────────────────────{C.RESET}")
    info(f"From:        {sender_name + ' ' if sender_name else ''}<{sender}>")
    info(f"Recipients:  {len(recipients)}")
    info(f"Subject:     {subject}")
    info(f"Attachments: {len(attachments)}")
    info(f"Port:        {port}   Threads: {threads}   Delay: {delay}s")
    print()

    go = prompt("Start sending? (y/n)", "y").lower()
    if go != "y":
        warn("Aborted.")
        return

    print()
    info("Sending…")
    print()

    if len(recipients) == 1:
        msg = build_message(sender, sender_name, recipients[0],
                            subject, body, html_body, attachments)
        success, status = direct_send(sender, recipients[0], msg,
                                      timeout=timeout, helo_name=helo,
                                      port=port, retries=retries)
        if success:
            ok(f"Delivered → {recipients[0]}  ({status})")
        else:
            err(f"Failed → {recipients[0]}  ({status})")
    else:
        sent, failed, failed_list = bulk_send(
            sender, sender_name, subject, body, html_body,
            attachments, recipients,
            delay=delay, threads=threads, timeout=timeout,
            helo=helo, port=port, retries=retries, log_file=log_file
        )
        print()
        ok(f"Done — Sent: {sent}  Failed: {failed}")
        if failed_list:
            warn("Failed addresses:")
            for a in failed_list:
                print(f"    {C.RED}{a}{C.RESET}")


# ─────────────────────────────────────────────────────────────────────────────
#  CLI ARG MODE (non-interactive / scriptable)
# ─────────────────────────────────────────────────────────────────────────────

def arg_mode(args):
    recipients = []
    if args.to:
        recipients = [args.to]
    if args.list:
        recipients = load_recipients(args.list)
    if not recipients:
        err("No recipients. Use --to or --list")
        sys.exit(1)

    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")

    html = ""
    if args.html_file:
        html = Path(args.html_file).read_text(encoding="utf-8")

    attachments = args.attach or []

    info(f"Sending to {len(recipients)} recipient(s)…")

    if len(recipients) == 1:
        msg = build_message(args.sender, args.name or "", recipients[0],
                            args.subject, body, html, attachments)
        success, status = direct_send(args.sender, recipients[0], msg,
                                      timeout=args.timeout, helo_name=args.helo,
                                      port=args.port, retries=args.retry)
        if success:
            ok(f"{recipients[0]} — {status}")
            sys.exit(0)
        else:
            err(f"{recipients[0]} — {status}")
            sys.exit(1)
    else:
        sent, failed, _ = bulk_send(
            args.sender, args.name or "", args.subject, body, html,
            attachments, recipients,
            delay=args.delay, threads=args.threads, timeout=args.timeout,
            helo=args.helo, port=args.port, retries=args.retry,
            log_file=args.log
        )
        ok(f"Done — Sent: {sent}  Failed: {failed}")
        sys.exit(1 if failed > 0 else 0)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="DirectMailer CLI — direct SMTP delivery, no relay",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  # Interactive menu
  python3 direct_mailer_cli.py

  # Single send
  python3 direct_mailer_cli.py -s you@domain.com -t target@proton.me \\
      --subject "Hello" --body "Test message"

  # Bulk from CSV, 5 threads, 2s delay
  python3 direct_mailer_cli.py -s you@domain.com --list emails.csv \\
      --subject "Hi" --body-file body.txt --threads 5 --delay 2 --log results.txt
"""
    )
    # Required only in arg mode
    parser.add_argument("-s", "--sender",  help="From address")
    parser.add_argument("-t", "--to",      help="Single recipient")
    parser.add_argument("--list",          help="CSV or TXT file of recipients")
    parser.add_argument("--subject",       help="Email subject", default="")
    parser.add_argument("--body",          help="Plain text body")
    parser.add_argument("--body-file",     help="Plain text body from file")
    parser.add_argument("--html-file",     help="HTML body from file")
    parser.add_argument("--attach",        help="Attachment path(s)", nargs="*")
    parser.add_argument("--name",          help="Sender display name")
    parser.add_argument("--helo",          help="EHLO hostname", default="")
    parser.add_argument("--port",          help="SMTP port", type=int, default=25)
    parser.add_argument("--timeout",       help="Timeout (s)", type=int, default=30)
    parser.add_argument("--delay",         help="Delay between sends (s)",
                        type=float, default=1.0)
    parser.add_argument("--threads",       help="Parallel threads", type=int, default=3)
    parser.add_argument("--retry",         help="Retries on fail", type=int, default=1)
    parser.add_argument("--log",           help="Log file path")

    args = parser.parse_args()

    # If no args given → interactive menu
    if len(sys.argv) == 1:
        interactive_mode()
    else:
        if not args.sender:
            parser.error("--sender (-s) is required in arg mode")
        arg_mode(args)


if __name__ == "__main__":
    main()
