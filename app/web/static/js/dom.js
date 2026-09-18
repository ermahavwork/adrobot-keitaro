// Крошечный помощник для сборки DOM без innerHTML.
// Весь текст попадает на страницу через textContent, поэтому название оффера вида
// `<img onerror=...>` из Keitaro останется текстом — XSS исключён самой конструкцией.

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set(["svg", "path", "circle", "polyline", "rect", "line", "g"]);

/**
 * h("button", {class: "btn", onclick: fn, disabled: true}, "Текст", h("span", ...))
 * Ключи on* — обработчики; dataset — объект; логические атрибуты понимает как есть;
 * null/undefined/false среди детей пропускаются — удобно для условной вёрстки.
 */
export function h(tag, attrs = {}, ...children) {
  const node = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else if (key === "style") {
      // Через CSSOM, а не атрибутом: атрибут style запрещён нашей Content-Security-Policy.
      node.style.cssText = String(value);
    } else if (key === "class") {
      node.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
    } else if (key === "value" && !SVG_TAGS.has(tag)) {
      node.value = value;
    } else if (value === true) {
      node.setAttribute(key, "");
    } else {
      node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
  return container;
}

/** Иконки — встроенные контуры, без иконочных шрифтов и внешних запросов. */
const ICONS = {
  download: "M12 4v11m0 0-4-4m4 4 4-4M5 19h14",
  upload: "M12 19V8m0 0-4 4m4-4 4 4M5 5h14",
  external: "M14 5h5v5m0-5-8 8M10 6H6a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1v-4",
  trash: "M5 7h14M10 7V5h4v2m-7 0 1 12h8l1-12",
  undo: "M9 7 5 11l4 4M5 11h9a5 5 0 0 1 0 10h-3",
  close: "M6 6l12 12M18 6 6 18",
  plus: "M12 5v14M5 12h14",
  history: "M12 8v4l3 2M4 12a8 8 0 1 0 3-6.2M4 5v4h4",
  refresh: "M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5",
  copy: "M9 9h10v11H9zM5 15V4h10",
  pin: "M9 4h6l-1 6 3 3H7l3-3-1-6zM12 13v7",
};

export function icon(name) {
  return h("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
  h("path", { d: ICONS[name] || "" }));
}

export const fmtDate = (iso) => {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

export function debounce(fn, ms) {
  let timer = 0;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
