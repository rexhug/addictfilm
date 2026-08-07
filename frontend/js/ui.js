// Small UI policy helpers shared by the classic frontend entry point.
(function attachAddictFilmUi(global) {
  "use strict";

  global.AddictFilmUi = {
    createErrorMapper(translate) {
      const keyByStatus = {
        401: "auth_err_s", 403: "auth_err_s", 404: "load_err",
        409: "load_err", 429: "search_toomany_s", 503: "search_limited_s",
      };
      return function uiError(error, fallbackKey = "load_err") {
        const status = error && error.status;
        if (status) console.warn("api error", status, error.code || "", error.message || "");
        return translate(keyByStatus[status] || fallbackKey);
      };
    },
  };
})(window);
