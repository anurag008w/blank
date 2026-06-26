#!/usr/bin/env python3
"""Download and start a local GOST v3 HTTP proxy.

Replaces cloudflare-proxy-setup.py — no Cloudflare account needed.
GOST runs locally as an HTTP CONNECT proxy; gost-proxy.js routes
selected domains through it transparently at the Node.js level.

Env vars:
  GOST_UPSTREAM_PROXY  — optional upstream: socks5://host:port, http://host:port,
                         socks5://user:pass@host:port, etc.
                         If not set, GOST routes directly (no upstream chaining).
  GOST_PROXY_PORT      — local listen port (default: 8118)
  GOST_PROXY_DEBUG     — "true" for verbose GOST logging
  GOST_VERSION         — GOST release to download (default: 3.2.6)

On success writes /tmp/huggingclaw-gost-proxy.env:
  export GOST_PROXY_URL="http://127.0.0.1:<port>"
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# ── Config from env ────────────────────────────────────────────────────────
GOST_VERSION         = os.environ.get("GOST_VERSION", "3.2.6").strip().lstrip("v")
GOST_PROXY_PORT      = int(os.environ.get("GOST_PROXY_PORT", "8118").strip() or "8118")
GOST_UPSTREAM_PROXY  = os.environ.get("GOST_UPSTREAM_PROXY", "").strip()
GOST_PROXY_DEBUG     = os.environ.get("GOST_PROXY_DEBUG", "false").strip().lower() in (
    "true", "1", "yes", "on"
)

# ── Paths ──────────────────────────────────────────────────────────────────
# Prefer the baked-in binary; fall back to downloaded copy for dev environments
_SYSTEM_GOST = Path("/usr/local/bin/gost")
_OPT_GOST    = Path("/opt/gost")
GOST_BINARY  = _SYSTEM_GOST if _SYSTEM_GOST.exists() else _OPT_GOST
GOST_LOG    = Path("/tmp/gost.log")
GOST_ENV    = Path("/tmp/huggingclaw-gost-proxy.env")
GOST_PID    = Path("/tmp/huggingclaw-gost.pid")


# ── Helpers ────────────────────────────────────────────────────────────────

def detect_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv"):
        return "armv6"
    return "amd64"


def download_gost() -> bool:
    arch = detect_arch()
    fname = f"gost_{GOST_VERSION}_linux_{arch}.tar.gz"
    url   = f"https://github.com/go-gost/gost/releases/download/v{GOST_VERSION}/{fname}"

    print(f"Downloading GOST v{GOST_VERSION} ({arch})…", flush=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = Path(tmp) / fname
            req = urllib.request.Request(url, headers={"User-Agent": "HuggingClaw/2.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tarball, "wb") as f:
                shutil.copyfileobj(r, f)

            with tarfile.open(tarball, "r:gz") as tar:
                member = next(
                    (m for m in tar.getmembers() if Path(m.name).name == "gost"),
                    None,
                )
                if not member:
                    raise RuntimeError("gost binary not found in release tarball")
                data = tar.extractfile(member)
                if data is None:
                    raise RuntimeError("could not extract gost from tarball")
                GOST_BINARY.write_bytes(data.read())

        GOST_BINARY.chmod(GOST_BINARY.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"GOST saved to {GOST_BINARY}", flush=True)
        return True
    except Exception as e:
        print(f"ERROR: Could not download GOST: {e}", file=sys.stderr)
        return False


def parse_upstream(url: str):
    """Return (scheme, host, port, user, password) from a proxy URL."""
    if "://" not in url:
        url = "socks5://" + url
    p = urlparse(url)
    scheme = (p.scheme or "socks5").lower()
    host   = p.hostname or ""
    default_port = 1080 if "socks" in scheme else 8080
    port   = p.port or default_port
    return scheme, host, port, p.username or "", p.password or ""


def gost_connector_type(scheme: str) -> str:
    return {
        "socks5":  "socks5",
        "socks5h": "socks5",
        "socks4":  "socks4",
        "socks4a": "socks4a",
        "http":    "http",
        "https":   "http",
    }.get(scheme, "socks5")


def build_cmd() -> list[str]:
    """
    Build GOST command-line args.
    GOST v3 CLI: gost -L <listener> [-F <forwarder>]

      Listener:  http://:8118          (plain HTTP proxy, no auth)
      Forwarder: socks5://host:port    (upstream to chain through)
                 http://user:pass@host:port
    """
    listener = f"http://127.0.0.1:{GOST_PROXY_PORT}"  # localhost only

    cmd = [str(GOST_BINARY), "-L", listener]

    if GOST_UPSTREAM_PROXY:
        scheme, host, port, user, pwd = parse_upstream(GOST_UPSTREAM_PROXY)
        ct = gost_connector_type(scheme)
        if user and pwd:
            forwarder = f"{ct}://{user}:{pwd}@{host}:{port}"
        else:
            forwarder = f"{ct}://{host}:{port}"
        cmd += ["-F", forwarder]

    if GOST_PROXY_DEBUG:
        cmd += ["-D"]          # GOST v3 debug flag

    return cmd


def wait_for_port(host: str, port: int, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.35)
    return False


def tail_log(n: int = 25) -> str:
    try:
        lines = GOST_LOG.read_text().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "(log unavailable)"


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    if GOST_UPSTREAM_PROXY:
        scheme, host, port, user, _ = parse_upstream(GOST_UPSTREAM_PROXY)
        display = f"{scheme}://{host}:{port}" + (" (authenticated)" if user else "")
        print(f"GOST upstream: {display}", flush=True)
    else:
        print("GOST_UPSTREAM_PROXY not set — GOST will route directly (no upstream).", flush=True)

    # ── Ensure binary ──────────────────────────────────────────────────────
    if not GOST_BINARY.exists():
        if not download_gost():
            print("GOST setup failed — continuing without outbound proxy.", file=sys.stderr)
            return

    # ── Build command & start ──────────────────────────────────────────────
    cmd = build_cmd()
    print(f"Starting: {' '.join(cmd)}", flush=True)

    log_fh = GOST_LOG.open("a")
    proc   = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    GOST_PID.write_text(str(proc.pid))

    # ── Wait for proxy to accept connections ───────────────────────────────
    if wait_for_port("127.0.0.1", GOST_PROXY_PORT, timeout=12.0):
        proxy_url = f"http://127.0.0.1:{GOST_PROXY_PORT}"
        print(f"GOST ready → {proxy_url}  (pid {proc.pid})", flush=True)
        GOST_ENV.write_text(
            f'export GOST_PROXY_URL="{proxy_url}"\n'
            f'export GOST_PROXY_PORT="{GOST_PROXY_PORT}"\n'
        )
    else:
        print(
            f"ERROR: GOST did not start within 12 s on port {GOST_PROXY_PORT}.",
            file=sys.stderr,
        )
        print(f"Log tail:\n{tail_log()}", file=sys.stderr)
        # Don't sys.exit(1) — let the rest of startup continue without proxy


if __name__ == "__main__":
    main()
