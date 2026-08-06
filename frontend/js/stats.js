// Pure statistics helpers. Rendering and translations stay in app.js for
// compatibility with the existing screen wiring.
(function attachAddictFilmStats(global) {
  "use strict";

  global.AddictFilmStats = {
    rankedRatings(distribution, limit = 2) {
      return (Array.isArray(distribution) ? distribution : [])
        .map((count, index) => ({ rating: index + 1, count: Number(count) || 0 }))
        .filter(item => item.count > 0)
        .sort((a, b) => b.count - a.count || a.rating - b.rating)
        .slice(0, limit)
        .map(item => item.rating);
    },
  };
})(window);
