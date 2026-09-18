// Диалог советника долей: «сейчас → предлагается → почему». Советник ничего не публикует:
// «Применить» кладёт цифры в черновик потока, дальше — обычные Push to KT или Cancel.

import { api } from "../api.js";
import { h, mount } from "../dom.js";
import { dialog, reportError } from "../ui.js";

const METRICS = [["cr", "по конверсии (CR)"], ["epc", "по доходу с клика (EPC)"]];
let opened = false; // второй клик по «Советник», пока диалог открыт, второй диалог не создаёт

/** Открывает диалог. onApplied(stream) вызывается со свежим видом потока после применения. */
export async function openAdvisor(stream, period, onApplied) {
  if (opened) return;
  opened = true;
  let metric = "cr";
  let advice = null;
  let requestNo = 0;
  const host = h("div", { class: "stack" }, h("div", { class: "skeleton" }));

  async function load() {
    const mine = ++requestNo;
    advice = null;
    mount(host, h("div", { class: "skeleton" }));
    let fresh;
    try {
      fresh = await api("GET", `/api/streams/${stream.id}/advice?period=${period}&metric=${metric}`);
    } catch (error) {
      if (mine === requestNo) mount(host, h("div", { class: "banner banner--error" }, error.message));
      return;
    }
    if (mine !== requestNo) return; // пока ждали, метрику переключили — этот ответ устарел
    advice = fresh;
    const controls = h("label", { class: "field" }, h("span", { class: "field__label" }, "Сравнивать офферы"),
      h("select", { class: "select", onchange: (event) => { metric = event.target.value; load(); } },
        METRICS.map(([value, label]) => h("option", { value, selected: value === metric }, label))));
    if (!advice.ready) {
      mount(host, controls, h("div", { class: "banner banner--warn" }, advice.message));
      return;
    }
    mount(host, controls, h("p", { class: "small muted" }, advice.message),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Оффер"), h("th", { class: "t-right" }, "Сейчас"),
          h("th", { class: "t-right" }, "Предлагается"), h("th", {}, "Почему"))),
        h("tbody", {}, advice.items.map((item) => h("tr", {},
          h("td", {}, h("span", { class: "offer__id" }, `[${item.offer_id}]`), item.offer_name),
          h("td", { class: "t-right mono" }, `${item.current}%`),
          h("td", { class: "t-right mono" }, h("strong", {}, `${item.proposed}%`)),
          h("td", { class: "small" }, item.reason)))))),
      advice.changed ? null : h("p", { class: "small muted" }, "Текущее распределение уже соответствует статистике."));
  }

  // Диалог показываем сразу, а отчёты грузим уже внутри него: два запроса статистики к трекеру
  // идут секунды, и всё это время человек должен видеть, что клик принят.
  load();
  let apply = false;
  try {
    apply = await dialog({
      title: `Советник долей · ${stream.name}`, wide: true, body: host,
      actions: [{ label: "Закрыть", value: false },
        { label: "Применить в черновик", kind: "primary", value: true }],
    });
  } finally {
    opened = false;
  }
  if (!apply) return;
  if (!advice || !advice.ready || !advice.changed) return;
  const shares = Object.fromEntries(advice.items.filter((i) => i.proposed !== i.current)
    .map((i) => [i.binding_id, i.proposed]));
  try {
    onApplied(await api("POST", `/api/streams/${stream.id}/apply-shares`, { shares }));
  } catch (error) { reportError(error); }
}
