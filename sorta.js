(() => {
  "use strict";

  const el = id => document.getElementById(id);
  const number = new Intl.NumberFormat("pt-BR");
  const state = { roots: [], disks: [], rootId: "", scan: null, selected: [], chosen: null,
    plan: null, planArgs: null, filter: "", page: 0, busy: false, library: null,
    catalogSettings: null, catalogMediaId: null, genreDraft: [], availableGenres: null,
    catalogFilter: "", catalogPage: 0, posterCache: new Map() };
  const pageSize = 80;
  const catalogPageSize = 24;

  function node(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined && value !== null) item.textContent = String(value);
    return item;
  }

  function message(text, isError = false) {
    el("notice").hidden = true;
    el("error").hidden = true;
    if (!text) return;
    const target = isError ? el("error") : el("notice");
    target.textContent = text;
    target.hidden = false;
  }

  function setBusy(busy) {
    state.busy = busy;
    document.body.classList.toggle("busy", busy);
    el("scan-button").disabled = busy || !chosenRoot()?.available;
    el("preview-button").disabled = busy || !state.chosen || !state.selected.length;
    el("organize-button").disabled = busy;
  }

  async function api(command, payload = {}) {
    const process = cockpit.spawn(["/usr/bin/python3", "/usr/local/libexec/cockpit-sorta/helper.py", command], { err: "message" });
    process.input(JSON.stringify(payload));
    const output = await process;
    let response;
    try { response = JSON.parse(output); }
    catch (_) { throw new Error("Resposta inválida do helper Sorta."); }
    if (!response.ok) throw new Error(response.error || "Falha na operação.");
    return response.result;
  }

  async function action(fn) {
    if (state.busy) return;
    setBusy(true);
    message("");
    try { await fn(); }
    catch (error) { message(error.message || String(error), true); }
    finally { setBusy(false); }
  }

  function chosenRoot() { return state.roots.find(root => root.id === state.rootId); }

  function renderRoots() {
    const selector = el("root-select");
    selector.replaceChildren();
    if (!state.roots.length) selector.append(node("option", "", "Nenhuma pasta cadastrada"));
    for (const root of state.roots) {
      const option = node("option", "", root.label + (root.available ? "" : " · indisponível"));
      option.value = root.id;
      selector.append(option);
    }
    selector.value = state.rootId;
    const active = chosenRoot();
    el("mount-status").textContent = active ? (active.available ? active.path : active.error) : "Adicione uma pasta para começar";
    el("mount-status").classList.toggle("bad", !!active && !active.available);
    el("scan-button").disabled = state.busy || !active || !active.available;
    el("recovery").hidden = !active?.pending_operation;

    const registered = el("registered-roots");
    registered.replaceChildren();
    for (const root of state.roots) {
      const row = node("div", "registered-row");
      const text = node("div");
      text.append(node("strong", "", root.label), node("small", "", root.path));
      const remove = node("button", "secondary", "Remover");
      remove.type = "button";
      remove.addEventListener("click", () => action(async () => {
        if (!window.confirm(`Remover “${root.label}” da lista? Os arquivos e o banco permanecerão no disco.`)) return;
        await api("remove-root", { root_id: root.id });
        await refreshStatus();
        clearWorkspace(); clearCatalog();
        if (chosenRoot()?.available) await scanSelected();
        await refreshCatalog();
        message("Pasta removida da lista.");
      }));
      row.append(text, remove);
      registered.append(row);
    }
    el("key-status").textContent = state.tmdbConfigured ? "Chave configurada" : "Chave ainda não configurada";
  }

  function renderDisks() {
    const select = el("disk-select");
    select.replaceChildren();
    for (const disk of state.disks) {
      const size = disk.size ? ` · ${Math.round(disk.size / 1e9)} GB` : "";
      const option = node("option", "", `${disk.label || disk.path} · ${disk.mountpoint}${size}`);
      option.value = disk.mountpoint;
      select.append(option);
    }
    if (!state.disks.length) select.append(node("option", "", "Nenhum disco de dados montado"));
    if (state.disks.some(d => d.mountpoint === "/mnt/hdd500")) select.value = "/mnt/hdd500";
  }

  async function refreshStatus() {
    const result = await api("status");
    state.roots = result.roots;
    state.tmdbConfigured = result.tmdb_configured;
    const saved = window.localStorage.getItem("cockpit-sorta-root");
    if (!state.roots.some(root => root.id === state.rootId)) {
      state.rootId = state.roots.find(root => root.id === saved)?.id || state.roots[0]?.id || "";
    }
    renderRoots();
    if (!state.roots.length) {
      el("settings").hidden = false;
      el("settings-toggle").setAttribute("aria-expanded", "true");
    }
  }

  function clearWorkspace() {
    state.scan = null;
    state.selected = [];
    state.chosen = null;
    state.plan = null;
    state.planArgs = null;
    state.page = 0;
    el("tmdb-results").replaceChildren();
    el("chosen-media").hidden = true;
    el("preview").hidden = true;
    el("organize-button").hidden = true;
    renderFiles();
    renderSelection();
  }

  function clearCatalog() {
    state.library = null;
    state.catalogSettings = null;
    state.catalogMediaId = null;
    state.genreDraft = [];
    state.availableGenres = null;
    state.catalogPage = 0;
    renderCatalog(); renderCatalogDetail(); renderTranslations();
  }

  function bytes(value) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, size = value;
    while (size >= 1000 && i < units.length - 1) { size /= 1000; i++; }
    return `${number.format(Math.round(size * 10) / 10)} ${units[i]}`;
  }

  function filteredFiles() {
    const term = state.filter.toLocaleLowerCase("pt-BR");
    return (state.scan?.files || []).filter(file => file.path.toLocaleLowerCase("pt-BR").includes(term));
  }

  function renderFiles() {
    const report = state.scan;
    el("total-count").textContent = report ? number.format(report.total) : "—";
    el("linked-count").textContent = report ? number.format(report.linked) : "—";
    el("pending-count").textContent = report ? number.format(report.pending) : "—";
    el("scan-summary").textContent = report
      ? `${report.truncated ? "A lista mostra os primeiros 5.000 pendentes. " : ""}${report.errors ? `${report.errors} pastas ou arquivos inacessíveis. ` : ""}${report.schema_version ? `Banco Sorta v${report.schema_version}.` : "O banco será criado ao organizar o primeiro item."}`
      : "Selecione um catálogo e clique em Varrer pasta.";
    const files = filteredFiles();
    const pages = Math.max(1, Math.ceil(files.length / pageSize));
    state.page = Math.min(state.page, pages - 1);
    const visible = files.slice(state.page * pageSize, (state.page + 1) * pageSize);
    const list = el("file-list");
    list.replaceChildren();
    if (!visible.length) list.append(node("p", "empty", report ? "Nenhum arquivo pendente neste filtro." : "Aguardando varredura."));
    for (const file of visible) {
      const row = node("label", "file-row");
      const check = node("input");
      check.type = "checkbox";
      check.checked = state.selected.includes(file.path);
      check.addEventListener("change", () => {
        if (check.checked && !state.selected.includes(file.path)) state.selected.push(file.path);
        if (!check.checked) state.selected = state.selected.filter(path => path !== file.path);
        invalidatePreview();
        renderSelection();
      });
      const text = node("span", "file-text");
      text.append(node("strong", "", file.path.split("/").at(-1)), node("small", "", file.path));
      const meta = node("span", "file-meta", `${file.kind === "tv" ? "Série?" : "Filme?"} · ${bytes(file.size)}`);
      row.append(check, text, meta);
      list.append(row);
    }
    el("page-label").textContent = `${state.page + 1} / ${pages} · ${number.format(files.length)} arquivos`;
    el("prev-page").disabled = state.page === 0;
    el("next-page").disabled = state.page >= pages - 1;
  }

  function renderSelection() {
    el("selected-count").textContent = `${state.selected.length} selecionado${state.selected.length === 1 ? "" : "s"}`;
    const list = el("selection-list");
    list.replaceChildren();
    if (!state.selected.length) list.append(node("p", "empty", "Marque um ou mais arquivos na lista."));
    for (const [index, path] of state.selected.entries()) {
      const row = node("div", "selection-row");
      row.append(node("span", "", `${index + 1}. ${path}`));
      const controls = node("span", "selection-controls");
      for (const [label, delta] of [["↑", -1], ["↓", 1]]) {
        const button = node("button", "secondary", label);
        button.type = "button";
        button.disabled = index + delta < 0 || index + delta >= state.selected.length;
        button.setAttribute("aria-label", delta < 0 ? "Subir episódio" : "Descer episódio");
        button.addEventListener("click", () => {
          [state.selected[index], state.selected[index + delta]] = [state.selected[index + delta], state.selected[index]];
          invalidatePreview(); renderSelection();
        });
        controls.append(button);
      }
      row.append(controls);
      list.append(row);
    }
    el("preview-button").disabled = state.busy || !state.chosen || !state.selected.length;
  }

  function invalidatePreview() {
    state.plan = null;
    state.planArgs = null;
    el("preview").hidden = true;
    el("organize-button").hidden = true;
  }

  function renderSearchResults(results) {
    const container = el("tmdb-results");
    container.replaceChildren();
    if (!results.length) container.append(node("p", "empty", "Nenhum resultado encontrado."));
    for (const result of results) {
      const button = node("button", "tmdb-result");
      button.type = "button";
      if (result.poster) {
        const image = node("img");
        image.src = result.poster;
        image.alt = "";
        image.loading = "lazy";
        button.append(image);
      } else button.append(node("span", "poster-placeholder", "Sem pôster"));
      const info = node("span");
      info.append(node("strong", "", result.title), node("small", "", `${result.type === "tv" ? "Série" : "Filme"} · ${result.year || "Ano desconhecido"} · TMDB ${result.id}`));
      button.append(info);
      button.addEventListener("click", () => {
        state.chosen = result;
        invalidatePreview();
        const chosen = el("chosen-media");
        chosen.replaceChildren(node("strong", "", result.title), node("span", "", result.type === "tv" ? "Série selecionada" : "Filme selecionado"));
        chosen.hidden = false;
        renderSelection();
      });
      container.append(button);
    }
  }

  function previewArgs() {
    if (!state.rootId || !state.chosen || !state.selected.length) throw new Error("Selecione arquivos e um título do TMDB.");
    return { root_id: state.rootId, sources: [...state.selected], media_type: state.chosen.type,
      tmdb_id: Number(state.chosen.id), season: Number(el("season-input").value),
      start_episode: Number(el("episode-input").value), rename: el("rename-input").checked,
      is_new: el("new-input").checked };
  }

  function relativePath(path) {
    const root = chosenRoot()?.path || "";
    return path.startsWith(root + "/") ? path.slice(root.length + 1) : path;
  }

  function renderPreview(plan) {
    const pane = el("preview");
    pane.replaceChildren();
    pane.append(node("h3", "", "Prévia da organização"), node("p", "", `${plan.title} → ${plan.folder_path}`));
    const moves = node("div", "preview-moves");
    for (const move of plan.moves) {
      const line = node("div", "preview-move");
      line.append(node("span", "", relativePath(move.from)), node("span", "arrow", "→"), node("strong", "", relativePath(move.to)));
      moves.append(line);
    }
    pane.append(moves, node("p", "help", "Ao confirmar, o banco e o manifesto serão atualizados. Uma cópia prévia do banco existente será mantida no SSD."));
    pane.hidden = false;
    el("organize-button").hidden = false;
  }

  function genreName(genre) { return genre.translated_name || genre.canonical_name; }

  function sortedGenres(genres) {
    return [...genres].sort((a, b) => Number(!!b.is_primary) - Number(!!a.is_primary)
      || genreName(a).localeCompare(genreName(b), "pt-BR") || a.id - b.id);
  }

  async function loadPoster(media, placeholder, rootId) {
    const key = `${rootId}/${media.id}`;
    try {
      let src = state.posterCache.get(key);
      if (src === undefined) {
        src = (await api("poster", { root_id: rootId, media_id: media.id })).src || null;
        state.posterCache.set(key, src);
        if (state.posterCache.size > 100) state.posterCache.delete(state.posterCache.keys().next().value);
      }
      if (src && placeholder.isConnected && rootId === state.rootId) {
        const image = node("img");
        image.src = src;
        image.alt = `Pôster de ${media.title}`;
        image.loading = "lazy";
        placeholder.replaceWith(image);
      }
    } catch (_) { /* Keep the text placeholder when a poster is unavailable. */ }
  }

  function queuePosters(jobs, rootId) {
    let next = 0;
    for (let worker = 0; worker < Math.min(4, jobs.length); worker++) {
      (async () => {
        while (next < jobs.length) {
          const [media, placeholder] = jobs[next++];
          await loadPoster(media, placeholder, rootId);
        }
      })();
    }
  }

  function renderCatalog() {
    const all = state.library?.media || [];
    const term = state.catalogFilter.toLocaleLowerCase("pt-BR");
    const filtered = all.filter(media => !term || [media.title, media.original_title, media.folder_path,
      ...media.genres.map(genreName)].some(value => (value || "").toLocaleLowerCase("pt-BR").includes(term)));
    const pages = Math.max(1, Math.ceil(filtered.length / catalogPageSize));
    state.catalogPage = Math.min(state.catalogPage, pages - 1);
    const visible = filtered.slice(state.catalogPage * catalogPageSize, (state.catalogPage + 1) * catalogPageSize);
    el("catalog-summary").textContent = state.library
      ? `${number.format(state.library.total)} mídia(s) no banco${state.library.total > all.length ? "; mostrando as primeiras 5.000" : ""}.`
      : "Selecione um catálogo para visualizar as mídias.";
    const grid = el("catalog-grid");
    grid.replaceChildren();
    const jobs = [];
    if (!visible.length) grid.append(node("p", "empty", state.library ? "Nenhuma mídia neste filtro." : "Aguardando catálogo."));
    for (const media of visible) {
      const card = node("button", `catalog-card${media.id === state.catalogMediaId ? " selected" : ""}`);
      card.type = "button";
      card.setAttribute("aria-pressed", String(media.id === state.catalogMediaId));
      const poster = node("span", "poster-placeholder", "Sem pôster");
      const info = node("span", "file-text");
      info.append(node("strong", "", media.title), node("small", "", media.media_type === "movie" ? "Filme" : "Série"),
        node("small", "", media.genres[0] ? genreName(media.genres[0]) : "Sem gênero"));
      card.append(poster, info);
      card.addEventListener("click", () => {
        state.catalogMediaId = media.id;
        state.genreDraft = media.genres.map(genre => ({ ...genre }));
        state.availableGenres = null;
        renderCatalog(); renderCatalogDetail();
      });
      grid.append(card);
      jobs.push([media, poster]);
    }
    queuePosters(jobs, state.rootId);
    el("catalog-page-label").textContent = `${state.catalogPage + 1} / ${pages} · ${number.format(filtered.length)} mídias`;
    el("catalog-prev").disabled = state.catalogPage === 0;
    el("catalog-next").disabled = state.catalogPage >= pages - 1;
  }

  function renderCatalogDetail() {
    const pane = el("catalog-detail");
    pane.replaceChildren();
    const media = state.library?.media.find(item => item.id === state.catalogMediaId);
    if (!media) {
      pane.append(node("p", "empty", "Selecione uma mídia para ver o pôster e editar os gêneros."));
      return;
    }
    const header = node("div", "catalog-detail-header");
    const poster = node("span", "poster-placeholder", "Sem pôster");
    const text = node("div");
    text.append(node("h2", "", media.title), node("p", "help", media.media_type === "movie" ? "Filme" : "Série"),
      node("p", "help", media.folder_path), node("p", "help", `TMDB ${media.tmdb_id}`));
    header.append(poster, text);
    pane.append(header, node("h3", "", "Gêneros · o primeiro é o principal"));
    queuePosters([[media, poster]], state.rootId);
    const draft = sortedGenres(state.genreDraft);
    for (const genre of draft) {
      const row = node("div", "catalog-genre-row");
      const label = node("label");
      const radio = node("input");
      radio.type = "radio";
      radio.name = "catalog-primary-genre";
      radio.checked = !!genre.is_primary;
      radio.addEventListener("change", () => {
        state.genreDraft = state.genreDraft.map(item => ({ ...item, is_primary: item.id === genre.id }));
        renderCatalogDetail();
      });
      label.append(radio, node("span", "", genreName(genre)));
      if (genre.is_primary) label.append(node("span", "pill", "Principal"));
      const remove = node("button", "secondary", "Remover");
      remove.type = "button";
      remove.disabled = draft.length === 1;
      remove.addEventListener("click", () => {
        state.genreDraft = state.genreDraft.filter(item => item.id !== genre.id);
        if (!state.genreDraft.some(item => item.is_primary) && state.genreDraft.length) state.genreDraft[0].is_primary = true;
        renderCatalogDetail();
      });
      row.append(label, remove);
      pane.append(row);
    }
    const addRow = node("div", "catalog-add-genre");
    if (state.availableGenres) {
      const select = node("select");
      select.setAttribute("aria-label", "Gênero para adicionar");
      const available = state.availableGenres.filter(genre => !state.genreDraft.some(item => item.id === genre.id))
        .sort((a, b) => genreName(a).localeCompare(genreName(b), "pt-BR"));
      for (const genre of available) {
        const option = node("option", "", genreName(genre));
        option.value = String(genre.id);
        select.append(option);
      }
      const add = node("button", "secondary", "Adicionar");
      add.type = "button";
      add.disabled = !available.length;
      add.addEventListener("click", () => {
        const genre = available.find(item => item.id === Number(select.value));
        if (genre) state.genreDraft.push({ ...genre, is_primary: !state.genreDraft.length });
        renderCatalogDetail();
      });
      addRow.append(select, add);
    } else {
      const load = node("button", "secondary", "+ Adicionar gênero do TMDB");
      load.type = "button";
      load.addEventListener("click", () => action(async () => {
        state.availableGenres = await api("tmdb-genres", { root_id: state.rootId, media_type: media.media_type });
        renderCatalogDetail();
      }));
      addRow.append(load);
    }
    pane.append(addRow);
    const save = node("button", "", "Salvar gêneros");
    save.type = "button";
    save.disabled = !state.genreDraft.length;
    save.addEventListener("click", () => action(async () => {
      const ordered = sortedGenres(state.genreDraft);
      const changed = await confirmEdit({ root_id: state.rootId, kind: "media-genres", media_id: media.id,
        genre_ids: ordered.map(genre => genre.id) });
      if (changed) message("Gêneros atualizados no catálogo.");
    }));
    pane.append(save, node("p", "help", "Ao trocar o gênero principal de um filme, sua pasta muda para a nova categoria."));
  }

  function renderTranslations() {
    const list = el("genre-translation-list");
    list.replaceChildren();
    const settings = state.catalogSettings;
    el("season-label-input").value = settings?.labels.season_label || "Season";
    el("season-label-input").disabled = !chosenRoot()?.database;
    el("season-label-form").querySelector("button").disabled = !chosenRoot()?.database;
    if (!settings?.genres.length) {
      list.append(node("p", "empty", "Nenhum gênero registrado neste catálogo."));
      return;
    }
    for (const genre of settings.genres) {
      const row = node("div", "translation-row");
      const title = node("div");
      title.append(node("strong", "", genre.canonical_name), node("small", "", genre.media_type === "movie" ? "Filme" : "Série"));
      const label = node("label", "", "Nome exibido");
      const input = node("input");
      input.type = "text";
      input.maxLength = 80;
      input.placeholder = genre.canonical_name;
      input.value = genre.translated_name || "";
      label.append(input);
      const save = node("button", "secondary", "Salvar");
      save.type = "button";
      save.addEventListener("click", () => action(async () => {
        const changed = await confirmEdit({ root_id: state.rootId, kind: "genre-translation",
          genre_id: genre.id, media_type: genre.media_type, translated: input.value });
        if (changed) message("Tradução do gênero atualizada.");
      }));
      row.append(title, label, save);
      list.append(row);
    }
  }

  async function refreshCatalog() {
    const rootId = state.rootId;
    if (!chosenRoot()?.available) { clearCatalog(); return; }
    const [library, settings] = await Promise.all([
      api("library", { root_id: rootId }), api("catalog-settings", { root_id: rootId })]);
    if (rootId !== state.rootId) return;
    state.library = library;
    state.catalogSettings = settings;
    state.posterCache.clear();
    const media = library.media.find(item => item.id === state.catalogMediaId);
    if (!media) state.catalogMediaId = null;
    state.genreDraft = media ? media.genres.map(genre => ({ ...genre })) : [];
    state.availableGenres = null;
    renderCatalog(); renderCatalogDetail(); renderTranslations();
  }

  async function confirmEdit(payload) {
    const preview = await api("preview-edit", payload);
    if (preview.no_change) { message("Nenhuma alteração a salvar."); return false; }
    const paths = preview.moves.slice(0, 5).map(move => `${relativePath(move.from)} → ${relativePath(move.to)}`);
    const more = preview.moves.length > 5 ? `\n… e mais ${preview.moves.length - 5} pasta(s).` : "";
    const summary = `${preview.summary}. ${preview.moves.length} pasta(s) serão movidas.\n${paths.join("\n")}${more}\n\nConfirmar?`;
    if (!window.confirm(summary)) return false;
    try { await api("apply-edit", { ...payload, token: preview.token }); }
    catch (error) { await refreshStatus(); throw error; }
    await refreshCatalog();
    if (preview.moves.length) await scanSelected();
    return true;
  }

  el("settings-toggle").addEventListener("click", () => {
    el("settings").hidden = !el("settings").hidden;
    el("settings-toggle").setAttribute("aria-expanded", String(!el("settings").hidden));
  });
  el("root-select").addEventListener("change", event => {
    state.rootId = event.target.value;
    window.localStorage.setItem("cockpit-sorta-root", state.rootId);
    clearWorkspace(); clearCatalog(); renderRoots();
    if (chosenRoot()?.available) action(async () => { await scanSelected(); await refreshCatalog(); });
  });
  for (const [tab, pane, otherTab, otherPane] of [
    ["tab-pending", "pending-pane", "tab-catalog", "catalog-pane"],
    ["tab-catalog", "catalog-pane", "tab-pending", "pending-pane"]]) {
    el(tab).addEventListener("click", () => {
      el(pane).hidden = false;
      el(otherPane).hidden = true;
      el(tab).classList.add("active");
      el(otherTab).classList.remove("active");
      el(tab).setAttribute("aria-current", "page");
      el(otherTab).removeAttribute("aria-current");
    });
  }
  el("scan-button").addEventListener("click", () => action(scanSelected));
  async function scanSelected() {
    if (!state.rootId) throw new Error("Selecione uma pasta.");
    state.scan = await api("scan", { root_id: state.rootId });
    state.selected = [];
    invalidatePreview(); renderFiles(); renderSelection();
  }
  el("file-filter").addEventListener("input", event => { state.filter = event.target.value; state.page = 0; renderFiles(); });
  el("catalog-filter").addEventListener("input", event => { state.catalogFilter = event.target.value; state.catalogPage = 0; renderCatalog(); });
  el("catalog-prev").addEventListener("click", () => { state.catalogPage--; renderCatalog(); });
  el("catalog-next").addEventListener("click", () => { state.catalogPage++; renderCatalog(); });
  el("catalog-refresh").addEventListener("click", () => action(refreshCatalog));
  el("season-label-form").addEventListener("submit", event => {
    event.preventDefault();
    action(async () => {
      const changed = await confirmEdit({ root_id: state.rootId, kind: "season-label", label: el("season-label-input").value });
      if (changed) message("Nome da pasta de temporada atualizado.");
    });
  });
  el("prev-page").addEventListener("click", () => { state.page--; renderFiles(); });
  el("next-page").addEventListener("click", () => { state.page++; renderFiles(); });
  for (const id of ["season-input", "episode-input", "rename-input", "new-input"]) el(id).addEventListener("change", invalidatePreview);
  el("tmdb-search-form").addEventListener("submit", event => {
    event.preventDefault();
    action(async () => {
      const results = el("tmdb-results");
      results.replaceChildren(node("p", "empty", "Buscando no TMDB…"));
      try { renderSearchResults(await api("search", { query: el("tmdb-query").value })); }
      catch (error) {
        results.replaceChildren(node("p", "empty search-error", error.message || String(error)));
        throw error;
      }
    });
  });
  el("preview-button").addEventListener("click", () => action(async () => {
    invalidatePreview();
    state.planArgs = previewArgs();
    state.plan = await api("preview", state.planArgs);
    renderPreview(state.plan);
  }));
  el("organize-button").addEventListener("click", () => action(async () => {
    if (!state.plan || !state.planArgs) throw new Error("Faça uma prévia primeiro.");
    const result = await api("organize", { ...state.planArgs, token: state.plan.token });
    message(`${result.files} arquivo(s) organizado(s) em ${result.folder_path}.`);
    state.selected = []; invalidatePreview(); renderSelection();
    await refreshStatus();
    await scanSelected();
    await refreshCatalog();
  }));
  el("recover-button").addEventListener("click", () => action(async () => {
    const result = await api("recover", { root_id: state.rootId });
    await refreshStatus();
    if (chosenRoot()?.available) await scanSelected();
    await refreshCatalog();
    message(result.status === "rolled_back" ? "Movimentação anterior desfeita." : "Catálogo recuperado.");
  }));
  el("add-root-form").addEventListener("submit", event => {
    event.preventDefault();
    action(async () => {
      const result = await api("add-root", { mountpoint: el("disk-select").value, folder: el("folder-input").value, label: el("label-input").value });
      state.rootId = result.id;
      window.localStorage.setItem("cockpit-sorta-root", state.rootId);
      await refreshStatus();
      await scanSelected();
      await refreshCatalog();
      message("Pasta adicionada ao catálogo.");
    });
  });
  el("key-form").addEventListener("submit", event => {
    event.preventDefault();
    action(async () => {
      await api("set-key", { key: el("key-input").value });
      el("key-input").value = "";
      await refreshStatus();
      message("Chave TMDB salva no servidor.");
    });
  });

  action(async () => {
    await refreshStatus();
    state.disks = await api("disks");
    renderDisks();
    if (chosenRoot()?.available) await scanSelected();
    await refreshCatalog();
  });
})();
