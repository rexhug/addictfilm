// Public-review pagination policy. Network calls and DOM wiring remain in
// app.js; this module keeps cursor ordering consistent across detail screens.
(function attachAddictFilmReviews(global) {
  "use strict";

  global.AddictFilmReviews = {
    create({ pageSize = 10 } = {}) {
      return {
        query(beforeId = null) {
          const query = new URLSearchParams({ limit: String(pageSize), sort: "newest" });
          if (beforeId) query.set("before_id", String(beforeId));
          return query;
        },
        nextCursor(data) {
          return data && data.next_before_id ? String(data.next_before_id) : null;
        },
      };
    },
  };
})(window);
