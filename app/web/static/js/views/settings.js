// Настройки: значения по умолчанию «создаватора», имя для журнала, тема, диагностика связи.

import { api, session } from "../api.js";
import { h, mount } from "../dom.js";
import { reportError, toast, withBusy } from "../ui.js";

const SOURCE_LABEL = { "настройки": "задано здесь", ".env": "из файла .env", "авто": "определено автоматически",
  "не задано": "не задано" };

export async function renderSettings(container, { refreshHealth }) {
  let lookups;
  let health;
  try {
    [lookups, health] = await Promise.all([
      api("GET", "/api/meta/lookups?refresh=true").catch((error) => ({ error })),
      api("GET", "/api/health"),
    ]);
  } catch (error) {
    mount(container, h("div", { class: "page-head" }, h("h1", {}, "Настройки")),
      h("div", { class: "banner banner--error" }, error.message));
    return;
  }

  const values = {};
  const defaults = lookups.error ? {} : lookups.defaults;

  const pick = (label, key, rows) => {
    const current = defaults[`${key}_source`] === "настройки" ? defaults[key] : "";
    values[key] = current || null;
    return h("label", { class: "field" }, h("span", { class: "field__label" }, label),
      h("select", { class: "select", onchange: (event) => { values[key] = event.target.value ? Number(event.target.value) : null; } },
        h("option", { value: "" }, "Автоматически"),
        rows.map((row) => h("option", { value: row.id, selected: row.id === current }, `${row.name} (#${row.id})`))),
      h("span", { class: "field__hint" }, `Сейчас: ${defaults[key] ? `#${defaults[key]}` : "—"} · ${SOURCE_LABEL[defaults[`${key}_source`]] || ""}`));
  };
  const text = (label, key, placeholder, hint) => {
    const own = defaults[`${key}_source`] === "настройки" ? defaults[key] : "";
    values[key] = own || null;
    return h("label", { class: "field" }, h("span", { class: "field__label" }, label),
      h("input", { class: "input", type: "url", value: own || "", placeholder,
        oninput: (event) => { values[key] = event.target.value.trim() || null; } }),
      h("span", { class: "field__hint" }, hint));
  };

  const domainField = lookups.error ? null : (lookups.domains.length
    ? pick("Домен по умолчанию", "default_domain_id", lookups.domains)
    : h("label", { class: "field" }, h("span", { class: "field__label" }, "Домен по умолчанию (ID)"),
      h("input", { class: "input mono", type: "number", min: "1",
        value: defaults.default_domain_id_source === "настройки" ? defaults.default_domain_id : "",
        placeholder: lookups.inferred_domain_id ? `авто: ${lookups.inferred_domain_id}` : "ID домена в Keitaro",
        oninput: (event) => { values.default_domain_id = event.target.value ? Number(event.target.value) : null; } }),
      h("span", { class: "field__hint" }, "Ключу API не виден список доменов, поэтому вводится ID. "
        + `Сейчас: ${defaults.default_domain_id ? `#${defaults.default_domain_id}` : "—"} · ${SOURCE_LABEL[defaults.default_domain_id_source] || ""}`)));
  if (!lookups.error && !lookups.domains.length) {
    values.default_domain_id = defaults.default_domain_id_source === "настройки" ? defaults.default_domain_id : null;
  }

  const lamp = health.keitaro.reachable ? "green" : health.keitaro.configured ? "red" : "amber";
  const theme = document.documentElement.dataset.theme || "auto";

  mount(container,
    h("div", { class: "page-head" }, h("div", { class: "page-head__title" }, h("h1", {}, "Настройки"))),
    h("div", { class: "create-grid" },
      h("div", { class: "stack" },
        h("form", { class: "card", onsubmit: async (event) => {
          event.preventDefault();
          try {
            await withBusy(event.submitter, () => api("PUT", "/api/settings", values));
            toast("Значения по умолчанию сохранены.");
          } catch (error) { reportError(error); }
        } }, h("div", { class: "card__body stack" },
          h("h2", {}, "Что «создаватор» подставляет сам"),
          lookups.error ? h("div", { class: "banner banner--error" }, `Справочники Keitaro недоступны: ${lookups.error.message}`) : [
            domainField,
            pick("Группа по умолчанию", "default_group_id", lookups.groups),
            pick("Источник по умолчанию", "default_traffic_source_id", lookups.traffic_sources),
            text("Редирект для Flow 1", "default_redirect_url", "https://google.com", "Куда уходит трафик из выбранной страны."),
            text("Адрес трекинг-домена", "tracking_domain_url", "https://tracking.example.com",
              "Нужен только чтобы показывать готовую ссылку кампании. На работу потоков не влияет."),
            h("div", {}, h("button", { class: "btn btn--primary", type: "submit" }, "Сохранить"))]))),
      h("div", { class: "stack" },
        h("div", { class: "card" }, h("div", { class: "card__body stack" },
          h("h2", {}, "Связь с Keitaro"),
          h("div", { class: "row" }, h("span", { class: `lamp lamp--${lamp}`, "aria-hidden": "true" }),
            h("span", {}, health.keitaro.reachable ? "Трекер отвечает, ключ API принят."
              : health.keitaro.message || "Связь не проверена.")),
          h("dl", { class: "kv" },
            h("dt", {}, "Админка"), h("dd", {}, health.admin_url ? h("a", { href: health.admin_url, target: "_blank", rel: "noopener noreferrer" }, health.admin_url) : "—"),
            h("dt", {}, "База AdRobot"), h("dd", {}, health.database),
            h("dt", {}, "Версия"), h("dd", { class: "mono" }, health.version)),
          h("p", { class: "small muted" }, "Адрес трекера и ключ API задаются только в файле .env на сервере: в базу и в браузер ключ не попадает."),
          h("div", {}, h("button", { class: "btn", onclick: async (event) => {
            await withBusy(event.currentTarget, refreshHealth);
            renderSettings(container, { refreshHealth });
          } }, "Проверить ещё раз")))),
        h("div", { class: "card" }, h("div", { class: "card__body stack" },
          h("h2", {}, "Этот браузер"),
          h("label", { class: "field" }, h("span", { class: "field__label" }, "Ваше имя для журнала операций"),
            h("input", { class: "input", type: "text", maxlength: "40", value: session.user, placeholder: "например, Андрей",
              oninput: (event) => { session.user = event.target.value.trim(); } }),
            h("span", { class: "field__hint" }, "Keitaro в своём журнале видит только API-ключ. Имя помогает понять, кто нажал кнопку.")),
          h("label", { class: "field" }, h("span", { class: "field__label" }, "Тема"),
            h("select", { class: "select", onchange: (event) => {
              const value = event.target.value;
              if (value === "auto") { delete document.documentElement.dataset.theme; localStorage.removeItem("adrobot.theme"); }
              else { document.documentElement.dataset.theme = value; localStorage.setItem("adrobot.theme", value); }
            } }, [["auto", "Как в системе"], ["light", "Светлая"], ["dark", "Тёмная"]].map(([value, label]) =>
              h("option", { value, selected: value === theme }, label)))))),
        h("div", { class: "card" }, h("div", { class: "card__body stack" },
          h("h2", {}, "Для разработчика"),
          h("p", { class: "small" }, "Все кнопки интерфейса — это методы REST API. Их можно вызвать и проверить вручную: ",
            h("a", { href: "docs", target: "_blank", rel: "noopener" }, "Swagger UI (/docs)"), "."))))));
}
