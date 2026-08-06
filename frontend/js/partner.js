// Pair presentation normalization. Pairing requests and navigation remain in
// app.js; this keeps optional server fields safe for every profile state.
(function attachAddictFilmPartner(global) {
  "use strict";

  global.AddictFilmPartner = {
    create() {
      return {
        normalize(partner) {
          const value = partner && typeof partner === "object" ? partner : {};
          return {
            status: typeof value.status === "string" ? value.status : "none",
            code: typeof value.code === "string" ? value.code : "",
            link: typeof value.link === "string" ? value.link : "",
          };
        },
      };
    },
  };
})(window);
