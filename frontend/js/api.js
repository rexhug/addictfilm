// Shared network client for the classic Addict Film frontend.
// The app remains a single script for compatibility, while request policy is
// isolated here so future screens do not duplicate auth/error/cache handling.
(function attachAddictFilmApi(global) {
  "use strict";

  global.AddictFilmApi = {
    create({ getInitData, cacheableRead, cache, cacheTtl }) {
      return async function api(path, opts = {}) {
        const method = (opts.method || "GET").toUpperCase();
        const canCache = cacheableRead(path, opts);
        const cached = canCache && cache.get(path);
        if (cached && cached.expiresAt > Date.now()) return cached.value;
        const res = await fetch(path, {
          ...opts,
          headers: {
            "Content-Type": "application/json",
            "X-Init-Data": getInitData() || "",
            ...(opts.headers || {}),
          },
        });
        if (!res.ok) {
          const detail = (await res.json().catch(() => ({}))).detail;
          const error = new Error(typeof detail === "string" ? detail : (detail?.message || String(res.status)));
          error.status = res.status;
          error.code = detail && typeof detail === "object" ? detail.code : null;
          throw error;
        }
        const value = await res.json();
        if (canCache) cache.set(path, { value, expiresAt: Date.now() + cacheTtl });
        if (method !== "GET") cache.clear();
        return value;
      };
    },
  };
})(window);
