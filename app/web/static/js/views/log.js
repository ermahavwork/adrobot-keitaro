// Журнал операций: каждая кнопка AdRobot оставляет здесь запись — и успех, и ошибку.

import { api } from "../api.js";
import { h, mount, fmtDate } from "../dom.js";

const ACTIONS = {
  create_campaign: "Создание кампании", create_campaign_dry_run: "Проверка без создания",
  import_campaigns: "Обновление списка", fetch_streams: "Fetch streams", add_offer: "Add",
  remove_offer: "Remove", bring_back: "Bring back", pin: "Pin", unpin: "Unpin", set_share: "Доля вручную",
  recalculate: "Пересчёт долей", equalize: "Выравнивание долей", push: "Push to KT", cancel: "Cancel",
  restore_snapshot: "Откат к снимку", forget_offer: "Удаление из архива", archive_campaign: "Кампания в архив",
};

export async function renderLog(container) {
  const state = { status: "", offset: 0, limit: 50 };
  const host = h("div", { class: "card" }, h("div", { class: "skeleton" }));

  async function load() {
    const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
    if (state.status) params.set("status", state.status);
    let data;
    try { data = await api("GET", `/api/operations?${params}`); }
    catch (error) { mount(host, h("div", { class: "empty" }, h("strong", {}, "Журнал недоступен"), error.message)); return; }
    if (!data.items.length) {
      mount(host, h("div", { class: "empty" }, h("strong", {}, "Записей нет"),
        "Создайте кампанию или нажмите Fetch в редакторе — операции появятся здесь."));
      return;
    }
    mount(host, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Когда"), h("th", {}, "Операция"), h("th", {}, "Итог"),
        h("th", {}, "Кампания / поток"), h("th", {}, "Что произошло"), h("th", {}, "Кто"), h("th", { class: "t-right" }, "мс"))),
      h("tbody", {}, data.items.map((op) => h("tr", {},
        h("td", { class: "small nowrap mono" }, fmtDate(op.created_at)),
        h("td", { class: "nowrap" }, ACTIONS[op.action] || op.action),
        h("td", {}, h("span", { class: `badge badge--${op.status === "ok" ? "green" : "red"}` }, op.status === "ok" ? "ok" : "ошибка")),
        h("td", { class: "mono small nowrap" }, [op.keitaro_campaign_id ? `#${op.keitaro_campaign_id}` : "—",
          op.keitaro_stream_id ? ` / ${op.keitaro_stream_id}` : ""]),
        h("td", { class: "small" }, op.error || op.summary || "—"),
        h("td", { class: "small muted" }, op.actor || "—"),
        h("td", { class: "t-right mono small" }, op.duration_ms)))))),
    h("div", { class: "card__body row" },
      h("span", { class: "small muted grow" }, `${state.offset + 1}–${state.offset + data.items.length} из ${data.total}`),
      h("button", { class: "btn btn--sm", disabled: state.offset === 0,
        onclick: () => { state.offset = Math.max(0, state.offset - state.limit); load(); } }, "← новее"),
      h("button", { class: "btn btn--sm", disabled: state.offset + state.limit >= data.total,
        onclick: () => { state.offset += state.limit; load(); } }, "старше →")));
  }

  mount(container,
    h("div", { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", {}, "Журнал операций"),
        h("div", { class: "meta-line" }, "Журнал Keitaro видит только API-ключ; здесь видно, какая кнопка что сделала.")),
      h("div", { class: "page-head__actions" },
        h("select", { class: "select", style: "width:auto", "aria-label": "Фильтр по итогу",
          onchange: (event) => { state.status = event.target.value; state.offset = 0; load(); } },
        h("option", { value: "" }, "Все записи"), h("option", { value: "ok" }, "Только успешные"),
        h("option", { value: "error" }, "Только ошибки")))),
    host);
  await load();
}
