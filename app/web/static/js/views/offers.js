// Экран «Офферы»: где оффер используется, куда его ещё можно добавить, массовое добавление
// в черновики и публикация пачкой. В админке Keitaro такого поиска по потокам нет.

import { api } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { autocomplete, confirmDialog, reportError, toast, withBusy } from "../ui.js";

export async function renderOffers(container) {
  const state = { offer: null, usage: null, picked: new Set() };
  const body = h("div", { class: "stack" });

  let usageRequestNo = 0;
  async function loadUsage() {
    if (!state.offer) return;
    const mine = ++usageRequestNo;
    try {
      const usage = await api("GET", `/api/offers/${state.offer.id}/usage`);
      if (mine !== usageRequestNo) return; // успели выбрать другой оффер — этот ответ устарел
      state.usage = usage;
      state.picked.clear();
      paint();
    } catch (error) { reportError(error); }
  }

  async function syncAll(button, onlyMissing) {
    try {
      await withBusy(button, async () => {
        const result = await api("POST", "/api/campaigns/sync-all", { only_missing: onlyMissing },
          { timeoutMs: 600000 });
        toast(`Синхронизировано кампаний: ${result.synced}` + (result.failed.length ? `, с ошибкой: ${result.failed.length}` : "") + ".",
          result.failed.length ? "amber" : "green");
        await loadUsage();
      });
    } catch (error) { reportError(error); }
  }

  async function bulkAdd(button) {
    const ids = [...state.picked];
    // Оффер берём из той же выдачи, по которой нарисована таблица: при быстром перевыборе
    // state.offer уже может указывать на другой оффер, чем отмеченные галочками потоки.
    const offerId = state.usage.offer.id;
    try {
      await withBusy(button, async () => {
        let added = 0;
        let total = 0;
        for (let start = 0; start < ids.length; start += 100) { // сервер принимает до 100 потоков за раз
          const data = await api("POST", `/api/offers/${offerId}/bulk-add`, { stream_ids: ids.slice(start, start + 100) });
          added += data.added;
          total += data.results.length;
        }
        toast(`Оффер добавлен в черновики: ${added} из ${total}. В Keitaro пока ничего не отправлено.`);
        await loadUsage();
      });
    } catch (error) { reportError(error); }
  }

  async function pushMany(button, ids) {
    const sure = await confirmDialog("Опубликовать потоки в Keitaro?",
      `Будет опубликовано потоков: ${ids.length}. Каждый пройдёт обычную проверку: если поток успели `
      + "поменять в Keitaro, он будет пропущен и останется черновиком.", "Опубликовать", "primary");
    if (!sure) return;
    try {
      await withBusy(button, async () => {
        const data = await api("POST", "/api/streams/push-many", { stream_ids: ids }, { timeoutMs: 600000 });
        const failed = data.results.filter((r) => r.status !== "pushed");
        toast(`Опубликовано потоков: ${data.pushed} из ${data.results.length}.`
          + (failed.length ? ` Пропущены: ${failed.map((r) => `«${r.campaign_name || r.stream_id}» — ${r.message}`).join("; ")}` : ""),
        failed.length ? "amber" : "green", { timeout: failed.length ? 15000 : 5000 });
        await loadUsage();
      });
    } catch (error) { reportError(error); }
  }

  function usedTable(rows) {
    if (!rows.length) return h("div", { class: "empty" }, h("strong", {}, "Оффер нигде не стоит"),
      "Ни в одном потоке, известном AdRobot, его нет.");
    return h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Кампания"), h("th", {}, "Поток"), h("th", { class: "t-right" }, "Доля"),
        h("th", {}, "Состояние"))),
      h("tbody", {}, rows.map((row) => h("tr", {},
        h("td", {}, h("a", { class: "row-link", href: `#/campaigns/${row.campaign_id}` }, row.campaign_name),
          h("span", { class: "muted small mono" }, ` #${row.keitaro_campaign_id}`)),
        h("td", {}, row.stream_name, h("span", { class: "muted small mono" }, ` ${row.keitaro_stream_id}`)),
        h("td", { class: "t-right mono" }, `${row.share}%`,
          row.is_pinned ? h("span", { class: "muted small" }, " закреплено") : null),
        h("td", { class: "nowrap" }, row.state === "removed"
          ? [h("span", { class: "lamp lamp--off", "aria-hidden": "true" }), " в архиве", row.pending ? " (ещё не опубликовано)" : ""]
          : row.pending ? [h("span", { class: "lamp lamp--amber", "aria-hidden": "true" }), " в черновике"]
            : [h("span", { class: "lamp lamp--green", "aria-hidden": "true" }), " в Keitaro"]))))));
  }

  function availableTable(rows) {
    if (!rows.length) return h("div", { class: "empty" }, "Свободных потоков нет: оффер уже стоит везде, где есть офферы.");
    const all = rows.map((r) => r.stream_id);
    const allPicked = all.every((id) => state.picked.has(id));
    return h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {},
        h("th", {}, h("input", { type: "checkbox", checked: allPicked, "aria-label": "Выбрать все потоки",
          onchange: (event) => { all.forEach((id) => (event.target.checked ? state.picked.add(id) : state.picked.delete(id))); paint(); } })),
        h("th", {}, "Кампания"), h("th", {}, "Поток"), h("th", {}, "Гео"), h("th", { class: "t-right" }, "Офферов сейчас"))),
      h("tbody", {}, rows.map((row) => h("tr", {},
        h("td", {}, h("input", { type: "checkbox", checked: state.picked.has(row.stream_id),
          "aria-label": `Выбрать поток ${row.stream_name} кампании ${row.campaign_name}`,
          onchange: (event) => { event.target.checked ? state.picked.add(row.stream_id) : state.picked.delete(row.stream_id); paint(); } })),
        h("td", {}, h("a", { class: "row-link", href: `#/campaigns/${row.campaign_id}` }, row.campaign_name),
          row.is_dirty ? h("span", { class: "badge badge--amber" }, "черновик") : null),
        h("td", {}, row.stream_name),
        h("td", {}, (row.geo || []).length ? h("span", { class: "chips" }, row.geo.map((g) => h("span", { class: "chip chip--geo" }, g))) : h("span", { class: "muted" }, "—")),
        h("td", { class: "t-right mono" }, row.active_offers))))));
  }

  function paint() {
    if (!state.offer) {
      mount(body, h("div", { class: "card empty" }, h("strong", {}, "Выберите оффер"),
        "Начните вводить ID или название — покажу все потоки, где он стоит, и куда его можно добавить."));
      return;
    }
    const usage = state.usage;
    if (!usage) { mount(body, h("div", { class: "skeleton" })); return; }
    const pendingIds = usage.used.filter((r) => r.pending).map((r) => r.stream_id);
    mount(body,
      usage.has_unsynced_campaigns ? h("div", { class: "banner banner--warn" },
        "Не все кампании трекера загружены в AdRobot, поэтому список может быть неполным.",
        h("div", {}, h("button", { class: "btn btn--sm", onclick: (event) => syncAll(event.currentTarget, true) },
          icon("download"), "Загрузить недостающие"))) : null,
      h("div", { class: "card" }, h("div", { class: "card__body row" },
        h("h2", { class: "grow" }, `Где используется: [${usage.offer.id}] ${usage.offer.name}`),
        pendingIds.length ? h("button", { class: "btn btn--primary", onclick: (event) => pushMany(event.currentTarget, pendingIds) },
          icon("upload"), `Опубликовать черновики (${pendingIds.length})`) : null),
      usedTable(usage.used)),
      h("div", { class: "card" }, h("div", { class: "card__body row" },
        h("h2", { class: "grow" }, "Куда можно добавить"),
        h("button", { class: "btn btn--primary", disabled: state.picked.size === 0,
          title: "Добавить в черновики выбранных потоков; доли пересчитаются, в Keitaro ничего не уйдёт до Push",
          onclick: (event) => bulkAdd(event.currentTarget) }, icon("plus"), `Добавить в выбранные (${state.picked.size})`)),
      availableTable(usage.available)));
  }

  const picker = autocomplete({
    placeholder: "Оффер: ID или часть названия", ariaLabel: "Поиск оффера",
    search: (query) => api("GET", `/api/offers?q=${encodeURIComponent(query)}&limit=30&include_inactive=true`),
    onPick: (offer) => { state.offer = offer; state.usage = null; picker.input.value = offer.label; paint(); loadUsage(); },
  });

  mount(container,
    h("div", { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", {}, "Офферы"),
        h("div", { class: "meta-line" }, "Где стоит оффер, куда его добавить и публикация сразу в нескольких потоках.")),
      h("div", { class: "page-head__actions" },
        h("button", { class: "btn", title: "Fetch streams по всем кампаниям трекера — нужен, чтобы поиск видел всё",
          onclick: (event) => syncAll(event.currentTarget, false) }, icon("refresh"), "Синхронизировать все кампании"))),
    h("div", { class: "stack" }, h("div", { class: "card" }, h("div", { class: "card__body" }, picker.root)), body));
  paint();
}
