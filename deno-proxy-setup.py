#!/usr/bin/env python3
"""Deploy or reuse a Deno Deploy worker for outbound proxy.

Replaces cloudflare-proxy-setup.py — uses the Deno Deploy v2 REST API
(api.deno.com/v2) instead of Cloudflare Workers.

Token setup  : console.deno.com → your org → Settings → Access Tokens
               Tokens start with 'ddo_'. Old dash.deno.com tokens (dd...)
               will NOT work — those are for the legacy v1 API being shut
               down July 20, 2026.

Env vars consumed:
  DENO_DEPLOY_TOKEN      — v2 access token (ddo_...). Required for auto-deploy.
  DENO_PROJECT_NAME      — Optional: override the auto-derived app slug.
  CLOUDFLARE_PROXY_URL   — If already set, skip deployment and just write
                           the env file (useful when you pre-deployed manually).
  CLOUDFLARE_PROXY_SECRET — Shared secret baked into the worker.  Auto-
                           generated on first run if absent.
  CLOUDFLARE_PROXY_DOMAINS — Comma-separated extra domains to proxy, or '*'
                             to allow any host.

Env vars written to ENV_FILE (sourced by start.sh):
  CLOUDFLARE_PROXY_URL     — HTTPS URL of the deployed Deno worker
  CLOUDFLARE_PROXY_SECRET  — Shared secret for the x-proxy-key header
"""

from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.deno.com/v2"
ENV_FILE = Path("/tmp/huggingclaw-cloudflare-proxy.env")

# Domains proxied by default — keep in sync with cloudflare-proxy.js defaults.
DEFAULT_ALLOWED: list[str] = [
    # Messaging & social (geo-restricted on HF Spaces)
    "api.telegram.org",
    "discord.com",
    "discordapp.com",
    "gateway.discord.gg",
    "status.discord.com",
    "web.whatsapp.com",
    "whatsapp.com",
    "whatsapp.net",
    # Social — confirmed/likely blocked by HF firewall
    "graph.facebook.com",
    "graph.instagram.com",
    "api.twitter.com",
    "api.x.com",
    "upload.twitter.com",
    "api.linkedin.com",
    "www.linkedin.com",
    "open.tiktokapis.com",
    "oauth.reddit.com",
    # Video
    "youtube.com",
    "www.youtube.com",
    # Email HTTP APIs (SMTP ports are blocked on HF Spaces)
    "api.resend.com",
    "api.sendgrid.com",
    "api.mailgun.net",
    # Google
    "googleapis.com",
    "google.com",
    "googleusercontent.com",
    "gstatic.com",
    # AI providers with per-IP rate limits — proxy gives dedicated edge IP.
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "openrouter.ai",
    "integrate.api.nvidia.com",
    "api.nvidia.com",
    # NOTE: api.openai.com, api.anthropic.com, api.deepseek.com etc. are NOT
    # proxied by default. Add via CLOUDFLARE_PROXY_DOMAINS if needed.
]


# ── HTTP helper ─────────────────────────────────────────────────────────────

def deno_request(
    method: str,
    path: str,
    token: str,
    body: bytes | None = None,
    content_type: str = "application/json",
    expected_codes: tuple[int, ...] = (200, 201, 202),
) -> dict:
    """Make a Deno Deploy v2 API request with up to 3 retries on transient errors."""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        req = urllib.request.Request(
            f"{API_BASE}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            # 409 = already exists — caller handles this
            raise RuntimeError(f"Deno API HTTP {e.code}: {detail[:400]}")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as error:
            last_error = error
            if attempt == 3:
                break
            print(
                f"Deno API {method} {path} failed transiently "
                f"({error}); retrying {attempt}/2...",
                file=sys.stderr,
            )
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Deno API request failed after 3 attempts: {last_error}")


# ── Naming helpers ───────────────────────────────────────────────────────────

def slugify(value: str, max_len: int = 32) -> str:
    """Convert a string to a Deno-compatible app slug (3–32 chars, [a-z0-9-])."""
    # Replace invalid chars with hyphens
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    # Collapse consecutive hyphens
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = "hc-proxy"
    # Deno slugs must be 3–32 chars
    result = cleaned[:max_len].rstrip("-")
    if len(result) < 3:
        result = (result + "hcp")[:3]
    return result


def derive_app_slug() -> str:
    explicit = os.environ.get("DENO_PROJECT_NAME", "").strip()
    if explicit:
        return slugify(explicit)
    space_host = os.environ.get("SPACE_HOST", "").strip()
    if space_host:
        base = space_host.replace(".hf.space", "")
        return slugify(f"{base}-proxy")
    author = os.environ.get("SPACE_AUTHOR_NAME", "").strip()
    repo = os.environ.get("SPACE_REPO_NAME", "").strip()
    if author and repo:
        return slugify(f"{author}-{repo}-prx")
    return "hc-proxy"


# ── Worker source code ───────────────────────────────────────────────────────

def render_worker(secret_value: str, allowed_targets: list[str], allow_proxy_all: bool) -> str:
    """Generate the Deno Deploy worker script using Deno.serve() (v2 runtime)."""
    allowed_json = json.dumps(allowed_targets)
    allow_all_js = "true" if allow_proxy_all else "false"
    secret_json = json.dumps(secret_value)
    return f"""// HuggingClaw Deno Deploy proxy worker
// Generated by deno-proxy-setup.py — do not edit manually.

const PROXY_SHARED_SECRET = {secret_json};
const ALLOW_PROXY_ALL = {allow_all_js};
const ALLOWED_TARGETS = {allowed_json};

function isAllowedHost(hostname) {{
  const normalized = String(hostname || "").trim().toLowerCase();
  if (!normalized) return false;
  if (ALLOW_PROXY_ALL) return true;
  return ALLOWED_TARGETS.some(
    (domain) => normalized === domain || normalized.endsWith(`.${{domain}}`),
  );
}}

async function handleRequest(request) {{
  const url = new URL(request.url);
  const queryTarget = url.searchParams.get("proxy_target");
  const targetHost = request.headers.get("x-target-host") || queryTarget;

  if (PROXY_SHARED_SECRET) {{
    const provided =
      request.headers.get("x-proxy-key") ||
      url.searchParams.get("proxy_key") ||
      "";
    if (provided !== PROXY_SHARED_SECRET) {{
      // Allow Telegram bot paths through even without a key (legacy compat)
      if (!(url.pathname.startsWith("/bot") && !targetHost)) {{
        return new Response("Unauthorized: Invalid proxy key", {{ status: 401 }});
      }}
    }}
  }}

  let targetBase = "";
  if (targetHost) {{
    if (!isAllowedHost(targetHost)) {{
      return new Response(
        `Forbidden: Host ${{targetHost}} is not in the allowed list.`,
        {{ status: 403 }},
      );
    }}
    targetBase = `https://${{targetHost}}`;
  }} else if (url.pathname.startsWith("/bot")) {{
    // Telegram shorthand: /bot<token>/... maps to api.telegram.org
    targetBase = "https://api.telegram.org";
  }} else {{
    return new Response("Invalid request: No target host provided.", {{ status: 400 }});
  }}

  const cleanSearch = new URLSearchParams(url.search);
  cleanSearch.delete("proxy_target");
  cleanSearch.delete("proxy_key");
  const searchStr = cleanSearch.toString();
  const targetUrl = targetBase + url.pathname + (searchStr ? `?${{searchStr}}` : "");

  const headers = new Headers(request.headers);
  // Strip hop-by-hop / proxy-specific headers before forwarding
  for (const h of ["host", "x-real-ip", "x-target-host", "x-proxy-key",
                    "x-forwarded-for", "x-forwarded-proto",
                    "cf-connecting-ip", "cf-ray", "cf-visitor"]) {{
    headers.delete(h);
  }}

  // WebSocket upgrades must NOT carry body/redirect — Deno passes them through transparently.
  const isWS = (request.headers.get("Upgrade") || "").toLowerCase() === "websocket";
  const fetchInit = isWS
    ? {{ method: request.method, headers }}
    : {{ method: request.method, headers, body: request.body, redirect: "follow" }};

  try {{
    return await fetch(new Request(targetUrl, fetchInit));
  }} catch (err) {{
    return new Response(`Proxy Error: ${{err.message}}`, {{ status: 502 }});
  }}
}}

Deno.serve((req) => handleRequest(req));
"""


# ── Deno Deploy API operations ───────────────────────────────────────────────

def get_or_create_app(token: str, slug: str) -> str:
    """
    Return the app slug (same as `slug` if newly created, or existing slug).
    Raises RuntimeError on unrecoverable failure.
    """
    # Try to create the app first
    create_payload = json.dumps({
        "slug": slug,
        "config": {
            "runtime": {
                "type": "dynamic",
                "entrypoint": "main.ts",
            },
        },
    }).encode()

    try:
        result = deno_request("POST", "/apps", token, body=create_payload)
        actual_slug = result.get("slug", slug)
        print(f"Created Deno Deploy app '{actual_slug}'", file=sys.stderr)
        return actual_slug
    except RuntimeError as e:
        msg = str(e)
        if "409" not in msg and "conflict" not in msg.lower() and "already" not in msg.lower():
            raise

    # App already exists — verify by fetching it
    try:
        existing = deno_request("GET", f"/apps/{slug}", token)
        actual_slug = existing.get("slug", slug)
        print(f"Using existing Deno Deploy app '{actual_slug}'", file=sys.stderr)
        return actual_slug
    except RuntimeError:
        pass

    # Try with numeric suffix if base slug is taken by someone else
    for i in range(1, 6):
        alt = slugify(f"{slug}-{i}")
        try:
            result = deno_request(
                "POST", "/apps", token,
                body=json.dumps({
                    "slug": alt,
                    "config": {"runtime": {"type": "dynamic", "entrypoint": "main.ts"}},
                }).encode(),
            )
            actual_slug = result.get("slug", alt)
            print(
                f"Created Deno Deploy app '{actual_slug}' "
                f"(base name '{slug}' was taken by another org).",
                file=sys.stderr,
            )
            return actual_slug
        except RuntimeError:
            continue

    raise RuntimeError(
        f"Could not create or find Deno Deploy app with slug '{slug}'. "
        "Set DENO_PROJECT_NAME to a unique value (3–32 chars, [a-z0-9-]) and retry."
    )


def deploy_worker(token: str, app_slug: str, worker_source: str) -> str:
    """Deploy worker source to the app. Returns the revision ID."""
    payload = json.dumps({
        "assets": {
            "main.ts": {
                "kind": "file",
                "content": worker_source,
                "encoding": "utf-8",
            },
        },
        "config": {
            "runtime": {
                "type": "dynamic",
                "entrypoint": "main.ts",
            },
        },
    }).encode()

    result = deno_request("POST", f"/apps/{app_slug}/deploy", token, body=payload)
    revision_id = result.get("id", "")
    if not revision_id:
        raise RuntimeError(
            f"Deno deploy endpoint returned unexpected response (no 'id'): "
            f"{json.dumps(result)[:200]}"
        )
    return revision_id


def wait_for_revision(token: str, revision_id: str, timeout: int = 120) -> str | None:
    """
    Poll until revision status is 'succeeded' or a terminal failure.
    Returns the preview_url if available, else None.
    """
    deadline = time.monotonic() + timeout
    interval = 3
    while time.monotonic() < deadline:
        try:
            rev = deno_request("GET", f"/revisions/{revision_id}", token)
        except RuntimeError:
            time.sleep(interval)
            continue

        status = rev.get("status", "")
        if status == "succeeded":
            # Try common URL field names
            url = (
                rev.get("preview_url")
                or rev.get("url")
                or rev.get("deployment_url")
            )
            return url
        if status in ("failed", "skipped", "cancelled", "errored"):
            reason = rev.get("failure_reason") or rev.get("error") or status
            raise RuntimeError(
                f"Deno Deploy revision {revision_id} failed with status '{status}': {reason}"
            )
        # Still building — keep polling
        time.sleep(interval)
        interval = min(interval + 1, 10)

    print(
        f"Warning: revision {revision_id} did not reach 'succeeded' within "
        f"{timeout}s — proceeding anyway.",
        file=sys.stderr,
    )
    return None


def get_app_url(token: str, app_slug: str, revision_preview_url: str | None) -> str:
    """
    Derive the stable proxy URL.
    Priority:
      1. revision's preview_url (reliable, revision-specific)
      2. app-level url field (if exposed)
      3. fallback: fetch app details and look for any URL field
    Raises RuntimeError if nothing found.
    """
    if revision_preview_url:
        return revision_preview_url

    # Try fetching app details — might have a 'url' or 'urls' field
    try:
        app = deno_request("GET", f"/apps/{app_slug}", token)
        for field in ("url", "default_url", "production_url", "domain"):
            val = app.get(field)
            if val and isinstance(val, str) and val.startswith("https://"):
                return val
        urls = app.get("urls") or app.get("domains") or []
        if isinstance(urls, list) and urls:
            first = urls[0]
            if isinstance(first, str) and first.startswith("https://"):
                return first
            if isinstance(first, dict):
                for k in ("url", "hostname", "domain"):
                    v = first.get(k, "")
                    if v:
                        return f"https://{v}" if not v.startswith("http") else v
    except RuntimeError:
        pass

    raise RuntimeError(
        f"Could not determine the Deno Deploy app URL for slug '{app_slug}'. "
        "Deploy succeeded but the URL was not returned by the API. "
        "Set CLOUDFLARE_PROXY_URL manually in your HF Space secrets."
    )


# ── Env file ─────────────────────────────────────────────────────────────────

def write_env(proxy_url: str, proxy_secret: str) -> None:
    ENV_FILE.write_text(
        "\n".join([
            f'export CLOUDFLARE_PROXY_URL="{proxy_url}"',
            f'export CLOUDFLARE_PROXY_SECRET="{proxy_secret}"',
        ]) + "\n",
        encoding="utf-8",
    )
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    existing_url = os.environ.get("CLOUDFLARE_PROXY_URL", "").strip()
    existing_secret = os.environ.get("CLOUDFLARE_PROXY_SECRET", "").strip()
    api_token = os.environ.get("DENO_DEPLOY_TOKEN", "").strip()

    # Fast path: user already has a deployed worker URL
    if existing_url:
        write_env(existing_url, existing_secret)
        if not existing_secret:
            print(
                "Warning: CLOUDFLARE_PROXY_URL is set but CLOUDFLARE_PROXY_SECRET is empty. "
                "Requests will only work if the deployed worker has no PROXY_SHARED_SECRET.",
                file=sys.stderr,
            )
        return 0

    if not api_token:
        # Nothing to do — proxy is optional
        return 0

    if not api_token.startswith("ddo_"):
        print(
            "Warning: DENO_DEPLOY_TOKEN does not start with 'ddo_'. "
            "This script uses the Deno Deploy v2 API (api.deno.com/v2). "
            "Generate a v2 token in the Deno Deploy console "
            "(console.deno.com → your org → Settings → Access Tokens). "
            "Old dash.deno.com tokens (starting with 'dd') are for the v1 API "
            "which will be shut down July 20, 2026 and will NOT work here.",
            file=sys.stderr,
        )

    try:
        # Build allowed-domains list
        allowed_raw = os.environ.get("CLOUDFLARE_PROXY_DOMAINS", "").strip()
        allow_proxy_all = allowed_raw == "*"
        if allow_proxy_all:
            allowed_targets = list(DEFAULT_ALLOWED)
        else:
            extra = [v.strip() for v in allowed_raw.split(",") if v.strip()]
            seen = set(DEFAULT_ALLOWED)
            allowed_targets = list(DEFAULT_ALLOWED)
            for domain in extra:
                if domain not in seen:
                    allowed_targets.append(domain)
                    seen.add(domain)

        proxy_secret = existing_secret or secrets.token_urlsafe(24)
        worker_source = render_worker(proxy_secret, allowed_targets, allow_proxy_all)

        app_slug = derive_app_slug()
        print(f"Setting up Deno Deploy proxy app '{app_slug}'...", file=sys.stderr)

        actual_slug = get_or_create_app(api_token, app_slug)

        print(f"Deploying worker to '{actual_slug}'...", file=sys.stderr)
        revision_id = deploy_worker(api_token, actual_slug, worker_source)
        print(f"Revision {revision_id} queued — waiting for build...", file=sys.stderr)

        preview_url = wait_for_revision(api_token, revision_id)
        proxy_url = get_app_url(api_token, actual_slug, preview_url)

        write_env(proxy_url, proxy_secret)
        print(f"Deno Deploy proxy ready: {proxy_url}", file=sys.stderr)
        return 0

    except RuntimeError as err:
        print(f"Deno proxy setup failed: {err}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"Deno proxy setup failed (unexpected): {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
