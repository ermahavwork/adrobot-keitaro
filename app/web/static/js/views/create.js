// «Создаватор»: Name + Geo + Offer → кампания Keitaro с двумя потоками.
// Справа — живая схема маршрута: что именно трекер сделает с переходом по ссылке кампании.

import { api, newIdempotencyKey } from "../api.js";
import { h, icon, mount, debounce } from "../dom.js";
import { autocomplete, copyButton, reportError, toast, withBusy } from "../ui.js";

export async function renderCreate(container) {
  const form = { name: "", geoRaw: "", geo: [], geoUnknown: [], offers: [], domainId: "", groupId: "",
    sourceId: "", redirectUrl: "", alias: "", splitByGeo: false, allowDuplicateName: false };
  let lookups = null;
  let idempotencyKey = newIdempotencyKey(); // один ключ на одну попытку создания

  const routeHost = h("div", { class: "card" });
  const resultHost = h("div", { class: "stack" });
  const geoChips = h("div", { class: "chips", "aria-live": "polite" });
  const offerChips = h("div", { class: "chips" });
  const formError = h("div", { class: "field__error", role: "alert" });

  // ---------------------------------------------------------------- схема маршрута

  function paintRoute() {
    const geoText = form.geo.length ? form.geo.map((g) => `${g.name} (${g.code})`).join(", ") : "страна не выбрана";
    const redirect = form.redirectUrl || lookups?.defaults.default_redirect_url || "https://google.com";
    const each = form.offers.length ? Math.floor(100 / form.offers.length) : 0;
    const extra = form.offers.length ? 100 - each * form.offers.length : 0;
    const split = form.offers.map((offer, index) =>
      ({ offer, share: each + (index >= form.offers.length - extra ? 1 : 0) }));
    const many = form.splitByGeo && form.geo.length > 1;
    mount(routeHost, h("div", { class: "route" },
      h("div", { class: "route__node" }, h("span", { class: "route__dot" }, "→"),
        h("div", {}, h("div", { class: "route__title" }, "Переход по ссылке кампании"),
          h("div", { class: "route__text" }, many
            ? `Будет создано кампаний: ${form.geo.length} — по одной на страну.`
            : `Кампания «${form.name || "без названия"}»: домен, группа и источник — из настроек ниже.`))),
      h("div", { class: "route__node route__node--geo" }, h("span", { class: "route__dot" }, "1"),
        h("div", {}, h("div", { class: "route__title" }, "Flow 1 · посетитель из выбранной страны?"),
          h("div", { class: "route__text" }, `${many ? "своя страна в каждой кампании" : geoText} → редирект на `, h("code", {}, redirect)))),
      h("div", { class: "route__node route__node--offer" }, h("span", { class: "route__dot" }, "2"),
        h("div", {}, h("div", { class: "route__title" }, "Flow 2 · все остальные"),
          split.length
            ? h("div", { class: "route__split" }, split.map(({ offer, share }) =>
              h("div", { class: "route__split-row" }, h("span", { class: "mono" }, `${share}%`),
                h("span", {}, h("span", { class: "mono muted" }, `[${offer.id}] `), offer.name))))
            : h("div", { class: "route__text muted" }, "выберите оффер — сюда пойдёт остальной трафик")))));
  }

  // ---------------------------------------------------------------- поля

  const parseGeo = debounce(async () => {
    try {
      const parsed = await api("GET", `/api/meta/geo/parse?raw=${encodeURIComponent(form.geoRaw)}`);
      form.geo = parsed.codes;
      form.geoUnknown = parsed.unknown;
    } catch { form.geo = []; form.geoUnknown = []; }
    mount(geoChips,
      form.geo.map((g) => h("span", { class: "chip chip--geo", title: g.name }, `${g.code} · ${g.name}`)),
      form.geoUnknown.map((token) => h("span", { class: "chip chip--bad", title: "Не похоже на код страны ISO" }, `${token} ?`)));
    paintRoute();
  }, 250);

  function paintOffers() {
    mount(offerChips, form.offers.map((offer) => h("span", { class: "chip chip--plain" },
      h("span", { class: "mono" }, `[${offer.id}]`), offer.name,
      h("button", { class: "chip__x", type: "button", "aria-label": `Убрать оффер ${offer.name}`,
        onclick: () => { form.offers = form.offers.filter((o) => o.id !== offer.id); paintOffers(); } }, "×"))));
    paintRoute();
  }

  const offerPicker = autocomplete({
    placeholder: "поиск по офферам: ID или название",
    ariaLabel: "Поиск оффера",
    search: async (query) => (await api("GET", `/api/offers?q=${encodeURIComponent(query)}&limit=30`))
      .filter((o) => !form.offers.some((chosen) => chosen.id === o.id)),
    onPick: (offer) => { form.offers.push(offer); paintOffers(); },
  });

  const select = (label, key, rows, autoId, note) => h("label", { class: "field" },
    h("span", { class: "field__label" }, label),
    h("select", { class: "select", onchange: (event) => { form[key] = event.target.value; } },
      h("option", { value: "" }, autoId ? `По умолчанию — #${autoId}` : "Не задано"),
      rows.map((row) => h("option", { value: row.id }, `${row.name} (#${row.id})`))),
    note ? h("span", { class: "field__hint" }, note) : null);

  // ---------------------------------------------------------------- отправка

  function payload(dryRun) {
    const body = { name: form.name.trim(), geo: form.geoRaw, offer_ids: form.offers.map((o) => o.id),
      split_by_geo: form.splitByGeo, dry_run: dryRun, allow_duplicate_name: form.allowDuplicateName };
    if (form.domainId) body.domain_id = Number(form.domainId);
    if (form.groupId) body.group_id = Number(form.groupId);
    if (form.sourceId) body.traffic_source_id = Number(form.sourceId);
    if (form.redirectUrl.trim()) body.redirect_url = form.redirectUrl.trim();
    if (form.alias.trim()) body.alias = form.alias.trim();
    return body;
  }

  function localProblems() {
    const problems = [];
    if (!form.name.trim()) problems.push("Введите название кампании.");
    if (!form.geoRaw.trim()) problems.push("Укажите страну: код ISO вроде AU или название.");
    if (form.geoUnknown.length) problems.push(`Не распознано: ${form.geoUnknown.join(", ")}.`);
    if (!form.offers.length) problems.push("Выберите хотя бы один оффер из списка.");
    return problems;
  }

  async function submit(button, dryRun) {
    const problems = localProblems();
    mount(formError, problems.join(" "));
    if (problems.length) return;
    try {
      await withBusy(button, async () => {
        const data = await api("POST", "/api/campaigns", payload(dryRun),
          { headers: dryRun ? {} : { "Idempotency-Key": idempotencyKey } });
        paintResults(data);
        if (!dryRun && data.results.some((r) => r.status === "created")) {
          idempotencyKey = newIdempotencyKey();
          toast(data.replayed ? "Этот запрос уже выполнялся — показан прежний результат, дубль не создан."
            : "Кампания создана в Keitaro.");
        }
      });
    } catch (error) {
      if (error.code === "duplicate_name") {
        form.allowDuplicateName = true;
        mount(formError, `${error.message} Нажмите Create ещё раз, чтобы всё равно создать.`);
      } else if (error.details && error.details.errors) {
        mount(formError, error.details.errors.join(" "));
      } else {
        reportError(error);
      }
    }
  }

  function paintResults(data) {
    mount(resultHost, data.results.map((result) => {
      if (result.status === "created") {
        return h("div", { class: "card result" }, h("div", { class: "card__body stack" },
          h("h2", {}, `Создана: ${result.name}`),
          h("dl", { class: "kv" },
            h("dt", {}, "KT ID"), h("dd", { class: "mono" }, result.keitaro_campaign_id),
            h("dt", {}, "Alias"), h("dd", { class: "mono" }, result.alias),
            h("dt", {}, "Гео"), h("dd", { class: "mono" }, result.geo.join(", ")),
            h("dt", {}, "Потоки"), h("dd", { class: "mono" }, result.stream_ids.join(", ")),
            result.campaign_url ? [h("dt", {}, "Ссылка"), h("dd", {}, h("span", { class: "mono" }, result.campaign_url), copyButton(result.campaign_url))] : null),
          (result.warnings || []).map((w) => h("p", { class: "small muted" }, w)),
          h("div", { class: "row" },
            h("a", { class: "btn btn--primary", href: `#/campaigns/${result.campaign_id}` }, "Открыть в редакторе"),
            result.admin_url ? h("a", { class: "btn", href: result.admin_url, target: "_blank", rel: "noopener noreferrer" }, icon("external"), "View in KT") : null)));
      }
      if (result.status === "planned") {
        return h("div", { class: "card result result--plan" }, h("div", { class: "card__body stack" },
          h("h2", {}, `План: ${result.plan.name}`),
          h("p", { class: "small muted" }, "Проверка пройдена. Ниже — ровно те запросы, которые уйдут в Keitaro при нажатии Create. Сейчас ничего не создано."),
          h("pre", { class: "code" }, JSON.stringify(result.plan.requests, null, 2))));
      }
      return h("div", { class: "card result result--error" }, h("div", { class: "card__body stack" },
        h("h2", {}, `Не создана: ${result.name}`), h("p", {}, result.error.message)));
    }));
    resultHost.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---------------------------------------------------------------- сборка экрана

  try {
    lookups = await api("GET", "/api/meta/lookups");
  } catch (error) {
    mount(container, h("div", { class: "page-head" }, h("h1", {}, "Новая кампания")),
      h("div", { class: "banner banner--error" }, error.message));
    return;
  }
  const d = lookups.defaults;
  const domainNote = lookups.domains_visible ? null
    : "Справочник доменов скрыт от ключа API; домен определён по существующим кампаниям.";

  const createButton = h("button", { class: "btn btn--primary", type: "submit" }, "Create");
  mount(container,
    h("div", { class: "page-head" }, h("div", { class: "page-head__title" }, h("h1", {}, "Новая кампания"),
      h("div", { class: "meta-line" }, "Два потока: выбранная страна → редирект, остальные → оффер. Домен, группа и источник проставляются сами."))),
    lookups.warnings.map((w) => h("div", { class: "banner banner--warn small" }, w)),
    h("div", { class: "create-grid" },
      h("form", { class: "card", novalidate: true, onsubmit: (event) => { event.preventDefault(); submit(createButton, false); } },
        h("div", { class: "card__body stack" },
          h("label", { class: "field" }, h("span", { class: "field__label" }, "Name"),
            h("input", { class: "input", type: "text", maxlength: "200", placeholder: "имя кампании", autofocus: true,
              oninput: (event) => { form.name = event.target.value; form.allowDuplicateName = false; paintRoute(); } })),
          h("label", { class: "field" }, h("span", { class: "field__label" }, "Geo"),
            h("input", { class: "input mono", type: "text", placeholder: "MX, AU, RO …", autocapitalize: "characters",
              oninput: (event) => { form.geoRaw = event.target.value; parseGeo(); } }),
            h("span", { class: "field__hint" }, "Коды ISO через запятую; понимает и названия: «Австралия», «UK»."),
            geoChips),
          h("div", { class: "field" }, h("span", { class: "field__label" }, "Offer"), offerPicker.root,
            h("span", { class: "field__hint" }, "Можно выбрать несколько — доли поделятся поровну."), offerChips),
          h("details", { class: "advanced" }, h("summary", {}, "Домен, группа, источник и другое"),
            h("div", { class: "stack" },
              select("Домен", "domainId", lookups.domains, d.default_domain_id, domainNote),
              select("Группа", "groupId", lookups.groups, d.default_group_id),
              select("Источник", "sourceId", lookups.traffic_sources, d.default_traffic_source_id,
                "Параметры источника (utm, sub_id) копируются в кампанию."),
              h("label", { class: "field" }, h("span", { class: "field__label" }, "Куда редиректить страну из Flow 1"),
                h("input", { class: "input", type: "url", placeholder: d.default_redirect_url,
                  oninput: (event) => { form.redirectUrl = event.target.value; paintRoute(); } })),
              h("label", { class: "field" }, h("span", { class: "field__label" }, "Alias (необязательно)"),
                h("input", { class: "input mono", type: "text", maxlength: "64", placeholder: "сгенерируется сам, например wVqN1RkT",
                  oninput: (event) => { form.alias = event.target.value; } })),
              h("label", { class: "check" }, h("input", { type: "checkbox",
                onchange: (event) => { form.splitByGeo = event.target.checked; paintRoute(); } }),
              h("span", {}, "Отдельная кампания на каждую страну", h("span", { class: "field__hint" }, " — к названию добавится [код]; можно вставить {geo} в имя."))))),
          formError,
          h("div", { class: "row" }, createButton,
            h("button", { class: "btn", type: "button", title: "Проверить данные и показать запросы, ничего не создавая",
              onclick: (event) => submit(event.currentTarget, true) }, "Проверить без создания")))),
      h("div", { class: "stack" }, routeHost, resultHost)));
  paintRoute();
}
