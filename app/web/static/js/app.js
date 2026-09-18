// Точка входа интерфейса: маршруты по hash, индикатор связи с Keitaro, запрос токена.

import { api, session } from "./api.js";
import { h, mount } from "./dom.js";
import { dialog } from "./ui.js";
import { renderCampaigns } from "./views/campaigns.js";
import { renderCreate } from "./views/create.js";
import { renderEditor } from "./views/editor.js";
import { renderLog } from "./views/log.js";
import { renderOffers } from "./views/offers.js";
import { renderSettings } from "./views/settings.js";

const app = document.getElementById("app");
const savedTheme = localStorage.getItem("adrobot.theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

let health = null;
let renderNo = 0;

async function refreshHealth(deep = true) {
  const link = document.getElementById("health");
  const [lamp, text] = [link.querySelector(".lamp"), link.querySelector(".lamp-link__text")];
  try {
    const fresh = await api("GET", `/api/health?deep=${deep}`);
    // Быстрая проверка связь с трекером не меряет — прежний ответ про неё не затираем.
    if (!deep && health && fresh.keitaro.reachable === null) fresh.keitaro = health.keitaro;
    health = fresh;
  } catch (error) {
    health = null;
    lamp.className = "lamp lamp--red";
    text.textContent = "AdRobot: нет связи";
    link.title = error.message;
    return;
  }
  const ok = health.keitaro.reachable === true;
  if (health.keitaro.configured && health.keitaro.reachable === null) {
    lamp.className = "lamp lamp--off";
    text.textContent = "Keitaro: проверка…";
    return;
  }
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
  [/^#\/campaigns\/(\d+)$/, "campaigns", (match, host) => renderEditor(host, Number(match[1]))],
  [/^#\/campaigns$/, "campaigns", (_match, host) => renderCampaigns(host)],
  [/^#\/new$/, "new", (_match, host) => renderCreate(host)],
  [/^#\/offers$/, "offers", (_match, host) => renderOffers(host)],
  [/^#\/log$/, "log", (_match, host) => renderLog(host)],
  [/^#\/settings$/, "settings", (_match, host) => renderSettings(host, { refreshHealth })],
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
  // У каждого перехода свой контейнер. Экран, с которого уже ушли, может дорисовывать что угодно
  // (ответ статистики, авто-Fetch) — он пишет в отсоединённый узел и новый экран не затирает.
  const host = h("div", {}, h("div", { class: "skeleton" }));
  mount(app, host);
  try {
    await render(match, host);
  } catch (error) {
    if (mine === renderNo) mount(host, h("div", { class: "banner banner--error" }, error.message || String(error)));
  }
  if (mine === renderNo) app.focus({ preventScroll: true });
}

let askingToken = false;
window.addEventListener("adrobot:auth-required", async () => {
  if (askingToken) return;
  askingToken = true;
  const input = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: "токен доступа" });
  const ok = await dialog({ title: "Нужен токен доступа",
    body: [h("p", {}, "На сервере включена защита. Введите свой токен: общий (ADROBOT_AUTH_TOKEN) "
      + "или именной (ADROBOT_AUTH_TOKENS). Именной токен с пометкой «только чтение» не даст ничего менять."), input],
    actions: [{ label: "Отмена", value: false }, { label: "Войти", kind: "primary", value: true }] });
  askingToken = false;
  if (ok && input.value.trim()) {
    session.token = input.value.trim();
    await refreshHealth(false);
    try { session.readOnly = (await api("GET", "/api/auth/me")).read_only === true; } catch { /* токен не подошёл */ }
    route();
    refreshHealth(true);
  }
});

window.addEventListener("hashchange", route);
// Сначала быстрая проверка (база, настройки) и сразу экран. Связь с трекером меряем фоном:
// при лежащем Keitaro она занимает десятки секунд, и держать всё это время пустую страницу нельзя.
await refreshHealth(false);
try { session.readOnly = (await api("GET", "/api/auth/me")).read_only === true; } catch { /* спросит токен */ }
await route();
refreshHealth(true);
setInterval(() => refreshHealth(true), 120000);
