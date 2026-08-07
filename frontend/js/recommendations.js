// Recommendation presentation helpers shared by picker screens.
(function attachAddictFilmRecommendations(global) {
  "use strict";

  global.AddictFilmRecommendations = {
    create({ translate }) {
      return {
        reasons(item) {
          const codes = Array.isArray(item?.reasons) ? item.reasons : [];
          const phrases = codes.map(code => translate(`reason_${code}`))
            .filter(text => text && !text.startsWith("reason_"));
          return phrases.length ? phrases.join(" · ") : "";
        },
      };
    },
  };
})(window);
