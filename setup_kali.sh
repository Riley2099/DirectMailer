#!/bin/bash
# DirectMailer — Kali VPS setup
echo "========================================"
echo "  DirectMailer CLI — Kali Setup"
echo "========================================"

# Python3 check
python3 --version >/dev/null 2>&1 || { echo "[-] python3 not found"; exit 1; }

# pip
pip3 install dnspython --quiet && echo "[+] dnspython installed" \
    || echo "[!] dnspython failed — MX fallback will be used"

echo ""
echo "[+] Setup done."
echo ""
echo "  Run interactive menu:"
echo "    python3 direct_mailer_cli.py"
echo ""
echo "  Single send example:"
echo "    python3 direct_mailer_cli.py -s you@domain.com -t target@proton.me \\"
echo "        --subject 'Hello' --body 'Test'"
echo ""
echo "  Bulk send example:"
echo "    python3 direct_mailer_cli.py -s you@domain.com --list emails.csv \\"
echo "        --subject 'Hello' --body-file body.txt --threads 3 --delay 1"
echo "========================================"
