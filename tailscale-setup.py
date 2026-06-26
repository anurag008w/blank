#!/usr/bin/env python3
"""
tailscale-setup.py — HuggingClaw Tailscale userspace proxy setup
─────────────────────────────────────────────────────────────────
Downloads the Tailscale static binary, starts tailscaled in userspace
mode (no root / no TUN device required), authenticates with an auth key,
and writes /tmp/huggingclaw-tailscale.env so start.sh can auto-wire
GOST_UPSTREAM_PROXY to the SOCKS5 listener.

Environment variables read:
  TAILSCALE_AUTHKEY      (required) auth key from admin.tailscale.com/settings/keys
  TAILSCALE_HOSTNAME     hostname shown in Tailscale admin (default: hf-space)
  TAILSCALE_EXIT_NODE    Tailscale IP of an exit node to route through (optional)
  TAILSCALE_SOCKS5_PORT  local SOCKS5 listen port (default: 1055)
  TAILSCALE_HTTP_PORT    local HTTP proxy listen port (default: 1055)
  TAILSCALE_VERSION      pin a specific release (default: 1.98.4)

Environment variables written to /tmp/huggingclaw-tailscale.env:
  TAILSCALE_SOCKS5_URL   e.g. socks5://127.0.0.1:1055
  TAILSCALE_HTTP_URL     e.g. http://127.0.0.1:1055
"""

import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────
DEFAULT_VERSION = "1.98.4"
DOWNLOAD_BASE = "https://pkgs.tailscale.com/stable"
INSTALL_DIR = Path("/tmp/tailscale-bin")
TAILSCALED_BIN = INSTALL_DIR / "tailscaled"
TAILSCALE_BIN = INSTALL_DIR / "tailscale"
SOCKET_PATH = "/tmp/tailscaled-hf.sock"
STATE_DIR = "/tmp/tailscale-state"
ENV_FILE = Path("/tmp/huggingclaw-tailscale.env")


def log(msg: str) -> None:
    print(f"[tailscale] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[tailscale] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def download_tailscale(version: str) -> None:
    """Download and extract the Tailscale static binary archive."""
    arch = "amd64"  # HF Spaces run on x86-64
    archive_name = f"tailscale_{version}_{arch}.tgz"
    url = f"{DOWNLOAD_BASE}/{archive_name}"

    log(f"Downloading Tailscale {version} from {url} ...")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        try:
            urllib.request.urlretrieve(url, tmp.name)
        except Exception as exc:
            die(f"Download failed: {exc}")

        log("Extracting archive...")
        with tarfile.open(tmp.name, "r:gz") as tar:
            # The archive has a top-level directory: tailscale_VERSION_ARCH/
            for member in tar.getmembers():
                basename = Path(member.name).name
                if basename in ("tailscaled", "tailscale"):
                    member.name = basename  # strip directory prefix
                    tar.extract(member, path=str(INSTALL_DIR))

    # Make both binaries executable
    for bin_path in (TAILSCALED_BIN, TAILSCALE_BIN):
        if bin_path.exists():
            bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        else:
            die(f"Expected binary not found after extraction: {bin_path}")

    log(f"Tailscale {version} installed to {INSTALL_DIR}")


def start_tailscaled(socks5_port: int) -> subprocess.Popen:
    """
    Start tailscaled in userspace-networking mode.
    SOCKS5 and HTTP proxy share the same port (standard Tailscale behaviour).
    """
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    cmd = [
        str(TAILSCALED_BIN),
        "--tun=userspace-networking",
        f"--socks5-server=localhost:{socks5_port}",
        f"--outbound-http-proxy-listen=localhost:{socks5_port}",
        f"--socket={SOCKET_PATH}",
        f"--statedir={STATE_DIR}",
    ]
    log(f"Starting tailscaled: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    # Give tailscaled a moment to create its socket
    for _ in range(20):
        if Path(SOCKET_PATH).exists():
            break
        time.sleep(0.25)
    else:
        die(f"tailscaled socket never appeared at {SOCKET_PATH} (pid {proc.pid})")
    log(f"tailscaled running (pid {proc.pid}), socket at {SOCKET_PATH}")
    return proc


def tailscale_up(authkey: str, hostname: str, exit_node: str) -> None:
    """Authenticate and bring up the Tailscale interface."""
    cmd = [
        str(TAILSCALE_BIN),
        f"--socket={SOCKET_PATH}",
        "up",
        f"--auth-key={authkey}",
        "--ephemeral",
        "--accept-routes=false",   # don't pull routes from other nodes
        f"--hostname={hostname}",
    ]
    # Exit node is optional — omitting it means traffic is NOT routed through
    # a specific device (the Space just joins the tailnet for SOCKS5 access).
    if exit_node:
        cmd.append(f"--exit-node={exit_node}")
        cmd.append("--exit-node-allow-lan-access=false")

    log(f"Running tailscale up (hostname={hostname}, exit-node={'none' if not exit_node else exit_node}) ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"tailscale up stderr: {result.stderr.strip()}")
        die(f"tailscale up failed (exit {result.returncode})")
    log("tailscale up succeeded")


def wait_connected(timeout: int = 30) -> bool:
    """Poll tailscale status until it reports a BackendState of Running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [str(TAILSCALE_BIN), f"--socket={SOCKET_PATH}", "status", "--json"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            import json
            try:
                data = json.loads(result.stdout)
                state = data.get("BackendState", "")
                if state == "Running":
                    return True
                if state not in ("Starting", "NoState", ""):
                    log(f"Unexpected BackendState: {state!r}")
            except json.JSONDecodeError:
                pass
        time.sleep(1)
    return False


def write_env(socks5_port: int) -> None:
    """Write env file consumed by start.sh."""
    socks5_url = f"socks5://127.0.0.1:{socks5_port}"
    http_url = f"http://127.0.0.1:{socks5_port}"
    ENV_FILE.write_text(
        f"export TAILSCALE_SOCKS5_URL={socks5_url}\n"
        f"export TAILSCALE_HTTP_URL={http_url}\n"
    )
    log(f"✓ Connected — SOCKS5 ready at {socks5_url}")
    log(f"  HTTP proxy ready at {http_url}")
    log(f"  env file written to {ENV_FILE}")


def main() -> None:
    authkey = os.environ.get("TAILSCALE_AUTHKEY", "").strip()
    if not authkey:
        die("TAILSCALE_AUTHKEY is not set — skipping Tailscale setup")

    hostname = os.environ.get("TAILSCALE_HOSTNAME", "hf-space").strip() or "hf-space"
    exit_node = os.environ.get("TAILSCALE_EXIT_NODE", "").strip()
    version = os.environ.get("TAILSCALE_VERSION", DEFAULT_VERSION).strip() or DEFAULT_VERSION

    try:
        socks5_port = int(os.environ.get("TAILSCALE_SOCKS5_PORT", "1055"))
    except ValueError:
        socks5_port = 1055

    # Skip download if binaries are already present (e.g. container restart)
    if not TAILSCALED_BIN.exists() or not TAILSCALE_BIN.exists():
        download_tailscale(version)
    else:
        log(f"Tailscale binaries already present at {INSTALL_DIR}, skipping download")

    start_tailscaled(socks5_port)
    tailscale_up(authkey, hostname, exit_node)

    log("Waiting for Tailscale to reach Running state...")
    if not wait_connected(timeout=45):
        log("WARNING: Tailscale did not reach Running state within 45 s — continuing anyway")
    else:
        log("Tailscale is Running ✓")

    write_env(socks5_port)


if __name__ == "__main__":
    main()
