/**
 * neuralosd-client — thin, zero-dependency browser/Node client for a
 * neuralOS instance server (neuralosd serve).
 *
 * ESM:   import { NeuralOSDClient } from "./neuralosd-client.mjs";
 * Node:  global fetch (Node 18+). Browser: native fetch.
 *
 *   const c = new NeuralOSDClient("http://127.0.0.1:8877", { token: "secret" });
 *   const r = await c.ask("total revenue by region");
 *   console.log(r.probe, r.results);
 *
 * Error model mirrors the server's honesty contract:
 *   - AuthError            → 401 (token missing/wrong)
 *   - RefusalError         → 422 (instance honestly declined; .envelope has the reason)
 *   - TransportError       → unreachable / kept 5xx-ing after retries
 * 4xx is never retried; connection errors and 5xx retry with backoff.
 */

export class AuthError extends Error {
  constructor(msg) { super(msg); this.name = "AuthError"; }
}
export class RefusalError extends Error {
  constructor(msg, envelope) {
    super(msg); this.name = "RefusalError"; this.envelope = envelope || {};
  }
}
export class TransportError extends Error {
  constructor(msg) { super(msg); this.name = "TransportError"; }
}

export class NeuralOSDClient {
  /**
   * @param {string} base_url   e.g. "http://127.0.0.1:8877"
   * @param {object} [opts]     { token, timeoutMs=30000, maxRetries=2, backoffMs=500 }
   */
  constructor(base_url, opts = {}) {
    this.base = String(base_url).replace(/\/+$/, "");
    this.token = opts.token || null;
    this.timeoutMs = opts.timeoutMs ?? 30000;
    this.maxRetries = opts.maxRetries ?? 2;
    this.backoffMs = opts.backoffMs ?? 500;
  }

  /** Ask one question → { question, probe, results, mode, cached, latencyMs, raw } */
  async ask(question) {
    const env = await this._request("/ask", {
      method: "POST",
      body: JSON.stringify({ question }),
      retryable: true,
    });
    return {
      question,
      probe: env.probe ?? null,
      results: env.results ?? null,
      mode: env.mode ?? null,
      cached: !!env.cached,
      latencyMs: env.latency_ms ?? null,
      raw: env,
    };
  }

  health() { return this._request("/healthz", {}); }
  ready() { return this._request("/ready", {}); }
  openapi() { return this._request("/openapi.json", {}); }

  // ── transport ────────────────────────────────────────────────────────
  async #fetchWithTimeout(url, init) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      return await fetch(url, { ...init, signal: ctrl.signal });
    } finally {
      clearTimeout(t);
    }
  }

  async _request(path, { method = "GET", body, retryable = false } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    let attempt = 0;
    let lastErr = null;
    const maxAttempts = retryable ? this.maxRetries + 1 : 1;
    while (attempt < maxAttempts) {
      let resp;
      try {
        resp = await this.#fetchWithTimeout(`${this.base}${path}`,
          { method, headers, body });
      } catch (e) {
        if (!retryable) throw new TransportError(`connection failed: ${e}`);
        lastErr = new TransportError(`connection failed: ${e}`);
      }
      if (resp) {
        const text = await resp.text();
        let parsed = {};
        try { parsed = JSON.parse(text || "{}"); } catch { parsed = { error: text }; }
        if (resp.ok) return parsed;
        if (resp.status === 401)
          throw new AuthError(parsed.error || "unauthorized: token missing or wrong");
        if (resp.status === 422)
          throw new RefusalError(parsed.error || "instance refused", parsed);
        if (resp.status >= 500 && retryable)
          lastErr = new TransportError(`HTTP ${resp.status}: ${parsed.error || text}`);
        else
          throw new TransportError(`HTTP ${resp.status}: ${parsed.error || text}`);
      }
      attempt += 1;
      if (attempt < maxAttempts)
        await new Promise(r => setTimeout(r, this.backoffMs * 2 ** (attempt - 1)));
    }
    throw lastErr || new TransportError("request failed");
  }
}
