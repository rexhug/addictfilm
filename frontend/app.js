// Публичный Mini App: мой список → карточка фильма → оценка, поиск, статистика.
// Модель single-user: у каждого свой список; на карточке — моя оценка и
// community-оценка (средняя по всем пользователям). Авторизация — initData.

const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const screen = document.getElementById("screen");
let me = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": tg.initData, // подпись Telegram — проверяется на бэке
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
  return res.json();
}

// ── Экраны ────────────────────────────────────────────────────────────────────
const STATUS_MAP = { want: "want_to_watch", watched: "watched", top: "top" };

async function showList(tab) {
  screen.innerHTML = `<div class="hint">Загружаю…</div>`;
  const { items } = await api(`/api/movies?status=${STATUS_MAP[tab]}&limit=60`);
  if (!items.length) {
    const empty = tab === "want" ? "Пусто. Добавь фильм через 🔍"
      : tab === "watched" ? "Пока ничего не просмотрено."
      : "Оцени просмотренные фильмы — появится твой топ.";
    screen.innerHTML = `<div class="hint">${empty}</div>`;
    return;
  }
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const m of items) {
    const card = document.createElement("div");
    card.className = "poster-card";
    card.innerHTML = `
      ${m.poster_url ? `<img loading="lazy" src="${m.poster_url}">`
                     : `<div class="no-poster">${esc(m.title)}</div>`}
      ${m.my_rating ? `<span class="badge">★ ${m.my_rating}</span>` : ""}
      <div class="title">${esc(m.title)} (${esc(m.year || "")})</div>`;
    card.onclick = () => showDetail(m.id);
    grid.appendChild(card);
  }
  screen.replaceChildren(grid);
}

async function showDetail(id) {
  screen.innerHTML = `<div class="hint">Загружаю…</div>`;
  const m = await api(`/api/movie/${id}`);
  const myRating = m.my_rating;
  const inList = m.status != null;
  const rateBtns = Array.from({ length: 10 }, (_, i) => i + 1)
    .map(n => `<button data-n="${n}" class="${n === myRating ? "mine" : ""}">${n}</button>`)
    .join("");
  screen.innerHTML = `
    <div class="detail">
      ${m.poster_url ? `<img class="hero" src="${m.poster_url}">` : ""}
      <h2>${esc(m.title)}${m.year ? ` · ${esc(m.year)}` : ""}</h2>
      <div class="meta">
        ${esc(m.genres || "")}${m.runtime ? ` · ${esc(m.runtime)}` : ""}
        ${m.directors ? `<br>реж. ${esc(m.directors)}` : ""}
        ${ratingLine(m)}
      </div>
      ${m.plot ? `<p class="plot">${esc(m.plot)}</p>` : ""}
      <div class="rate-label">Моя оценка:</div>
      <div class="rate-row">${rateBtns}</div>
      <div class="actions">
        <button id="toggle-status">${m.status === "watched" ? "↩️ В «Хочу посмотреть»" : "✅ Смотрел(а)"}</button>
        ${inList ? `<button id="del" class="danger">🗑 Убрать из списка</button>` : ""}
      </div>
    </div>`;

  screen.querySelectorAll(".rate-row button").forEach(b => b.onclick = async () => {
    tg.HapticFeedback?.impactOccurred("light");
    await api(`/api/movie/${id}/rate`, { method: "POST", body: JSON.stringify({ rating: +b.dataset.n }) });
    showDetail(id);
  });
  document.getElementById("toggle-status").onclick = async () => {
    const status = m.status === "watched" ? "want_to_watch" : "watched";
    await api(`/api/movie/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
    showDetail(id);
  };
  const del = document.getElementById("del");
  if (del) del.onclick = () => {
    tg.showConfirm(`Убрать «${m.title}» из своего списка?`, async ok => {
      if (!ok) return;
      await api(`/api/movie/${id}`, { method: "DELETE" });
      setActive("want"); showList("want");
    });
  };
}

function showSearch() {
  screen.innerHTML = `
    <input id="search-input" placeholder="Название фильма или сериала…" autofocus>
    <div id="search-results"></div>`;
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  let timer;
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = ""; return; }
      results.innerHTML = `<div class="hint">Ищу…</div>`;
      let data;
      try {
        data = await api(`/api/search?q=${encodeURIComponent(q)}`);
      } catch (e) {
        const msg = String(e.message) === "429"
          ? "Слишком часто. Подожди минуту 🙂"
          : `Ошибка поиска: ${esc(e.message)}`;
        results.innerHTML = `<div class="hint">${msg}</div>`;
        return;
      }
      if (data.limited) {
        results.innerHTML = `<div class="hint">Поиск временно ограничен (дневной лимит источника). Попробуй позже.</div>`;
        return;
      }
      const items = data.items;
      if (!items.length) { results.innerHTML = `<div class="hint">Не найдено. Попробуй год или английское название.</div>`; return; }
      const grid = document.createElement("div");
      grid.className = "grid";
      for (const it of items) {
        const card = document.createElement("div");
        card.className = "poster-card";
        card.innerHTML = `
          ${it.poster || it.poster_url ? `<img src="${it.poster || it.poster_url}">`
                                       : `<div class="no-poster">${esc(it.title)}</div>`}
          ${it.rating ? `<span class="badge">⭐ ${it.rating}</span>` : ""}
          <div class="title">${esc(it.title)} (${esc(it.year || "")})</div>`;
        card.onclick = () => {
          tg.showConfirm(`Добавить «${it.title}» в «Хочу посмотреть»?`, async ok => {
            if (!ok) return;
            const r = await api("/api/add", { method: "POST",
              body: JSON.stringify({ src: it.src, ref: it.ref }) });
            if (r.reason === "exists") tg.showAlert("Уже в твоём списке!");
            else { tg.HapticFeedback?.notificationOccurred("success"); setActive("want"); showList("want"); }
          });
        };
        grid.appendChild(card);
      }
      results.replaceChildren(grid);
    }, 400);
  };
  input.focus();
}

async function showStats() {
  screen.innerHTML = `<div class="hint">Считаю…</div>`;
  const s = await api("/api/stats");
  const y = s.year;
  screen.innerHTML = `
    <div class="stat-block">🎬 Просмотрено: <b>${s.watched}</b> · в списке «Хочу»: <b>${s.want}</b><br>
      ⏱ Экранное время: <b>${Math.floor(s.total_runtime_min / 60)} ч</b>
      ${s.avg_rating != null ? `<br>⭐ Моя средняя оценка: <b>${s.avg_rating}</b> <small>(${s.rating_count})</small>` : ""}</div>
    ${s.top_genres_pct.length ? `<div class="stat-block">🎭 Мои жанры:<br>${
      s.top_genres_pct.map(([g, p]) => `${esc(g)} — ${p}%`).join("<br>")}</div>` : ""}
    ${s.top_actors.length ? `<div class="stat-block">⭐ Актёры:<br>${
      s.top_actors.map(([n, c]) => `${esc(n)} — ${c}`).join("<br>")}</div>` : ""}
    ${s.top_directors.length ? `<div class="stat-block">🎬 Режиссёры:<br>${
      s.top_directors.map(([n, c]) => `${esc(n)} — ${c}`).join("<br>")}</div>` : ""}
    ${y.count ? `<div class="stat-block">📅 Итоги ${y.year}: <b>${y.count}</b> фильмов
      ${y.avg_rating ? ` · средняя <b>${y.avg_rating}</b>` : ""}
      ${y.top_genre ? `<br>любимый жанр — ${esc(y.top_genre)}` : ""}
      ${y.top_actor ? `<br>актёр года — ${esc(y.top_actor[0])}` : ""}</div>` : ""}`;
}

// ── Утилиты ───────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function ratingLine(m) {
  const parts = [];
  if (m.kp_rating) parts.push(`КП ${m.kp_rating}`);
  if (m.imdb_rating) parts.push(`IMDb ${m.imdb_rating}`);
  if (m.community && m.community.count) parts.push(`👥 ${m.community.avg} (${m.community.count})`);
  return parts.length ? `<br>⭐ ${parts.join(" · ")}` : "";
}
function setActive(tab) {
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
}

// ── Навигация ─────────────────────────────────────────────────────────────────
document.querySelectorAll("#tabs button").forEach(btn => {
  btn.onclick = () => {
    setActive(btn.dataset.tab);
    const t = btn.dataset.tab;
    if (t === "search") showSearch();
    else if (t === "stats") showStats();
    else showList(t);
  };
});

// Старт: проверяем авторизацию и открываем «Хочу посмотреть».
(async () => {
  try {
    me = await api("/api/me");
    showList("want");
  } catch (e) {
    screen.innerHTML = `<div class="hint">⛔ ${esc(e.message)}<br>Открой через кнопку меню бота в Telegram.</div>`;
  }
})();
