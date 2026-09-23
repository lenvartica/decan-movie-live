/* MovieBox Web — app.js
 * Single-file vanilla JS app. Mirrors the terminal app's model: providers,
 * search, browse shelves, details with seasons/episodes, ranked releases,
 * favorites, watch history / continue watching, TV/M3U mode, addon manager,
 * and the same slash-command vocabulary (docs/controls.md).
 */
(() => {
  "use strict";

  const BOOT = JSON.parse(document.getElementById("boot").textContent);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const el = (tag, attrs = {}, children = []) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children)) if (c !== null && c !== undefined) n.append(c);
    return n;
  };

  // ---------------------------------------------------------------- api
  async function api(path, opts = {}) {
    const resp = await fetch(path, {
      headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
      ...opts,
    });
    let data = null;
    try { data = await resp.json(); } catch { /* no body */ }
    if (!resp.ok) throw new ApiError((data && data.error) || `Request failed (${resp.status})`, resp.status, data);
    return data;
  }
  class ApiError extends Error { constructor(msg, status, data) { super(msg); this.status = status; this.data = data; } }
  const getJSON = (path) => api(path);
  const postJSON = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
  const putJSON = (path, body) => api(path, { method: "PUT", body: JSON.stringify(body || {}) });
  const patchJSON = (path, body) => api(path, { method: "PATCH", body: JSON.stringify(body || {}) });
  const del = (path) => api(path, { method: "DELETE" });

  // ---------------------------------------------------------------- state
  const state = {
    config: BOOT.config,
    providers: BOOT.providers,
    addons: BOOT.addons,
    themes: BOOT.themes,
    view: "home",
    query: "",
    lastFocus: null,
    playing: null,
    tv: { groups: [], selectedGroup: "", query: "", channels: [], offset: 0, total: 0, loading: false },
    details: { provider: null, id: null, data: null, season: null, streams: null, streamsFor: null, streamsLoading: false },
  };
  const currentProvider = () => state.providers.find((p) => p.id === state.config.active_provider) || state.providers[0];

  // ---------------------------------------------------------------- toasts
  const toastStack = $("#toast-stack");
  function toast(message, kind = "info", ms = 4200) {
    const t = el("div", { class: `toast ${kind === "error" ? "err" : kind === "ok" ? "ok" : ""}` }, message);
    toastStack.append(t);
    setTimeout(() => t.remove(), ms);
  }
  const statusMsg = $("#status-msg");
  function flash(message, isError = false) {
    statusMsg.textContent = message;
    statusMsg.classList.toggle("err", isError);
    clearTimeout(flash._t);
    flash._t = setTimeout(() => { statusMsg.textContent = ""; }, 5000);
  }
  function onError(err) {
    if (err instanceof ApiError) toast(err.message, "error");
    else { toast("Something went wrong. Check the console for details.", "error"); console.error(err); }
  }

  // ---------------------------------------------------------------- small formatters
  function timeAgo(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
    return new Date(ts * 1000).toLocaleDateString();
  }
  function fmtDuration(total) {
    total = Math.max(0, Math.floor(total || 0));
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  }
  function fmtSize(bytes) {
    if (!bytes) return "";
    const mb = bytes / 1024 / 1024;
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
  }
  function posterImg(url, alt) {
    if (!url) return el("div", { class: "ph" }, "🎬");
    const img = el("img", { src: url, alt, loading: "lazy" });
    img.addEventListener("error", () => img.replaceWith(el("div", { class: "ph" }, "🎬")));
    return img;
  }
  function qualityBadge(q) { return el("span", { class: `badge ${q ? "q-" + q : ""}` }, q || "SD"); }

  // ---------------------------------------------------------------- views
  const views = { home: $("#view-home"), search: $("#view-search"), details: $("#view-details"), tv: $("#view-tv") };
  function showView(name) {
    state.view = name;
    for (const [k, v] of Object.entries(views)) v.classList.toggle("active", k === name);
    updateStatusBar();
    window.scrollTo({ top: 0 });
  }

  // ---------------------------------------------------------------- top bar: provider + mode
  const providerBadge = $("#provider-badge");
  function renderProviderBadge() {
    const p = currentProvider();
    providerBadge.innerHTML = "";
    providerBadge.append(el("span", { class: "k" }, "provider "), p.label);
  }
  async function setProvider(id) {
    if (id === state.config.active_provider) return;
    state.config = await putJSON("/api/config", { active_provider: id });
    renderProviderBadge();
    closeMenu();
    $("#cmd-input").value = "";
    state.query = "";
    await goHome();
    flash(`Switched to ${currentProvider().label}`);
  }
  function cycleProvider() {
    const ids = state.providers.map((p) => p.id);
    const i = ids.indexOf(state.config.active_provider);
    setProvider(ids[(i + 1) % ids.length]).catch(onError);
  }
  let providerMenuEl = null;
  function closeMenu() { if (providerMenuEl) { providerMenuEl.remove(); providerMenuEl = null; } }
  providerBadge.addEventListener("click", () => {
    if (providerMenuEl) return closeMenu();
    const menu = el("div", { class: "cmd-hint", style: "top:calc(100% + 6px); left:0; min-width:200px;" });
    for (const p of state.providers) {
      const row = el("div", { class: "row" + (p.id === state.config.active_provider ? " sel" : ""), onclick: () => setProvider(p.id).catch(onError) }, [
        el("span", { class: "name" }, p.label),
      ]);
      menu.append(row);
    }
    providerBadge.style.position = "relative";
    providerBadge.append(menu);
    providerMenuEl = menu;
    setTimeout(() => document.addEventListener("click", closeMenu, { once: true }), 0);
  });

  const modeButtons = $$(".pill-toggle button");
  function renderMode() {
    for (const b of modeButtons) b.classList.toggle("active", b.dataset.mode === state.config.active_mode);
  }
  async function setMode(mode) {
    if (mode === state.config.active_mode) return;
    state.config = await putJSON("/api/config", { active_mode: mode });
    renderMode();
    renderModeVisibility();
    if (mode === "tv") { showView("tv"); loadTvPlaylists(); loadTvChannels(true); }
    else { await goHome(); }
  }
  for (const b of modeButtons) b.addEventListener("click", () => setMode(b.dataset.mode).catch(onError));

  $("#brand-home").addEventListener("click", () => {
    cmdInput.value = "";
    state.query = "";
    if (state.config.active_mode === "tv") openAllChannels();
    else goHome();
  });
  $("#btn-addons").addEventListener("click", openAddonsModal);
  $("#btn-tv-playlists").addEventListener("click", openTvPlaylistsModal);
  $("#btn-favorites").addEventListener("click", () => openFavorites().catch(onError));
  $("#btn-history").addEventListener("click", () => openHistory().catch(onError));
  $("#btn-settings").addEventListener("click", openSettingsModal);
  $("#btn-help").addEventListener("click", openHelpModal);
  function renderModeVisibility() {
    $("#btn-addons").classList.toggle("hidden", state.config.active_mode !== "streaming");
    $("#btn-tv-playlists").classList.toggle("hidden", state.config.active_mode !== "tv");
  }

  // ---------------------------------------------------------------- command bar (search + slash commands)
  const COMMANDS = [
    { name: "/browse", desc: "Curated shelves", modes: ["streaming"] },
    { name: "/favorites", desc: "Your starred titles", modes: ["streaming"] },
    { name: "/history", desc: "Watch history", modes: ["streaming"] },
    { name: "/config", desc: "Addon manager", modes: ["streaming"] },
    { name: "/config", desc: "Playlist manager", modes: ["tv"] },
    { name: "/list", desc: "All TV channels", modes: ["tv"] },
    { name: "/settings", desc: "Theme & preferences", modes: ["streaming", "tv"] },
    { name: "/clear", desc: "Clear search", modes: ["streaming", "tv"] },
    { name: "/help", desc: "Keyboard shortcuts", modes: ["streaming", "tv"] },
  ];
  const cmdInput = $("#cmd-input");
  const cmdHint = $("#cmd-hint");
  let searchDebounce = null;
  let hintSel = -1;

  function availableCommands(prefix) {
    const mode = state.config.active_mode;
    const seen = new Set();
    return COMMANDS.filter((c) => c.modes.includes(mode) && c.name.startsWith(prefix) && !seen.has(c.name) && seen.add(c.name));
  }
  function renderHint(list) {
    cmdHint.innerHTML = "";
    hintSel = -1;
    if (!list.length) return cmdHint.classList.add("hidden");
    list.forEach((c) => {
      const row = el("div", { class: "row", onclick: () => runCommand(c.name) }, [el("span", { class: "name" }, c.name), el("span", { class: "desc" }, c.desc)]);
      cmdHint.append(row);
    });
    cmdHint.classList.remove("hidden");
  }
  function runCommand(name) {
    cmdInput.value = "";
    cmdHint.classList.add("hidden");
    cmdInput.blur();
    if (name === "/browse") return goHome();
    if (name === "/favorites") return openFavorites().catch(onError);
    if (name === "/history") return openHistory().catch(onError);
    if (name === "/settings") return openSettingsModal();
    if (name === "/help") return openHelpModal();
    if (name === "/clear") { state.query = ""; return goHome(); }
    if (name === "/config") return state.config.active_mode === "tv" ? openTvPlaylistsModal() : openAddonsModal();
    if (name === "/list") return openAllChannels();
  }
  cmdInput.addEventListener("input", () => {
    const v = cmdInput.value;
    if (v.startsWith("/")) {
      renderHint(availableCommands(v));
      return;
    }
    cmdHint.classList.add("hidden");
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      const q = v.trim();
      state.query = q;
      if (state.config.active_mode === "tv") { state.tv.query = q; loadTvChannels(true); }
      else if (q) runSearch(q).catch(onError);
      else goHome();
    }, 320);
  });
  cmdInput.addEventListener("keydown", (e) => {
    const rows = $$(".row", cmdHint);
    if (!cmdHint.classList.contains("hidden") && rows.length) {
      if (e.key === "ArrowDown") { e.preventDefault(); hintSel = Math.min(rows.length - 1, hintSel + 1); rows.forEach((r, i) => r.classList.toggle("sel", i === hintSel)); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); hintSel = Math.max(0, hintSel - 1); rows.forEach((r, i) => r.classList.toggle("sel", i === hintSel)); return; }
      if (e.key === "Tab" || (e.key === "Enter" && hintSel >= 0)) { e.preventDefault(); runCommand(rows[Math.max(0, hintSel)].querySelector(".name").textContent); return; }
    }
    if (e.key === "Enter" && cmdInput.value.startsWith("/")) { e.preventDefault(); runCommand(cmdInput.value.trim()); return; }
    if (e.key === "Escape") {
      cmdInput.blur();
      cmdHint.classList.add("hidden");
      clearTimeout(searchDebounce);
      if (cmdInput.value || state.query) { cmdInput.value = ""; state.query = ""; goHome(); }
    }
  });
  cmdInput.addEventListener("blur", () => setTimeout(() => cmdHint.classList.add("hidden"), 120));

  // ---------------------------------------------------------------- keyboard grid navigation
  function gridNav(e, container, perRow) {
    const cards = $$(".card", container);
    if (!cards.length) return;
    const cur = document.activeElement;
    const i = cards.indexOf(cur);
    if (i === -1) { cards[0].focus(); return; }
    const cols = perRow || Math.max(1, Math.round(container.clientWidth / (cards[0].offsetWidth + 14)));
    const map = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols };
    if (!(e.key in map)) return;
    e.preventDefault();
    const next = cards[i + map[e.key]];
    if (next) next.focus();
  }

  // =================================================================
  // HOME
  // =================================================================
  const homeSections = $("#home-sections");
  async function goHome() {
    cmdInput.value = "";
    state.query = "";
    showView("home");
    homeSections.innerHTML = "";
    homeSections.append(el("div", { class: "spinner" }));
    try {
      const [hist, favs, browse] = await Promise.all([
        getJSON("/api/history"),
        getJSON("/api/favorites"),
        currentProvider().supports_homepage ? getJSON(`/api/browse?provider=${currentProvider().id}`) : Promise.resolve({ shelves: [], supported: false }),
      ]);
      homeSections.innerHTML = "";
      if (hist.continue.length) homeSections.append(buildShelf("Continue Watching", hist.continue.slice(0, 12), { historyCard: true }));
      if (favs.items.length) homeSections.append(buildShelf("Favorites", favs.items.slice(0, 12), { favoriteCard: true }, favs.items.length > 12 ? "/favorites" : null));
      if (browse.supported) {
        for (const shelf of browse.shelves) {
          if (shelf.error) { homeSections.append(el("div", { class: "shelf" }, [sectionTitle(shelf.label), el("div", { class: "shelf-error" }, shelf.error)])); continue; }
          if (shelf.items.length) homeSections.append(buildShelf(shelf.label, shelf.items));
        }
      } else if (!hist.continue.length && !favs.items.length) {
        homeSections.append(el("div", { class: "empty-state" }, `${currentProvider().label} doesn't offer curated browsing. Type above to search.`));
      }
    } catch (err) { homeSections.innerHTML = ""; onError(err); }
  }
  function sectionTitle(label, count, moreLink) {
    const t = el("div", { class: "section-title" }, [label, count != null ? el("span", { class: "n" }, `${count}`) : null]);
    if (moreLink) t.append(el("span", { class: "n", style: "margin-left:auto;cursor:pointer;", onclick: moreLink }, "see all →"));
    return t;
  }
  function buildShelf(label, items, opts = {}, moreCmd) {
    const grid = el("div", { class: "grid", role: "list" });
    grid.addEventListener("keydown", (e) => gridNav(e, grid));
    for (const item of items) grid.append(opts.historyCard ? historyCard(item) : opts.favoriteCard ? favoriteCard(item) : catalogCard(item));
    return el("div", { class: "shelf" }, [sectionTitle(label, null, moreCmd ? () => runCommand(moreCmd) : null), grid]);
  }
  function catalogCard(item) {
    const card = el("div", { class: "card", tabindex: "0", role: "listitem", onclick: () => openDetails(item.provider, item.id) });
    card.addEventListener("keydown", (e) => { if (e.key === "Enter") card.click(); });
    card.append(
      el("div", { class: "poster" }, posterImg(item.poster_url, item.title)),
      el("div", { class: "title" }, item.title),
      el("div", { class: "sub" }, item.year || "")
    );
    return card;
  }
  function historyCard(item) {
    const card = el("div", { class: "card", tabindex: "0", onclick: () => resumeItem(item) });
    const pct = item.duration_seconds ? Math.min(100, (item.progress_seconds / item.duration_seconds) * 100) : 0;
    card.append(
      el("div", { class: "poster" }, [posterImg(item.cover_url, item.title), el("div", { class: "bar-progress" }, el("i", { style: `width:${pct}%` }))]),
      el("div", { class: "title" }, item.title),
      el("div", { class: "sub" }, item.stype === 2 && item.episode ? `S${item.season} E${item.episode}` : fmtDuration(item.duration_seconds - item.progress_seconds) + " left")
    );
    return card;
  }
  function favoriteCard(item) {
    const card = el("div", { class: "card", tabindex: "0", onclick: () => openDetails(item.provider, item.subject_id) });
    card.append(
      el("div", { class: "poster" }, [posterImg(item.cover_url, item.title), el("div", { class: "fav-dot" }, "♥")]),
      el("div", { class: "title" }, item.title),
      el("div", { class: "sub" }, item.release_year || "")
    );
    return card;
  }
  async function resumeItem(item) {
    try { await openDetails(item.provider, item.subject_id, { autoResume: item }); } catch (err) { onError(err); }
  }

  // =================================================================
  // SEARCH
  // =================================================================
  const searchResults = $("#search-results");
  const searchHeading = $("#search-heading");
  async function runSearch(q) {
    showView("search");
    searchHeading.innerHTML = "";
    searchHeading.append("Results for ", el("span", { class: "q" }, `"${q}"`), el("span", { class: "provider-tag" }, currentProvider().label));
    searchResults.innerHTML = "";
    searchResults.append(el("div", { class: "spinner" }));
    try {
      const data = await getJSON(`/api/search?q=${encodeURIComponent(q)}&provider=${currentProvider().id}`);
      searchResults.innerHTML = "";
      if (!data.results.length) { searchResults.append(el("div", { class: "empty-state" }, "No matches. Try a different title.")); return; }
      const grid = el("div", { class: "grid" });
      grid.addEventListener("keydown", (e) => gridNav(e, grid));
      for (const item of data.results) grid.append(catalogCard(item));
      searchResults.append(grid);
    } catch (err) { searchResults.innerHTML = ""; onError(err); }
  }

  // =================================================================
  // DETAILS
  // =================================================================
  const detailsRoot = $("#view-details");
  async function openDetails(provider, id, opts = {}) {
    showView("details");
    detailsRoot.innerHTML = "";
    detailsRoot.append(backBtn(), el("div", { class: "spinner" }));
    try {
      const data = await getJSON(`/api/details?id=${encodeURIComponent(id)}&provider=${provider}`);
      state.details = { provider, id: data.details.id, data, season: data.details.seasons[0] ? data.details.seasons[0].number : null, streams: null, streamsFor: null };
      renderDetails();
      if (data.details.media_type !== "series") loadStreams(0, 0);
      if (opts.autoResume) {
        const r = data.resume;
        if (r) {
          if (r.stype === 2) selectEpisode(r.season, r.episode, true);
          else playFromReleases(true);
        }
      }
    } catch (err) { detailsRoot.innerHTML = ""; detailsRoot.append(backBtn()); onError(err); }
  }
  function backBtn() {
    return el("button", { class: "details-back", onclick: () => (state.query ? runSearch(state.query) : goHome()) }, "← Back");
  }
  function renderDetails() {
    const { data } = state.details;
    const d = data.details;
    detailsRoot.innerHTML = "";
    const facts = [d.year, d.duration, d.imdb_rating ? `★ ${d.imdb_rating}` : null].filter(Boolean);
    const heroWrap = el("div", { class: "details-hero" }, [
      el("div", { class: "poster" }, posterImg(d.poster_url, d.title)),
      el("div", { class: "details-meta" }, [
        el("h1", {}, d.title),
        el("div", { class: "details-facts" }, facts.map((f, i) => el("span", { class: d.imdb_rating && i === facts.length - 1 ? "rating" : "" }, f))),
        el("div", { class: "details-genres" }, (d.genres || []).map((g) => el("span", { class: "badge" }, g))),
        d.description ? el("p", { class: "details-desc" }, d.description) : null,
        el("div", { class: "details-crew" }, [
          d.director ? el("div", {}, [el("b", {}, "Director/Writer  "), d.director]) : null,
          d.stars ? el("div", {}, [el("b", {}, "Cast  "), d.stars]) : null,
        ]),
        el("div", { class: "details-actions" }, [
          el("button", { class: "btn", id: "fav-btn", onclick: toggleFavorite }, [data.favorite ? "♥ Favorited" : "♡ Favorite", el("span", { class: "k" }, " f")]),
          d.media_type !== "series" ? el("button", { class: "btn primary", onclick: () => playFromReleases(false) }, "▶ Play") : null,
        ]),
      ]),
    ]);
    detailsRoot.append(backBtn(), heroWrap);

    if (d.media_type === "series" && d.seasons.length) {
      const tabs = el("div", { class: "season-tabs" });
      for (const s of d.seasons) tabs.append(el("button", { class: s.number === state.details.season ? "active" : "", onclick: () => { state.details.season = s.number; renderDetails(); } }, s.number === 0 ? "Specials" : `Season ${s.number}`));
      detailsRoot.append(tabs);

      const season = d.seasons.find((s) => s.number === state.details.season) || d.seasons[0];
      const list = el("div", { class: "episode-list" });
      list.addEventListener("keydown", (e) => gridNav(e, list, 1));
      for (const ep of season.episodes) {
        const isSel = state.details.streamsFor && state.details.streamsFor.season === ep.season && state.details.streamsFor.episode === ep.number;
        const watched = data.watched.includes(`${ep.season}:${ep.number}`);
        const row = el("div", { class: "episode-row" + (isSel ? " active" : ""), tabindex: "0", onclick: () => selectEpisode(ep.season, ep.number) }, [
          el("div", { class: "n" }, String(ep.number)),
          ep.thumbnail ? el("div", { class: "thumb" }, posterImg(ep.thumbnail, ep.title || "")) : null,
          el("div", { style: "flex:1;min-width:0" }, [el("div", { class: "et" }, ep.title || `Episode ${ep.number}`), ep.overview ? el("div", { class: "eo" }, ep.overview) : null]),
          watched ? el("span", { class: "watched-mark" }, "✓") : null,
        ]);
        row.addEventListener("keydown", (e) => { if (e.key === "Enter") row.click(); });
        list.append(row);
      }
      detailsRoot.append(list);
    }

    detailsRoot.append(el("div", { id: "streams-slot" }));
    renderStreamsSlot();
  }
  async function toggleFavorite() {
    const { data, provider, id } = state.details;
    const d = data.details;
    try {
      const r = await postJSON("/api/favorites/toggle", { provider, subject_id: id, title: d.title, stype: d.stype, release_year: d.year || "", cover_url: d.poster_url });
      data.favorite = r.favorite;
      const btn = $("#fav-btn");
      if (btn) btn.firstChild.textContent = r.favorite ? "♥ Favorited" : "♡ Favorite";
      flash(r.favorite ? "Added to favorites" : "Removed from favorites");
    } catch (err) { onError(err); }
  }
  async function selectEpisode(season, episode, autoResume) {
    state.details.streamsFor = { season, episode };
    renderDetails();
    await loadStreams(season, episode, autoResume);
  }
  async function loadStreams(season, episode, autoPlay) {
    const { provider, id, data } = state.details;
    state.details.streamsLoading = true;
    renderStreamsSlot();
    try {
      const isSeries = data.details.media_type === "series";
      const r = await getJSON(`/api/streams?id=${encodeURIComponent(id)}&provider=${provider}&season=${season}&episode=${episode}&type=${isSeries ? "series" : "movie"}`);
      state.details.streams = r;
      state.details.streamsLoading = false;
      renderStreamsSlot();
      if (autoPlay) playFromReleases(true);
    } catch (err) {
      state.details.streamsLoading = false;
      state.details.streams = { releases: [], blocked: [], error: err.message };
      renderStreamsSlot();
    }
  }
  function renderStreamsSlot() {
    const slot = $("#streams-slot");
    if (!slot) return;
    slot.innerHTML = "";
    const { streams, streamsLoading, data } = state.details;
    if (data.details.media_type === "series" && !state.details.streamsFor) return;
    slot.append(el("div", { class: "releases-title" }, [el("div", { class: "section-title" }, "Streams"), streams && !streamsLoading ? el("button", { class: "btn ghost", onclick: () => loadStreams((state.details.streamsFor && state.details.streamsFor.season) || 0, (state.details.streamsFor && state.details.streamsFor.episode) || 0) }, ["↻ Refresh", el("span", { class: "k" }, " r")]) : null]));
    if (streamsLoading) { slot.append(el("div", { class: "spinner" })); return; }
    if (!streams) return;
    if (streams.error) { slot.append(el("div", { class: "shelf-error" }, streams.error)); return; }
    if (!streams.releases.length) {
      slot.append(el("div", { class: "empty-state" }, "No playable streams found for this title yet."));
    } else {
      const list = el("div", { class: "release-list" });
      for (const rel of streams.releases) list.append(releaseRow(rel));
      slot.append(list);
    }
    if (streams.blocked.length) slot.append(el("div", { class: "blocked-note" }, `${streams.blocked.join(", ")} answered but had nothing playable in a browser.`));
  }
  function releaseRow(rel) {
    const badges = [qualityBadge(rel.quality), rel.codec ? el("span", { class: "badge" }, rel.codec) : null, rel.language ? el("span", { class: "badge" }, rel.language) : null, rel.size_bytes ? el("span", { class: "badge" }, fmtSize(rel.size_bytes)) : null];
    const mirror = rel.mirrors[0];
    const notWebReady = rel.mirrors.every((m) => !m.web_ready);
    return el("div", { class: "release" }, [
      el("div", { class: "name" }, [mirror ? mirror.label + " — " : "", rel.filename]),
      el("div", { class: "badges" }, badges),
      notWebReady ? el("div", { class: "warn" }, "may not play in-browser") : null,
      el("div", { class: "actions" }, [
        el("button", { class: "btn primary", onclick: () => playRelease(rel) }, "▶ Play"),
        mirror ? el("a", { class: "btn ghost", href: mirror.download_url, download: "" }, "⭳") : null,
      ]),
    ]);
  }
  function playFromReleases(silent) {
    const { streams } = state.details;
    if (!streams || !streams.releases.length) { if (!silent) toast("No streams available yet.", "error"); return; }
    playRelease(streams.releases[0]);
  }
  function playRelease(rel) {
    const { data, streamsFor } = state.details;
    const d = data.details;
    const isSeries = d.media_type === "series";
    const identity = { provider: state.details.provider, subject_id: d.id, title: d.title, stype: d.stype, release_year: d.year || "", cover_url: d.poster_url, season: isSeries ? streamsFor.season : 0, episode: isSeries ? streamsFor.episode : 0 };
    let resumeSeconds = 0;
    if (data.resume && data.resume.subject_id === d.id && data.resume.season === identity.season && data.resume.episode === identity.episode) resumeSeconds = data.resume.progress_seconds;
    openPlayer({ title: d.title + (isSeries ? ` — S${streamsFor.season}E${streamsFor.episode}` : ""), mirrors: rel.mirrors, identity, resumeSeconds, isLive: false });
  }

  // =================================================================
  // FAVORITES / HISTORY full pages (reuse the search view's layout slot)
  // =================================================================
  async function openFavorites() {
    showView("search");
    searchHeading.innerHTML = "";
    searchHeading.append("Favorites");
    searchResults.innerHTML = "";
    const data = await getJSON("/api/favorites");
    if (!data.items.length) { searchResults.append(el("div", { class: "empty-state" }, "No favorites yet. Press f on a title to star it.")); return; }
    const list = el("div", { class: "row-list" });
    for (const item of data.items) {
      const row = el("div", { class: "row-item", tabindex: "0", onclick: () => openDetails(item.provider, item.subject_id) }, [
        el("div", { class: "thumb" }, posterImg(item.cover_url, item.title)),
        el("div", { class: "info" }, [el("div", { class: "t" }, item.title), el("div", { class: "s" }, [item.release_year, timeAgo(item.added_at)].filter(Boolean).join(" · "))]),
        el("button", { class: "rm", title: "Remove", onclick: (e) => { e.stopPropagation(); removeFavorite(item, row); } }, "✕"),
      ]);
      list.append(row);
    }
    searchResults.append(list);
  }
  async function removeFavorite(item, row) {
    try {
      await postJSON("/api/favorites/toggle", { provider: item.provider, subject_id: item.subject_id, title: item.title, stype: item.stype, release_year: item.release_year });
      row.remove();
      flash("Removed from favorites");
    } catch (err) { onError(err); }
  }
  async function openHistory() {
    showView("search");
    searchHeading.innerHTML = "";
    searchHeading.append("Watch History");
    searchResults.innerHTML = "";
    const data = await getJSON("/api/history");
    if (!data.recent.length) { searchResults.append(el("div", { class: "empty-state" }, "Nothing watched yet.")); return; }
    const list = el("div", { class: "row-list" });
    for (const item of data.recent) {
      const pct = item.duration_seconds ? Math.min(100, (item.progress_seconds / item.duration_seconds) * 100) : 0;
      const sub = [item.stype === 2 ? `S${item.season} E${item.episode}` : null, item.completed ? "Watched" : item.duration_seconds ? `${fmtDuration(item.progress_seconds)} / ${fmtDuration(item.duration_seconds)}` : null, timeAgo(item.timestamp)].filter(Boolean).join(" · ");
      const row = el("div", { class: "row-item", tabindex: "0", onclick: () => resumeItem(item) }, [
        el("div", { class: "thumb" }, posterImg(item.cover_url, item.title)),
        el("div", { class: "info" }, [el("div", { class: "t" }, item.title), el("div", { class: "s" }, sub), !item.completed && item.duration_seconds ? el("div", { class: "progress-line" }, el("i", { style: `width:${pct}%` })) : null]),
        el("button", { class: "rm", title: "Remove", onclick: (e) => { e.stopPropagation(); removeHistory(item, row); } }, "✕"),
      ]);
      list.append(row);
    }
    searchResults.append(list);
    searchResults.append(el("div", { style: "margin-top:16px" }, el("button", { class: "btn ghost danger", onclick: clearHistory }, "Clear all history")));
  }
  async function removeHistory(item, row) {
    try { await del(`/api/history?provider=${item.provider}&id=${encodeURIComponent(item.subject_id)}&season=${item.season}&episode=${item.episode}`); row.remove(); flash("Removed"); } catch (err) { onError(err); }
  }
  async function clearHistory() {
    if (!confirm("Clear your entire watch history?")) return;
    try { await del("/api/history/all"); openHistory(); flash("History cleared"); } catch (err) { onError(err); }
  }

  // =================================================================
  // TV MODE
  // =================================================================
  const tvGroupsEl = $("#tv-groups");
  const tvChannelsEl = $("#tv-channels");
  async function loadTvPlaylists() {
    try {
      const data = await getJSON("/api/tv/playlists");
      state.tv.allowLocal = data.allow_local_files;
      state.tv.sources = data.sources;
    } catch (err) { onError(err); }
  }
  async function loadTvChannels(reset) {
    if (reset) { state.tv.offset = 0; state.tv.channels = []; }
    state.tv.loading = true;
    if (reset) { tvChannelsEl.innerHTML = ""; tvChannelsEl.append(el("div", { class: "spinner" })); }
    try {
      const params = new URLSearchParams({ limit: "120", offset: String(state.tv.offset), q: state.tv.query || "", group: state.tv.selectedGroup || "" });
      const data = await getJSON(`/api/tv/channels?${params}`);
      state.tv.groups = data.groups;
      state.tv.total = data.total;
      state.tv.channels = reset ? data.channels : state.tv.channels.concat(data.channels);
      state.tv.offset = state.tv.channels.length;
      renderTvGroups();
      renderTvChannels();
      if (data.failed.length && reset) toast(`${data.failed.length} playlist(s) could not be loaded.`, "error");
      if (data.sources === 0 && reset) tvChannelsEl.replaceChildren(el("div", { class: "empty-state" }, "No playlists yet. Open Playlists to add an M3U URL or file."));
    } catch (err) { tvChannelsEl.innerHTML = ""; onError(err); }
    state.tv.loading = false;
  }
  function renderTvGroups() {
    tvGroupsEl.innerHTML = "";
    tvGroupsEl.append(el("button", { class: !state.tv.selectedGroup ? "active" : "", onclick: () => { state.tv.selectedGroup = ""; loadTvChannels(true); } }, "All"));
    for (const g of state.tv.groups) tvGroupsEl.append(el("button", { class: g.name === state.tv.selectedGroup ? "active" : "", onclick: () => { state.tv.selectedGroup = g.name; loadTvChannels(true); } }, [g.name, el("span", { class: "n" }, ` ${g.count}`)]));
  }
  function renderTvChannels() {
    tvChannelsEl.innerHTML = "";
    if (!state.tv.channels.length && !state.tv.loading) { tvChannelsEl.append(el("div", { class: "empty-state" }, "No channels match.")); return; }
    const grid = el("div", { class: "grid dense" });
    grid.addEventListener("keydown", (e) => gridNav(e, grid));
    for (const ch of state.tv.channels) {
      const card = el("div", { class: "card channel-card", tabindex: "0", onclick: () => playChannel(ch) }, [
        el("div", { class: "poster" }, ch.logo ? posterImg(ch.logo, ch.name) : el("div", { class: "ph" }, "📺")),
        el("div", { class: "title" }, ch.name),
        el("div", { class: "sub" }, ch.group),
      ]);
      grid.append(card);
    }
    tvChannelsEl.append(grid);
    if (state.tv.offset < state.tv.total) tvChannelsEl.append(el("div", { class: "tv-more" }, el("button", { class: "btn", onclick: () => loadTvChannels(false) }, "Load more")));
  }
  function playChannel(ch) {
    openPlayer({ title: ch.name, mirrors: [{ play_url: ch.play_url, direct_url: ch.direct_url, label: ch.name }], identity: null, isLive: true });
  }
  async function openAllChannels() {
    state.tv.query = "";
    state.tv.selectedGroup = "";
    cmdInput.value = "";
    await loadTvChannels(true);
  }

  // =================================================================
  // PLAYER
  // =================================================================
  const overlay = $("#player-overlay");
  const videoEl = $("#player-video");
  const playerTitle = $("#player-title");
  const playerMsg = $("#player-msg");
  const sourceSelect = $("#player-source");
  const liveBadge = $("#player-live");
  let hlsInstance = null;
  let progressTimer = null;
  let lastReportedAt = 0;

  function openPlayer({ title, mirrors, identity, resumeSeconds, isLive }) {
    state.playing = { mirrors, identity, isLive, mirrorIndex: 0, resumeSeconds: resumeSeconds || 0, reportedStart: false };
    playerTitle.textContent = title;
    liveBadge.classList.toggle("hidden", !isLive);
    playerMsg.classList.add("hidden");
    overlay.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    sourceSelect.innerHTML = "";
    mirrors.forEach((m, i) => sourceSelect.append(el("option", { value: i }, m.label || `Source ${i + 1}`)));
    sourceSelect.classList.toggle("hidden", mirrors.length < 2);
    loadMirror(0);
    videoEl.focus();
  }
  function closePlayer(flushFinal) {
    if (state.playing && flushFinal) reportProgress(true);
    clearInterval(progressTimer);
    progressTimer = null;
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
    videoEl.pause();
    videoEl.removeAttribute("src");
    videoEl.load();
    overlay.classList.add("hidden");
    document.body.style.overflow = "";
    state.playing = null;
  }
  function loadMirror(index) {
    const p = state.playing;
    if (!p) return;
    p.mirrorIndex = index;
    sourceSelect.value = String(index);
    const mirror = p.mirrors[index];
    playerMsg.classList.add("hidden");
    if (hlsInstance) { hlsInstance.destroy(); hlsInstance = null; }
    const url = mirror.play_url || mirror.proxy_url;
    const isHls = /\.m3u8($|\?)/i.test(mirror.direct_url || url);
    if (isHls && window.Hls && window.Hls.isSupported()) {
      hlsInstance = new window.Hls({ maxBufferLength: 30 });
      hlsInstance.on(window.Hls.Events.ERROR, (_e, data) => { if (data.fatal) tryNextOrFail(); });
      hlsInstance.loadSource(url);
      hlsInstance.attachMedia(videoEl);
    } else {
      videoEl.src = url;
    }
    videoEl.play().catch(() => {});
  }
  function tryNextOrFail() {
    const p = state.playing;
    if (!p) return;
    if (p.mirrorIndex + 1 < p.mirrors.length) {
      toast("That source failed — trying the next one.", "error");
      loadMirror(p.mirrorIndex + 1);
    } else {
      showPlayerError("None of the available sources would play in this browser. Try downloading instead, or open it in an external player.");
    }
  }
  function showPlayerError(message) {
    playerMsg.innerHTML = "";
    playerMsg.append(message, el("button", { class: "btn", onclick: () => closePlayer(false) }, "Close"));
    playerMsg.classList.remove("hidden");
  }
  videoEl.addEventListener("error", tryNextOrFail);
  videoEl.addEventListener("loadedmetadata", () => {
    const p = state.playing;
    if (!p) return;
    if (p.resumeSeconds && isFinite(videoEl.duration) && p.resumeSeconds < videoEl.duration * 0.95) videoEl.currentTime = p.resumeSeconds;
    if (p.identity && !p.reportedStart) { p.reportedStart = true; postJSON("/api/history/start", { item: p.identity, start: Math.floor(p.resumeSeconds || 0) }).catch(() => {}); }
  });
  videoEl.addEventListener("play", () => { clearInterval(progressTimer); progressTimer = setInterval(() => reportProgress(false), 8000); });
  videoEl.addEventListener("pause", () => reportProgress(false));
  videoEl.addEventListener("ended", () => {
    reportProgress(true);
    playerMsg.innerHTML = "";
    playerMsg.append("Finished.", el("div", { style: "display:flex;gap:10px" }, [
      el("button", { class: "btn", onclick: () => { videoEl.currentTime = 0; videoEl.play(); playerMsg.classList.add("hidden"); } }, "↻ Replay"),
      el("button", { class: "btn", onclick: () => closePlayer(false) }, "Close"),
    ]));
    playerMsg.classList.remove("hidden");
  });
  sourceSelect.addEventListener("change", () => loadMirror(Number(sourceSelect.value)));
  $("#player-close").addEventListener("click", () => closePlayer(true));
  function reportProgress(final) {
    const p = state.playing;
    if (!p || !p.identity || p.isLive) return;
    const progress = Math.floor(videoEl.currentTime || 0);
    const duration = isFinite(videoEl.duration) && videoEl.duration > 0 ? Math.floor(videoEl.duration) : null;
    if (!final && Date.now() - lastReportedAt < 4000) return;
    lastReportedAt = Date.now();
    const body = { item: p.identity, progress, duration };
    if (final) sendBeaconJSON("/api/history/progress", body);
    else postJSON("/api/history/progress", body).catch(() => {});
  }
  function sendBeaconJSON(url, body) {
    try {
      if (!navigator.sendBeacon || !navigator.sendBeacon(url, new Blob([JSON.stringify(body)], { type: "application/json" }))) {
        postJSON(url, body).catch(() => {});
      }
    } catch { postJSON(url, body).catch(() => {}); }
  }
  window.addEventListener("beforeunload", () => { if (state.playing) reportProgress(true); });
  document.addEventListener("visibilitychange", () => { if (document.hidden && state.playing) reportProgress(true); });

  // =================================================================
  // MODALS: generic
  // =================================================================
  const modalBackdrop = $("#modal-backdrop");
  function openModal(contentEl, { wide } = {}) {
    state.lastFocus = document.activeElement;
    modalBackdrop.innerHTML = "";
    const modal = el("div", { class: "modal" + (wide ? " wide" : "") }, contentEl);
    modalBackdrop.append(modal);
    modalBackdrop.classList.remove("hidden");
    const focusable = $("input,button,select", modal);
    if (focusable) focusable.focus();
  }
  function closeModal() {
    modalBackdrop.classList.add("hidden");
    modalBackdrop.innerHTML = "";
    if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
  }
  modalBackdrop.addEventListener("click", (e) => { if (e.target === modalBackdrop) closeModal(); });
  function modalHead(title) { return el("div", { class: "modal-head" }, [el("h2", {}, title), el("button", { onclick: closeModal, "aria-label": "Close" }, "✕")]); }

  // ---------------------------------------------------------------- addons modal
  async function openAddonsModal() {
    const body = el("div", { class: "modal-body" }, el("div", { class: "spinner" }));
    openModal([modalHead("Addon Manager"), body], { wide: true });
    await refreshAddonsModal(body);
  }
  async function refreshAddonsModal(body) {
    try {
      const data = await getJSON("/api/addons");
      state.addons = data.addons;
      body.innerHTML = "";
      for (const a of data.addons) {
        const isCore = a.name.toLowerCase() === "cinemeta";
        const row = el("div", { class: "addon-row" }, [
          el("div", { class: "info" }, [el("div", { class: "name" }, [a.name, a.version ? ` v${a.version}` : "", isCore ? el("span", { class: "core" }, "core · locked") : null]), el("div", { class: "url" }, a.manifest_url)]),
          el("label", { class: "switch" }, [
            el("input", { type: "checkbox", checked: a.enabled, disabled: isCore, onchange: async (e) => { try { await patchJSON("/api/addons", { manifest_url: a.manifest_url, enabled: e.target.checked }); flash(e.target.checked ? "Enabled" : "Disabled"); } catch (err) { e.target.checked = !e.target.checked; onError(err); } } }),
            el("span", { class: "track" }),
          ]),
          !isCore ? el("button", { class: "rm", onclick: async () => { try { await del(`/api/addons?manifest_url=${encodeURIComponent(a.manifest_url)}`); refreshAddonsModal(body); } catch (err) { onError(err); } } }, "✕") : null,
        ]);
        body.append(row);
      }
      const form = el("form", { class: "inline-form", style: "margin-top:12px" });
      const input = el("input", { type: "text", placeholder: "Addon manifest URL (https://.../manifest.json)" });
      const submitBtn = el("button", { class: "btn primary", type: "submit" }, "Install");
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const url = input.value.trim();
        if (!url) return;
        submitBtn.disabled = true;
        try { await postJSON("/api/addons", { manifest_url: url }); input.value = ""; await refreshAddonsModal(body); flash("Addon installed"); }
        catch (err) { onError(err); }
        finally { submitBtn.disabled = false; }
      });
      form.append(input, submitBtn);
      body.append(form);
    } catch (err) { body.innerHTML = ""; onError(err); }
  }

  // ---------------------------------------------------------------- TV playlists modal
  async function openTvPlaylistsModal() {
    const body = el("div", { class: "modal-body" }, el("div", { class: "spinner" }));
    openModal([modalHead("TV Playlists"), body], { wide: true });
    await refreshPlaylistsModal(body);
  }
  async function refreshPlaylistsModal(body) {
    try {
      const data = await getJSON("/api/tv/playlists");
      body.innerHTML = "";
      if (!data.sources.length) body.append(el("div", { class: "empty-state" }, "No playlists yet."));
      for (const s of data.sources) {
        body.append(el("div", { class: "playlist-row" }, [
          el("div", { class: "info" }, [el("div", { class: "name" }, s.label), el("div", { class: "url" }, s.source)]),
          el("button", { class: "rm", onclick: async () => { try { await del(`/api/tv/playlists?source=${encodeURIComponent(s.source)}`); await refreshPlaylistsModal(body); loadTvChannels(true); } catch (err) { onError(err); } } }, "✕"),
        ]));
      }
      const urlForm = el("form", { class: "inline-form" });
      const urlInput = el("input", { type: "text", placeholder: "M3U playlist URL" + (data.allow_local_files ? " or local file path" : "") });
      const urlBtn = el("button", { class: "btn primary", type: "submit" }, "Add");
      urlForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const src = urlInput.value.trim();
        if (!src) return;
        try { await postJSON("/api/tv/playlists", { source: src }); urlInput.value = ""; await refreshPlaylistsModal(body); loadTvChannels(true); flash("Playlist added"); }
        catch (err) { onError(err); }
      });
      urlForm.append(urlInput, urlBtn);
      body.append(el("div", { class: "field", style: "margin-top:12px" }, [el("label", {}, "Add by URL"), urlForm]));

      const fileInput = el("input", { type: "file", accept: ".m3u,.m3u8" });
      fileInput.addEventListener("change", async () => {
        if (!fileInput.files.length) return;
        const fd = new FormData();
        fd.append("file", fileInput.files[0]);
        try {
          const resp = await fetch("/api/tv/playlists", { method: "POST", body: fd });
          const json = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new ApiError(json.error || "Upload failed", resp.status);
          await refreshPlaylistsModal(body);
          loadTvChannels(true);
          flash("Playlist uploaded");
        } catch (err) { onError(err); }
      });
      body.append(el("div", { class: "field" }, [el("label", {}, "Or upload an .m3u file"), fileInput]));
    } catch (err) { body.innerHTML = ""; onError(err); }
  }

  // ---------------------------------------------------------------- settings modal (theme + proxy mode)
  function openSettingsModal() {
    const themeGrid = el("div", { class: "theme-grid" });
    for (const [name, pal] of Object.entries(state.themes)) {
      const sw = el("button", { class: "theme-swatch" + (name === state.config.active_theme ? " active" : "") }, [
        el("div", { class: "dots" }, [pal.base, pal.text, pal.accent, pal.highlight].map((c) => el("span", { style: `background:${c}` }))),
        el("div", { class: "name" }, name),
      ]);
      sw.addEventListener("click", () => selectTheme(name, sw, themeGrid));
      themeGrid.append(sw);
    }
    const proxySelect = el("select", {}, [
      el("option", { value: "auto", selected: state.config.proxy_mode === "auto" }, "Auto (direct when possible)"),
      el("option", { value: "always", selected: state.config.proxy_mode === "always" }, "Always proxy"),
    ]);
    proxySelect.addEventListener("change", async (e) => { try { state.config = await putJSON("/api/config", { proxy_mode: e.target.value }); flash("Saved"); } catch (err) { onError(err); } });
    const body = el("div", { class: "modal-body" }, [
      el("div", { class: "field" }, [el("label", {}, "Theme"), themeGrid]),
      el("div", { class: "field" }, [el("label", {}, "Playback proxy"), proxySelect, el("div", { class: "hint" }, "Proxying rewrites every request through this server, which lets header-gated streams play but uses more of its bandwidth. Auto only proxies streams that need it.")]),
      el("div", { class: "field" }, [el("button", { class: "btn danger", onclick: clearCache }, "Clear cached catalog data")]),
      el("div", { class: "hint" }, `MovieBox Web ${BOOT.version}`),
    ]);
    openModal([modalHead("Settings"), body]);
  }
  async function selectTheme(name, sw, grid) {
    try {
      state.config = await putJSON("/api/config", { active_theme: name });
      applyTheme(name);
      $$(".theme-swatch", grid).forEach((n) => n.classList.remove("active"));
      sw.classList.add("active");
    } catch (err) { onError(err); }
  }
  function applyTheme(name) {
    const styleTag = $("#theme-vars");
    styleTag.textContent = cssForTheme(name);
  }
  function cssForTheme(name) {
    const p = state.themes[name] || state.themes.Mocha;
    const decls = Object.entries(p).filter(([k]) => k !== "is_light").map(([k, v]) => `--c-${k.replace(/_/g, "-")}:${v};`).join("");
    return `:root{${decls}color-scheme:${p.is_light ? "light" : "dark"};}`;
  }
  async function clearCache() {
    try { const r = await postJSON("/api/cache/clear", {}); flash(`Cleared ${r.removed} cached file(s)`); } catch (err) { onError(err); }
  }

  // ---------------------------------------------------------------- help modal
  const SHORTCUTS = [
    ["/", "Focus search / command bar"], ["Enter", "Open selected title"], ["Esc", "Close / clear search"],
    ["Ctrl+P", "Cycle provider"], ["Ctrl+T", "Toggle TV mode"], ["Ctrl+S", "Streaming mode"],
    ["f", "Favorite / unfavorite"], ["r", "Refresh streams / channels"], ["c", "Clear search"], ["?", "This help"],
    ["↑ ↓ ← →", "Move through grid"], ["/browse", "Curated shelves"], ["/favorites", "Your favorites"],
    ["/history", "Watch history"], ["/config", "Addon or playlist manager"], ["/settings", "Theme & preferences"],
  ];
  function openHelpModal() {
    const grid = el("div", { class: "help-grid" }, SHORTCUTS.map(([k, d]) => el("div", { class: "row" }, [el("kbd", {}, k), el("span", {}, d)])));
    openModal([modalHead("Keyboard Shortcuts"), el("div", { class: "modal-body" }, grid)], { wide: true });
  }

  // =================================================================
  // GLOBAL KEYBOARD SHORTCUTS
  // =================================================================
  document.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (!overlay.classList.contains("hidden")) {
      if (e.key === "Escape") closePlayer(true);
      return;
    }
    if (!modalBackdrop.classList.contains("hidden")) {
      if (e.key === "Escape") closeModal();
      return;
    }
    if (e.ctrlKey && e.key.toLowerCase() === "p") { e.preventDefault(); cycleProvider(); return; }
    if (e.ctrlKey && e.key.toLowerCase() === "t") { e.preventDefault(); setMode(state.config.active_mode === "tv" ? "streaming" : "tv").catch(onError); return; }
    if (e.ctrlKey && e.key.toLowerCase() === "s") { e.preventDefault(); setMode("streaming").catch(onError); return; }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); cmdInput.focus(); return; }
    if (e.key === "?") { e.preventDefault(); openHelpModal(); return; }
    if (e.key === "Escape") { if (cmdInput.value || state.query) { state.query = ""; cmdInput.value = ""; goHome(); } return; }
    if (e.key.toLowerCase() === "c" && state.view !== "details") { state.query = ""; cmdInput.value = ""; goHome(); return; }
    if (e.key.toLowerCase() === "f" && state.view === "details" && state.details.data) { toggleFavorite(); return; }
    if (e.key.toLowerCase() === "r") {
      if (state.view === "details" && state.details.streamsFor) loadStreams(state.details.streamsFor.season, state.details.streamsFor.episode);
      else if (state.view === "tv") loadTvChannels(true);
      return;
    }
  });

  // =================================================================
  // INIT
  // =================================================================
  function updateStatusBar() {
    const bar = $("#status-shortcuts");
    const mode = state.config.active_mode;
    const hints = mode === "tv"
      ? [["/", "search"], ["Ctrl+T", "streaming"], ["?", "help"]]
      : state.view === "details"
        ? [["f", "favorite"], ["r", "streams"], ["Esc", "back"], ["?", "help"]]
        : [["/", "search"], ["Ctrl+P", "provider"], ["Ctrl+T", "tv"], ["?", "help"]];
    bar.innerHTML = "";
    hints.forEach(([k, d], i) => { bar.append(el("span", { class: "k" }, k), ` ${d}`); if (i < hints.length - 1) bar.append("   "); });
  }

  (async function init() {
    renderProviderBadge();
    renderMode();
    renderModeVisibility();
    applyTheme(state.config.active_theme);
    if (state.config.active_mode === "tv") { showView("tv"); await loadTvPlaylists(); await loadTvChannels(true); }
    else { await goHome(); }
  })();
})();
