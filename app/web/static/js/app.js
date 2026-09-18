// Точка входа интерфейса: маршруты по hash, индикатор связи с Keitaro, запрос токена.

import { api, session } from "./api.js";
import { h, mount } from "./dom.js";
import { dialog } from "./ui.js";
import { renderCampaigns } from "./views/campaigns.js";
import { renderCreate } from "./views/create.js";
import { renderEditor } from "./views/editor.js";
import { renderLog } from "./views/log.js";
import { renderSettings } from "./views/settings.js";

const app = document.getElementById("app");
const savedTheme = localStorage.getItem("adrobot.theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

let health = null;
let renderNo = 0;

async function refreshHealth() {
  const link = document.getElementById("health");
  const [lamp, text] = [link.querySelector(".lamp"), link.querySelector(".lamp-link__text")];
  try {
    health = await api("GET", "/api/health");
  } catch (error) {
    health = null;
    lamp.className = "lamp lamp--red";
    text.textContent = "AdRobot: нет связи";
    link.title = error.message;
    return;
  }
  const ok = health.keitaro.reachable === true;
  lamp.className = `lamp lamp--${ok ? "green" : health.keitaro.configured ? "red" : "amber"}`;
  text.textContent = ok ? "Keitaro: на связи" : health.keitaro.configured ? "Keitaro: нет связи" : "Keitaro: не настроен";
  link.title = ok ? `Трекер отвечает: ${health.admin_url}` : health.keitaro.message;
}

function setupScreen() {
  mount(app, h("div", { class: "page-head" }, h("h1", {}, "Осталось подключить Keitaro")),
    h("div", { class: "card" }, h("div", { class: "card__body stack" },
      h("p", {}, "AdRobot запущен, но не знает адрес трекера и ключ API. Они задаются в файле ", h("code", {}, ".env"), " рядом с проектом:"),
      h("pre", { class: "code" }, "KEITARO_BASE_URL=https://ваш-трекер.example\nKEITARO_API_KEY=ключ из админки: Профиль → API ключи"),
      h("p", {}, "После правки перезапустите сервер и обновите страницу. Шаблон со всеми параметрами — в файле ", h("code", {}, ".env.example"), "."),
      h("p", { class: "small muted" }, "Ключ хранится только на сервере: в базу данных, в логи и в браузер он не попадает."))));
}

const ROUTES = [
  [/^#\/campaigns\/(\d+)$/, "campaigns", (match) => renderEditor(app, Number(match[1]))],
  [/^#\/campaigns$/, "campaigns", () => renderCampaigns(app)],
  [/^#\/new$/, "new", () => renderCreate(app)],
  [/^#\/log$/, "log", () => renderLog(app)],
  [/^#\/settings$/, "settings", () => renderSettings(app, { refreshHealth })],
];

async function route() {
  const mine = ++renderNo;
  const hash = location.hash || "#/campaigns";
  const found = ROUTES.map(([pattern, name, render]) => [hash.match(pattern), name, render]).find(([match]) => match);
  if (!found) { location.hash = "#/campaigns"; return; }
  const [match, name, render] = found;
  for (const link of document.querySelectorAll(".nav__link")) {
    if (link.dataset.route === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (health && health.database !== "ok") {
    mount(app, h("div", { class: "page-head" }, h("h1", {}, "База данных не готова")),
      h("div", { class: "banner banner--error" }, `Состояние базы: ${health.database}.`),
      h("p", {}, "Выполните в каталоге проекта ", h("code", {}, "alembic upgrade head"), " и обновите страницу."));
    return;
  }
  if (health && !health.keitaro.configured && name !== "settings") { setupScreen(); return; }
  mount(app, h("div", { class: "skeleton" }));
  try {
    await render(match);
  } catch (error) {
    if (mine === renderNo) mount(app, h("div", { class: "banner banner--error" }, error.message || String(error)));
  }
  if (mine === renderNo) app.focus({ preventScroll: true });
}

let askingToken = false;
window.addEventListener("adrobot:auth-required", async () => {
  if (askingToken) return;
  askingToken = true;
  const input = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: "токен из ADROBOT_AUTH_TOKEN" });
  const ok = await dialog({ title: "Нужен токен доступа",
    body: [h("p", {}, "На сервере включена защита: введите токен, заданный в переменной ADROBOT_AUTH_TOKEN."), input],
    actions: [{ label: "Отмена", value: false }, { label: "Войти", kind: "primary", value: true }] });
  askingToken = false;
  if (ok && input.value.trim()) { session.token = input.value.trim(); await refreshHealth(); route(); }
});

window.addEventListener("hashchange", route);
await refreshHealth();
await route();
setInterval(refreshHealth, 120000);
