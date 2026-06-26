/**
 * GOST Proxy: Transparent outbound proxy via local GOST HTTP proxy.
 *
 * Patches https.request / http.request / fetch / undici so that traffic
 * for selected hosts is tunnelled through a local GOST HTTP proxy.
 * GOST handles upstream chaining (SOCKS5, HTTP, direct) transparently.
 *
 * Loaded via NODE_OPTIONS=--require so every Node.js process inherits it.
 *
 * Env vars (read at startup, written by gost-proxy-setup.py):
 *   GOST_PROXY_URL     — local GOST HTTP proxy, e.g. http://127.0.0.1:8118
 *   GOST_PROXY_DEBUG   — "true" for per-request debug logs on stderr
 *   GOST_PROXY_DOMAINS — extra comma-separated domains to proxy, or "*" for all
 */
"use strict";

const https = require("https");
const http  = require("http");
const tls   = require("tls");

const DEBUG  = process.env.GOST_PROXY_DEBUG === "true";
const debug  = (...a) => { if (DEBUG) console.error("[gost-proxy]", ...a); };
const warn   = (...a) => console.warn("[gost-proxy]", ...a);

let PROXY_URL = (process.env.GOST_PROXY_URL || "").trim();
if (PROXY_URL && !/^https?:\/\//.test(PROXY_URL)) PROXY_URL = `http://${PROXY_URL}`;

// ── Domain list ────────────────────────────────────────────────────────────
const DEFAULT_PROXY_DOMAINS = [
  // Messaging — geo-blocked from HF Spaces egress IPs
  "api.telegram.org",
  "discord.com", "discordapp.com", "gateway.discord.gg", "status.discord.com",
  "web.whatsapp.com", "whatsapp.com", "whatsapp.net",
  "graph.facebook.com", "graph.instagram.com",
  "api.twitter.com", "api.x.com", "upload.twitter.com",
  "api.linkedin.com", "www.linkedin.com",
  "open.tiktokapis.com", "oauth.reddit.com",
  "youtube.com", "www.youtube.com",
  // Email HTTP APIs
  "api.resend.com", "api.sendgrid.com", "api.mailgun.net",
  // AI providers with per-IP rate limits
  "generativelanguage.googleapis.com", "aiplatform.googleapis.com",
  "googleapis.com", "google.com", "googleusercontent.com", "gstatic.com",
  "openrouter.ai",
  "integrate.api.nvidia.com", "api.nvidia.com",
];

// Never proxy HF infra even in wildcard mode
const NEVER_PROXY_DOMAINS = [
  "huggingface.co",
  "router.huggingface.co",
  "api-inference.huggingface.co",
];

const _rawExtra   = (process.env.GOST_PROXY_DOMAINS || "").trim();
const PROXY_ALL   = _rawExtra === "*";
const EXTRA_HOSTS = _rawExtra.split(",").map(d => d.trim()).filter(Boolean);

const _set = new Set(DEFAULT_PROXY_DOMAINS);
for (const d of EXTRA_HOSTS) if (!_set.has(d)) { DEFAULT_PROXY_DOMAINS.push(d); _set.add(d); }

// ── Bail early if no proxy URL ─────────────────────────────────────────────
if (!PROXY_URL) return; // CommonJS module-level return — no-op, process continues

try {
  const _purl      = new URL(PROXY_URL);
  const PROXY_HOST = _purl.hostname;
  const PROXY_PORT = parseInt(_purl.port || "8118", 10);

  const matchesList = (h, list) => list.some(d => h === d || h.endsWith(`.${d}`));

  const shouldProxy = (hostname) => {
    const h = String(hostname || "").toLowerCase().trim();
    if (!h) return false;
    if (h === "localhost" || h === "127.0.0.1" || h === "::1" ||
        h === "0.0.0.0"  || h === PROXY_HOST   || h.endsWith(".hf.space")) return false;
    if (matchesList(h, NEVER_PROXY_DOMAINS)) return false;
    return PROXY_ALL || matchesList(h, DEFAULT_PROXY_DOMAINS);
  };

  // Capture originals before patching to avoid infinite recursion
  const _origHttpsReq = https.request.bind(https);
  const _origHttpReq  = http.request.bind(http);
  const _origFetch    = typeof globalThis.fetch === "function"
    ? globalThis.fetch.bind(globalThis) : null;

  // ── HTTP CONNECT tunnel agent for HTTPS ──────────────────────────────────
  class GostTunnelAgent extends https.Agent {
    createConnection(opts, cb) {
      const hostname = opts.host || opts.hostname || "";
      const port     = opts.port || 443;
      const target   = `${hostname}:${port}`;
      debug(`CONNECT ${target} via ${PROXY_HOST}:${PROXY_PORT}`);

      const req = _origHttpReq({
        method:   "CONNECT",
        hostname: PROXY_HOST,
        port:     PROXY_PORT,
        path:     target,
        headers:  { Host: target },
      });

      req.once("connect", (res, socket) => {
        if (res.statusCode !== 200) {
          socket.destroy();
          return cb(new Error(`GOST CONNECT ${target} → ${res.statusCode} ${res.statusMessage}`));
        }
        const tlsSocket = tls.connect({
          socket,
          servername:         opts.servername || hostname,
          rejectUnauthorized: opts.rejectUnauthorized !== false,
          session:            opts.session,
          minVersion:         opts.minVersion,
          maxVersion:         opts.maxVersion,
          ALPNProtocols:      opts.ALPNProtocols,
          secureContext:      opts.secureContext,
        });
        tlsSocket.once("secureConnect", () => cb(null, tlsSocket));
        tlsSocket.once("error", cb);
      });
      req.once("error", (err) => { warn(`CONNECT ${target} error: ${err.message}`); cb(err); });
      req.end();
    }
  }

  const _tunnelAgent = new GostTunnelAgent({ keepAlive: false, maxSockets: Infinity });

  // ── Parse Node.js http/https request args ────────────────────────────────
  const parseArgs = (a1, a2, a3) => {
    let opts = {}, cb;
    if (typeof a1 === "string" || a1 instanceof URL) {
      const u = typeof a1 === "string" ? new URL(a1) : a1;
      opts = { protocol: u.protocol, hostname: u.hostname,
               port: u.port || (u.protocol === "https:" ? 443 : 80),
               path: u.pathname + u.search };
      if (a2 && typeof a2 === "object") { opts = { ...opts, ...a2 }; cb = a3; }
      else cb = a2;
    } else { opts = { ...a1 }; cb = a2; }
    return { opts, cb };
  };

  const extractHost = (o) =>
    o.hostname || (o.host ? String(o.host).split(":")[0] : "") || "";

  // ── Patch https.request ──────────────────────────────────────────────────
  https.request = function patchedHttpsRequest(a1, a2, a3) {
    const { opts, cb } = parseArgs(a1, a2, a3);
    const h = extractHost(opts);
    if (shouldProxy(h) && !opts._gostProxied && !opts.agent) {
      debug(`https.request → ${h}`);
      opts._gostProxied = true;
      opts.agent = _tunnelAgent;
    }
    return _origHttpsReq(opts, cb);
  };
  https.get = (u, o, c) => { const r = https.request(u, o, c); r.end(); return r; };

  // ── Patch http.request (HTTP forwarding proxy) ───────────────────────────
  const _origHttpReqOrig = http.request;
  http.request = function patchedHttpRequest(a1, a2, a3) {
    const { opts, cb } = parseArgs(a1, a2, a3);
    const h = extractHost(opts);
    if (shouldProxy(h) && !opts._gostProxied) {
      debug(`http.request → ${h}`);
      const proto = opts.protocol || "http:";
      const port  = opts.port || 80;
      const path  = opts.path  || "/";
      opts._gostProxied = true;
      opts.hostname = PROXY_HOST;
      opts.host     = undefined;
      opts.port     = PROXY_PORT;
      opts.path     = `${proto}//${h}:${port}${path}`;
    }
    return _origHttpReqOrig.call(http, opts, cb);
  };
  http.get = (u, o, c) => { const r = http.request(u, o, c); r.end(); return r; };

  // ── Patch globalThis.fetch ───────────────────────────────────────────────
  if (_origFetch) {
    globalThis.fetch = async function patchedFetch(input, init) {
      let urlStr;
      try {
        urlStr = input instanceof Request ? input.url : String(input);
        const u = new URL(urlStr);
        if (!shouldProxy(u.hostname)) return _origFetch(input, init);
        debug(`fetch → ${u.hostname}`);

        let undici;
        try { undici = require("undici"); } catch (_) {}
        if (undici?.ProxyAgent && undici?.fetch) {
          const pa = new undici.ProxyAgent(`http://${PROXY_HOST}:${PROXY_PORT}`);
          const rl  = input instanceof Request ? input : null;
          const headers = new Headers(init?.headers || rl?.headers || undefined);
          const body    = init?.body ?? (!rl?.bodyUsed ? rl?.body : undefined);
          const ni = { method: init?.method || rl?.method || "GET", headers, dispatcher: pa };
          if (body != null) { ni.body = body; if (body instanceof ReadableStream) ni.duplex = init?.duplex || rl?.duplex || "half"; }
          for (const k of ["signal", "redirect", "integrity"]) {
            const v = init?.[k] || rl?.[k]; if (v) ni[k] = v;
          }
          return await undici.fetch(String(u), ni);
        }
      } catch (_) {}
      return _origFetch(input, init);
    };
  }

  // ── Patch undici ─────────────────────────────────────────────────────────
  const _seen = new WeakSet();

  const patchUndici = (mod) => {
    if (!mod || typeof mod !== "object" || _seen.has(mod)) return;
    _seen.add(mod);
    const PA = mod.ProxyAgent;
    if (!PA) { debug("undici: no ProxyAgent, skipping"); return; }

    let pa;
    try { pa = new PA(`http://${PROXY_HOST}:${PROXY_PORT}`); }
    catch (e) { warn(`undici ProxyAgent failed: ${e.message}`); return; }

    const patchDispatch = (proto, label) => {
      if (!proto?.dispatch || proto.dispatch._gostProxied) return;
      const orig = proto.dispatch;
      proto.dispatch = function gostDispatch(options, handler) {
        let origin = options.origin || this.origin;
        if (origin && typeof origin !== "string") {
          try { origin = origin.origin || origin.href || String(origin); } catch (_) { origin = ""; }
        }
        let h = "";
        try { h = new URL(String(origin || "")).hostname; } catch (_) {}
        if (h && shouldProxy(h)) { debug(`undici ${label}.dispatch → ${h}`); return pa.dispatch(options, handler); }
        return orig.call(this, options, handler);
      };
      proto.dispatch._gostProxied = true;
    };

    for (const key of Object.keys(mod)) {
      try { if (mod[key]?.prototype?.dispatch) patchDispatch(mod[key].prototype, key); } catch (_) {}
    }
    for (const cls of ["Agent", "Pool", "Client"]) {
      if (mod[cls]?.prototype) patchDispatch(mod[cls].prototype, cls);
    }
    try {
      if (typeof mod.getGlobalDispatcher === "function") {
        const gd = mod.getGlobalDispatcher();
        if (gd?.dispatch && !gd.dispatch._gostProxied) patchDispatch(gd, "GlobalDispatcher");
      }
    } catch (_) {}

    if (mod.fetch && !mod.fetch._gostProxied) {
      const origFetch = mod.fetch;
      mod.fetch = async function gostUndiciFetch(input, init) {
        let url;
        try {
          const s = input && typeof input === "object" && "url" in input ? input.url : String(input);
          url = new URL(s);
        } catch (_) { return origFetch(input, init); }
        if (!shouldProxy(url.hostname)) return origFetch(input, init);
        debug(`undici.fetch → ${url.hostname}`);
        const rl  = input && typeof input === "object" ? input : null;
        const headers = new Headers(init?.headers || rl?.headers || undefined);
        const body    = init?.body ?? (!rl?.bodyUsed ? rl?.body : undefined);
        const ni = { method: init?.method || rl?.method || "GET", headers, dispatcher: pa };
        if (body != null) { ni.body = body; if (body instanceof ReadableStream) ni.duplex = init?.duplex || rl?.duplex || "half"; }
        for (const k of ["signal", "redirect", "integrity"]) { const v = init?.[k] || rl?.[k]; if (v) ni[k] = v; }
        return origFetch(String(url), ni);
      };
      mod.fetch._gostProxied = true;
    }
    debug("undici patched");
  };

  try { patchUndici(require("undici")); } catch (_) {}

  // Hook future require() calls so bundled undici copies get patched too
  const Module  = require("module");
  const _origRq = Module.prototype.require;
  const UNDICI_RE = /(?:^|\/)node_modules\/undici(?:\/|$)/;
  Module.prototype.require = function (id) {
    const mod = _origRq.apply(this, arguments);
    if (id === "undici" || UNDICI_RE.test(id)) { try { patchUndici(mod); } catch (_) {} }
    return mod;
  };

  if (DEBUG) {
    try {
      require("fs").writeFileSync("/tmp/.gost-proxy-banner", "1", { flag: "wx" });
      debug(`active (${PROXY_ALL ? "wildcard" : "domain-list"}) → ${PROXY_HOST}:${PROXY_PORT}`);
    } catch (_) {}
  }
} catch (err) {
  warn(`init failed: ${err.message}`);
}
