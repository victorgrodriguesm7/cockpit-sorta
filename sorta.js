(() => {
  "use strict";

  const el = id => document.getElementById(id);
  const number = new Intl.NumberFormat("pt-BR");
  const state = { roots: [], disks: [], rootId: "", scan: null, selected: [], chosen: null,
    plan: null, planArgs: null, filter: "", page: 0, busy: false };
  const pageSize = 80;

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
        clearWorkspace();
        if (chosenRoot()?.available) await scanSelected();
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

  el("settings-toggle").addEventListener("click", () => {
    el("settings").hidden = !el("settings").hidden;
    el("settings-toggle").setAttribute("aria-expanded", String(!el("settings").hidden));
  });
  el("root-select").addEventListener("change", event => {
    state.rootId = event.target.value;
    window.localStorage.setItem("cockpit-sorta-root", state.rootId);
    clearWorkspace(); renderRoots();
    if (chosenRoot()?.available) action(scanSelected);
  });
  el("scan-button").addEventListener("click", () => action(scanSelected));
  async function scanSelected() {
    if (!state.rootId) throw new Error("Selecione uma pasta.");
    state.scan = await api("scan", { root_id: state.rootId });
    state.selected = [];
    invalidatePreview(); renderFiles(); renderSelection();
  }
  el("file-filter").addEventListener("input", event => { state.filter = event.target.value; state.page = 0; renderFiles(); });
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
    await scanSelected();
  }));
  el("recover-button").addEventListener("click", () => action(async () => {
    const result = await api("recover", { root_id: state.rootId });
    await refreshStatus();
    if (chosenRoot()?.available) await scanSelected();
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
  });
})();
