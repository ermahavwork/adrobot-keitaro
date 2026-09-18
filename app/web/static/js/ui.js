// Общие элементы интерфейса: тосты, диалоги, автокомплит, шкала долей, спарклайн.

import { h, icon, debounce } from "./dom.js";
import { ApiError } from "./api.js";

// ---------------------------------------------------------------- тосты

export function toast(message, kind = "green", { timeout = 5000 } = {}) {
  const root = document.getElementById("toasts");
  const node = h("div", { class: "toast" },
    h("span", { class: `lamp lamp--${kind}`, "aria-hidden": "true" }),
    h("div", {}, message),
    h("button", { class: "toast__close", "aria-label": "Закрыть", onclick: () => node.remove() }, "×"));
  root.append(node);
  if (timeout) setTimeout(() => node.remove(), kind === "red" ? timeout * 2 : timeout);
}

/** Показывает ошибку тостом; возвращает её же, чтобы вызывающий мог разобрать `code`. */
export function reportError(error) {
  const message = error instanceof ApiError ? error.message : `Неожиданная ошибка: ${error.message || error}`;
  toast(message, "red");
  if (!(error instanceof ApiError)) console.error(error);
  return error;
}

/** Кнопка на время операции блокируется и показывает индикатор — защита от двойного клика. */
export async function withBusy(button, task) {
  if (!button || button.disabled) return undefined;
  button.disabled = true;
  button.classList.add("btn--busy");
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.classList.remove("btn--busy");
  }
}

// ---------------------------------------------------------------- диалоги

/**
 * Модальное окно. `actions`: [{label, kind, value}] — промис вернёт value нажатой кнопки,
 * Esc и клик по фону возвращают null. Фокус заперт внутри окна и возвращается обратно.
 */
export function dialog({ title, body, actions, wide = false }) {
  return new Promise((resolve) => {
    const root = document.getElementById("modal-root");
    const previouslyFocused = document.activeElement;
    const close = (value) => {
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
      resolve(value);
    };
    const buttons = actions.map((action) => h("button", {
      class: ["btn", action.kind ? `btn--${action.kind}` : ""], onclick: () => close(action.value),
    }, action.label));
    const modal = h("div", { class: ["modal", wide ? "modal--wide" : ""], role: "dialog",
      "aria-modal": "true", "aria-label": title },
    h("div", { class: "modal__head" }, h("h2", {}, title)),
    h("div", { class: "modal__body" }, body),
    h("div", { class: "modal__foot" }, buttons));
    const backdrop = h("div", { class: "modal-backdrop",
      onmousedown: (event) => { if (event.target === backdrop) close(null); } }, modal);

    function onKey(event) {
      if (event.key === "Escape") { event.preventDefault(); close(null); return; }
      if (event.key !== "Tab") return;
      const focusable = modal.querySelectorAll("button, a[href], input, select, textarea");
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", onKey, true);
    root.append(backdrop);
    (buttons[buttons.length - 1] || modal).focus();
  });
}

export const confirmDialog = (title, body, confirmLabel, kind = "primary") => dialog({
  title, body: typeof body === "string" ? h("p", {}, body) : body,
  actions: [{ label: "Отмена", value: false }, { label: confirmLabel, kind, value: true }],
}).then(Boolean);

// ---------------------------------------------------------------- автокомплит

/**
 * Поле поиска с выпадающим списком (замена Select2 из оригинала).
 * search(query) -> Promise<[{id, name, ...}]>; onPick(item) вызывается по Enter или клику.
 * Клавиатура: ↑ ↓ — выбор, Enter — взять, Esc — закрыть.
 */
export function autocomplete({ placeholder, search, onPick, ariaLabel, renderItem }) {
  let items = [];
  let active = -1;
  let requestNo = 0;
  const listId = `ac-${Math.random().toString(36).slice(2, 8)}`;
  const input = h("input", { class: "input", type: "text", placeholder, autocomplete: "off",
    role: "combobox", "aria-expanded": "false", "aria-controls": listId, "aria-autocomplete": "list",
    "aria-label": ariaLabel || placeholder });
  const list = h("ul", { class: "ac__list", id: listId, role: "listbox", hidden: true });
  const root = h("div", { class: "ac" }, input, list);

  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
  const open = () => { list.hidden = false; input.setAttribute("aria-expanded", "true"); };

  function paint(state) {
    list.replaceChildren();
    if (state) { list.append(h("li", { class: "ac__empty" }, state)); open(); return; }
    items.forEach((item, index) => {
      list.append(h("li", { class: "ac__item", role: "option", id: `${listId}-${index}`,
        "aria-selected": String(index === active),
        onmousedown: (event) => { event.preventDefault(); pick(index); },
        onmousemove: () => { if (active !== index) { active = index; paint(); } } },
      renderItem ? renderItem(item) : [h("span", { class: "ac__id" }, item.id), h("span", {}, item.name)]));
    });
    if (active >= 0) input.setAttribute("aria-activedescendant", `${listId}-${active}`);
    else input.removeAttribute("aria-activedescendant");
    open();
  }

  function pick(index) {
    const item = items[index];
    if (!item) return;
    input.value = "";
    close();
    onPick(item);
  }

  const run = debounce(async () => {
    const mine = ++requestNo;
    paint("Ищу…");
    try {
      const found = await search(input.value.trim());
      if (mine !== requestNo) return; // пришёл ответ на устаревший запрос
      items = found;
      active = found.length ? 0 : -1;
      paint(found.length ? null : "Ничего не найдено");
    } catch (error) {
      if (mine === requestNo) paint(error.message || "Не удалось выполнить поиск");
    }
  }, 220);

  input.addEventListener("input", run);
  input.addEventListener("focus", run);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) { run(); return; }
      if (!items.length) return;
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      paint();
      list.children[active]?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      if (!list.hidden && active >= 0) { event.preventDefault(); pick(active); }
    } else if (event.key === "Escape") {
      close();
    }
  });
  return { root, input };
}

// ---------------------------------------------------------------- шкала долей

export const segmentColor = (index) => `var(--seg-${index % 8})`;

/**
 * Полоса 100%: сегмент на каждый активный оффер. Недобор до 100 показан штриховкой-пустотой,
 * закреплённые доли — диагональной штриховкой. `rows`: [{key, share, pinned, color, title}].
 */
export function shareBar(rows, { baseline = false, label = "" } = {}) {
  const total = rows.reduce((sum, row) => sum + row.share, 0);
  const scale = Math.max(total, 100);
  const bar = h("div", { class: ["sharebar", baseline ? "sharebar--baseline" : ""], role: "img",
    "aria-label": `${label || "Распределение долей"}: ${rows.map((r) => `${r.title} ${r.share}%`).join(", ") || "пусто"}` });
  for (const row of rows) {
    if (row.share <= 0) continue;
    bar.append(h("div", { class: ["sharebar__seg", row.pinned ? "sharebar__seg--pinned" : ""],
      style: `flex-basis:${(row.share / scale) * 100}%;background:${row.color}`,
      title: `${row.title} — ${row.share}%${row.pinned ? " (закреплено)" : ""}` }));
  }
  if (total < 100) {
    bar.append(h("div", { class: "sharebar__seg sharebar__seg--void",
      style: `flex-basis:${((100 - total) / scale) * 100}%`, title: `Не распределено ${100 - total}%` }));
  }
  return bar;
}

// ---------------------------------------------------------------- спарклайн

export function sparkline(values) {
  const width = 84;
  const height = 24;
  const max = Math.max(...values, 1);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values.map((value, index) =>
    `${(index * step).toFixed(1)},${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`).join(" ");
  return h("svg", { class: "spark", viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `Клики по дням: ${values.join(", ")}` },
  h("polyline", { points, fill: "none", stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linejoin": "round", "stroke-linecap": "round" }));
}

export function copyButton(text, label = "Копировать") {
  return h("button", { class: "btn btn--sm btn--quiet", type: "button", title: label,
    onclick: async () => {
      try { await navigator.clipboard.writeText(text); toast("Скопировано"); }
      catch { toast("Браузер не дал доступ к буферу обмена — скопируйте вручную.", "amber"); }
    } }, icon("copy"));
}
