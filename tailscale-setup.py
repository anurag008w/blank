#!/usr/bin/env python3
"""Download and start Tailscale in userspace-networking mode.

Runs BEFORE GOST so GOST can chain outbound traffic through the
Tailscale SOCKS5 proxy.  No kernel module, no root required — works
inside rootless HuggingFace Spaces containers.

Architecture:
  HF Space → GOST (port 8118) → Tailscale SOCKS5 (port 1055)
             → your exit-node machine → internet

Env vars:
  TAILSCALE_AUTHKEY       — required; reusable ephemeral key from
                            https://login.tailscale.com/admin/settings/keys
  TAILSCALE_HOSTNAME      — Tailscale device name (default: hf-space)
  TAILSCALE_EXIT_NODE     — Tailscale IP of your exit node (100.x.x.x).
                            Leave unset to use Tailscale as a SOCKS5 relay
                            without routing all traffic through an exit node.
  TAILSCALE_SOCKS5_PORT   — local SOCKS5 listen port (default: 1055)
  TAILSCALE_VERSION       — Tailscale release to download (default: 1.98.4)

On success writes /tmp/huggingclaw-tailscale.env:
  export TAILSCALE_SOCKS5_URL="socks5://127.0.0.1:<port>"
  export TAILSCALE_HTTP_URL="http://127.0.0.1:<http_port>"
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

# ── Config from env ────────────────────────────────────────────────────────
TS_VERSION    = os.environ.get("TAILSCALE_VERSION", "1.98.4").strip().lstrip("v")
TS_AUTHKEY    = os.environ.get("TAILSCALE_AUTHKEY", "").strip()
TS_HOSTNAME   = os.environ.get("TAILSCALE_HOSTNAME", "hf-space").strip() or "hf-space"
TS_EXIT_NODE  = os.environ.get("TAILSCALE_EXIT_NODE", "").strip()
TS_SOCKS5_PORT = int(os.environ.get("TAILSCALE_SOCKS5_PORT", "1055").strip() or "1055")
TS_HTTP_PORT   = TS_SOCKS5_PORT + 1   # HTTP proxy one port above SOCKS5

# ── Paths ──────────────────────────────────────────────────────────────────
TS_DIR      = Path("/opt/tailscale")
TS_BIN      = TS_DIR / "tailscale"
TSd_BIN     = TS_DIR / "tailscaled"
TS_SOCKET   = Path("/tmp/tailscaled-hf.sock")
TS_LOG      = Path("/tmp/tailscaled.log")
TS_ENV      = Path("/tmp/huggingclaw-tailscale.env")
TS_PID      = Path("/tmp/huggingclaw-tailscaled.pid")


# ── Helpers ────────────────────────────────────────────────────────────────

def detect_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv"):
        return "arm"
    return "amd64"


def download_tailscale() -> bool:
    arch  = detect_arch()
    fname = f"tailscale_{TS_VERSION}_linux_{arch}.tgz"
    url   = f"https://pkgs.tailscale.com/stable/{fname}"

    print(f"Downloading Tailscale v{TS_VERSION} ({arch})…", flush=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = Path(tmp) / fname
            req = urllib.request.Request(url, headers={"User-Agent": "HuggingClaw/2.0"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tarball, "wb") as f:
                shutil.copyfileobj(r, f)

            # The tarball contains  tailscale_VERSION_arch/{tailscale,tailscaled,...}
            with tarfile.open(tarball, "r:gz") as tar:
                TS_DIR.mkdir(parents=True, exist_ok=True)
                for member in tar.getmembers():
                    bname = Path(member.name).name
                    if bname in ("tailscale", "tailscaled"):
                        data = tar.extractfile(member)
                        if data is None:
                            continue
                        dest = TS_DIR / bname
                        dest.write_bytes(data.read())
                        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        print(f"  extracted → {dest}", flush=True)

        if not TS_BIN.exists() or not TSd_BIN.exists():
            raise RuntimeError("tailscale or tailscaled binary not found in tarball")

        print(f"Tailscale binaries saved to {TS_DIR}", flush=True)
        return True
    except Exception as e:
        print(f"ERROR: Could not download Tailscale: {e}", file=sys.stderr)
        return False


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def wait_for_socket(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.4)
    return False


def tail_log(n: int = 30) -> str:
    try:
        lines = TS_LOG.read_text().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "(log unavailable)"


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    if not TS_AUTHKEY:
        print(
            "TAILSCALE_AUTHKEY is not set — skipping Tailscale setup.",
            flush=True,
        )
        return

    print(
        f"[tailscale] hostname={TS_HOSTNAME}  socks5_port={TS_SOCKS5_PORT}"
        + (f"  exit_node={TS_EXIT_NODE}" if TS_EXIT_NODE else "  exit_node=<none>"),
        flush=True,
    )

    # ── Ensure binaries ────────────────────────────────────────────────────
    if not TS_BIN.exists() or not TSd_BIN.exists():
        if not download_tailscale():
            print(
                "[tailscale] Setup failed — continuing without Tailscale.",
                file=sys.stderr,
            )
            return

    # ── Start tailscaled (daemon) ──────────────────────────────────────────
    # --tun=userspace-networking: no kernel TUN device needed (rootless OK)
    # --socks5-server: SOCKS5 proxy for outbound traffic
    # --outbound-http-proxy-listen: HTTP CONNECT proxy for the same
    # --state=mem: ephemeral — state is not written to disk
    # --socket: explicit socket path so tailscale CLI can reach it
    daemon_cmd = [
        str(TSd_BIN),
        "--tun=userspace-networking",
        f"--socks5-server=localhost:{TS_SOCKS5_PORT}",
        f"--outbound-http-proxy-listen=localhost:{TS_HTTP_PORT}",
        "--state=mem:",
        f"--socket={TS_SOCKET}",
    ]

    print(f"[tailscale] Starting daemon: {' '.join(daemon_cmd)}", flush=True)
    log_fh = TS_LOG.open("a")
    daemon = subprocess.Popen(
        daemon_cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    TS_PID.write_text(str(daemon.pid))

    # Wait for the socket to appear (daemon ready)
    if not wait_for_socket(TS_SOCKET, timeout=15.0):
        print(
            f"[tailscale] ERROR: tailscaled socket did not appear within 15 s.\n"
            f"Log tail:\n{tail_log()}",
            file=sys.stderr,
        )
        return

    # ── Authenticate ───────────────────────────────────────────────────────
    up_cmd = [
        str(TS_BIN),
        f"--socket={TS_SOCKET}",
        "up",
        f"--authkey={TS_AUTHKEY}",
        "--ephemeral",                       # auto-remove device when offline
        f"--hostname={TS_HOSTNAME}",
        "--accept-routes",                   # pick up exit-node routes
    ]
    if TS_EXIT_NODE:
        up_cmd.append(f"--exit-node={TS_EXIT_NODE}")
        up_cmd.append("--exit-node-allow-lan-access=false")

    print(f"[tailscale] Authenticating (hostname={TS_HOSTNAME})…", flush=True)
    try:
        result = subprocess.run(
            up_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(
                f"[tailscale] ERROR: tailscale up failed (rc={result.returncode}):\n"
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
            print(f"Log tail:\n{tail_log()}", file=sys.stderr)
            return
    except subprocess.TimeoutExpired:
        print("[tailscale] ERROR: tailscale up timed out after 60 s.", file=sys.stderr)
        return

    # ── Wait for SOCKS5 to accept connections ──────────────────────────────
    if not wait_for_port("127.0.0.1", TS_SOCKS5_PORT, timeout=15.0):
        print(
            f"[tailscale] ERROR: SOCKS5 proxy did not come up on port {TS_SOCKS5_PORT} "
            f"within 15 s.\nLog tail:\n{tail_log()}",
            file=sys.stderr,
        )
        return

    # ── Write env file for start.sh to source ─────────────────────────────
    socks5_url = f"socks5://127.0.0.1:{TS_SOCKS5_PORT}"
    http_url   = f"http://127.0.0.1:{TS_HTTP_PORT}"

    TS_ENV.write_text(
        f'export TAILSCALE_SOCKS5_URL="{socks5_url}"\n'
        f'export TAILSCALE_HTTP_URL="{http_url}"\n'
    )

    print(
        f"[tailscale] ✓ Connected — SOCKS5 ready at {socks5_url}  "
        f"(pid {daemon.pid})",
        flush=True,
    )
    if TS_EXIT_NODE:
        print(f"[tailscale] ✓ Exit node: {TS_EXIT_NODE}", flush=True)


if __name__ == "__main__":
    main()
