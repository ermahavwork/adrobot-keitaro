// Список кампаний: то, что AdRobot знает о трекере. Отсюда открывается редактор
// любой существующей кампании — и созданной у нас, и заведённой руками в Keitaro.

import { api } from "../api.js";
import { h, icon, mount, debounce, fmtDate } from "../dom.js";
import { reportError, toast, withBusy } from "../ui.js";

export async function renderCampaigns(container) {
  const state = { q: "", origin: "", onlyDrafts: false, data: null, syncing: false };

  const tableHost = h("div", { class: "card" }, h("div", { class: "skeleton" }));

  async function load() {
    const params = new URLSearchParams({ q: state.q, origin: state.origin, limit: "200",
      only_drafts: String(state.onlyDrafts) });
    try {
      state.data = await api("GET", `/api/campaigns?${params}`);
      paintTable();
    } catch (error) {
      mount(tableHost, h("div", { class: "empty" }, h("strong", {}, "Не удалось загрузить список"), error.message));
    }
  }

  async function syncFromKeitaro(button) {
    try {
      await withBusy(button, async () => {
        const result = await api("POST", "/api/campaigns/import");
        toast(`Список обновлён из Keitaro: всего ${result.total}, новых ${result.created}`
          + (result.gone ? `, исчезло ${result.gone}` : "") + ".");
        await load();
      });
    } catch (error) { reportError(error); }
  }

  function paintTable() {
    const items = state.data.items;
    if (!items.length) {
      const filtered = state.q || state.origin || state.onlyDrafts;
      mount(tableHost, h("div", { class: "empty" },
        h("strong", {}, filtered ? "Под фильтр ничего не подошло" : "AdRobot пока не знает ни одной кампании"),
        filtered ? "Измените запрос или сбросьте фильтры."
          : "Нажмите «Обновить из Keitaro», чтобы подтянуть существующие, или создайте новую."));
      return;
    }
    mount(tableHost, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Кампания"), h("th", {}, "KT ID"), h("th", {}, "Alias"),
        h("th", {}, "Гео"), h("th", {}, "Откуда"), h("th", {}, "Состояние"), h("th", {}, "Синхронизация"), h("th", {}, ""))),
      h("tbody", {}, items.map((c) => h("tr", {},
        h("td", {}, h("a", { class: "row-link", href: `#/campaigns/${c.id}` }, c.name)),
        h("td", { class: "mono" }, c.keitaro_id),
        h("td", { class: "mono small" }, c.alias || "—"),
        h("td", {}, c.geo.length ? h("span", { class: "chips" }, c.geo.map((g) => h("span", { class: "chip chip--geo" }, g))) : h("span", { class: "muted" }, "—")),
        h("td", {}, h("span", { class: "badge" }, c.origin === "adrobot" ? "AdRobot" : "Keitaro")),
        h("td", { class: "nowrap" }, c.has_draft
          ? [h("span", { class: "lamp lamp--amber", "aria-hidden": "true" }), " есть черновик"]
          : c.state !== "active" ? h("span", { class: "muted" }, c.state)
            : [h("span", { class: "lamp lamp--green", "aria-hidden": "true" }), " active"]),
        h("td", { class: "small muted nowrap" }, fmtDate(c.streams_fetched_at)),
        h("td", { class: "t-right nowrap" }, c.admin_url ? h("a", { class: "btn btn--sm btn--quiet", href: c.admin_url,
          target: "_blank", rel: "noopener noreferrer", title: "Открыть в админке Keitaro" }, icon("external")) : null)))))),
    h("div", { class: "card__body small muted" }, `Показано ${items.length} из ${state.data.total}`));
  }

  const search = h("input", { class: "input", type: "search", placeholder: "Поиск: название, alias или KT ID",
    "aria-label": "Поиск по кампаниям", oninput: debounce((event) => { state.q = event.target.value; load(); }, 250) });
  const openById = h("input", { class: "input", type: "number", min: "1", placeholder: "KT ID", style: "width:120px",
    "aria-label": "ID кампании в Keitaro" });

  mount(container,
    h("div", { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", {}, "Кампании"),
        h("div", { class: "meta-line" }, "Откройте кампанию, чтобы управлять офферами её потоков.")),
      h("div", { class: "page-head__actions" },
        h("button", { class: "btn", onclick: (event) => syncFromKeitaro(event.currentTarget) }, icon("refresh"), "Обновить из Keitaro"),
        h("a", { class: "btn btn--primary", href: "#/new" }, icon("plus"), "Новая кампания"))),
    h("div", { class: "stack" },
      h("div", { class: "row" },
        h("div", { class: "grow" }, search),
        h("select", { class: "select", style: "width:auto", "aria-label": "Откуда кампания",
          onchange: (event) => { state.origin = event.target.value; load(); } },
        h("option", { value: "" }, "Все"), h("option", { value: "adrobot" }, "Созданы в AdRobot"),
        h("option", { value: "keitaro" }, "Импортированы")),
        h("label", { class: "check" }, h("input", { type: "checkbox",
          onchange: (event) => { state.onlyDrafts = event.target.checked; load(); } }), "только с черновиками"),
        h("form", { class: "row", onsubmit: async (event) => {
          event.preventDefault();
          const id = Number.parseInt(openById.value, 10);
          if (!id) return;
          try {
            const opened = await api("POST", `/api/campaigns/open/${id}`);
            location.hash = `#/campaigns/${opened.id}`;
          } catch (error) { reportError(error); }
        } }, openById, h("button", { class: "btn", type: "submit", title: "Открыть кампанию Keitaro по её ID" }, "Открыть по ID"))),
      tableHost));

  await load();
  if (state.data && state.data.total === 0 && !state.q) {
    // Первый запуск: сразу подтягиваем кампании из трекера, чтобы экран не был пустым.
    try {
      const result = await api("POST", "/api/campaigns/import");
      if (result.total) await load();
    } catch { /* нет связи с Keitaro — останется подсказка «Обновить из Keitaro» */ }
  }
}
