// Редактор кампании: потоки, офферы, доли. Повторяет рабочий цикл оригинального AdRobot:
// FETCH STREAMS FROM KT → ADD / REMOVE / BRING BACK / pin → поток «жёлтый» → PUSH TO KT или CANCEL.
// Каждая кнопка — один вызов API; сервер возвращает свежий вид потока, мы его перерисовываем.

import { api, session } from "../api.js";
import { h, icon, mount, fmtDate } from "../dom.js";
import { autocomplete, confirmDialog, dialog, reportError, segmentColor, shareBar, sparkline,
  toast, withBusy } from "../ui.js";
import { openAdvisor } from "./advisor.js";

const STALE_AFTER_MS = 2 * 60 * 1000; // открыли кампанию позже — тихо перечитываем её из Keitaro

const PERIODS = [["today", "сегодня"], ["7d", "7 дней"], ["30d", "30 дней"]];
const DIFF_TEXT = {
  add: (c, name) => ["+", `${name} — появится в Keitaro с долей ${c.to}%`],
  remove: (c, name) => ["−", `${name} — исчезнет из Keitaro (сейчас ${c.from}%)`],
  share: (c, name) => ["~", `${name}: ${c.from}% → ${c.to}%`],
  state: (c, name) => ["~", `${name}: изменится состояние, доля ${c.to}%`],
};

export async function renderEditor(container, campaignId) {
  const state = { campaign: null, stats: null, period: "7d", showArchive: true };
  mount(container, h("div", { class: "skeleton" }));

  const colorOf = new Map(); // offer_id -> цвет сегмента; стабилен, пока открыт экран
  const colorFor = (offerId) => {
    if (!colorOf.has(offerId)) colorOf.set(offerId, segmentColor(colorOf.size));
    return colorOf.get(offerId);
  };

  async function load({ autoFetch = false } = {}) {
    state.campaign = await api("GET", `/api/campaigns/${campaignId}`);
    const fetchedAt = state.campaign.streams_fetched_at ? Date.parse(state.campaign.streams_fetched_at) : 0;
    const stale = Date.now() - fetchedAt > STALE_AFTER_MS;
    // Фоновой синхронизации нет, поэтому при открытии подтягиваем свежее состояние сами.
    // Черновик при этом не теряется: Fetch по умолчанию его сохраняет.
    // С токеном «только чтение» Fetch запрещён (он пишет в базу AdRobot) — показываем что есть.
    if (autoFetch && stale && !state.campaign.is_deleted && !session.readOnly) {
      paint();
      await fetchStreams(null, { silent: true });
    }
    paint();
    loadStats();
  }

  async function loadStats() {
    try {
      state.stats = await api("GET", `/api/campaigns/${campaignId}/stats?period=${state.period}`);
    } catch {
      state.stats = { available: false };
    }
    paint();
  }

  async function fetchStreams(button, { silent = false, discardDraft = false } = {}) {
    const run = async () => {
      // auto: фоновое обновление при открытии — в журнал оно попадёт, только если что-то изменилось
      const data = await api("POST", `/api/campaigns/${campaignId}/fetch`,
        { discard_draft: discardDraft, auto: silent });
      state.campaign = data.campaign;
      if (!silent) {
        const archived = data.result.archived_offers.length;
        toast(`Потоки получены из Keitaro: ${data.result.streams}.`
          + (archived ? ` Офферов, которых там больше нет, — ${archived}: они в архиве, их можно вернуть.` : ""));
      }
      paint();
    };
    try { button ? await withBusy(button, run) : await run(); } catch (error) { reportError(error); }
  }

  async function onFetchClick(event) {
    const button = event.currentTarget;
    if (!state.campaign.is_dirty) return fetchStreams(button);
    const choice = await dialog({
      title: "В кампании есть неопубликованные изменения",
      body: h("p", {}, "Fetch подтянет из Keitaro актуальное состояние. Что сделать с черновиком?"),
      actions: [
        { label: "Отмена", value: null },
        { label: "Сбросить черновик", kind: "danger", value: "discard" },
        { label: "Сохранить черновик", kind: "primary", value: "keep" },
      ],
    });
    if (choice) await fetchStreams(button, { discardDraft: choice === "discard" });
    return undefined;
  }

  /** Общая обёртка правок потока: вызвать API, подменить поток в состоянии, перерисовать. */
  // --- фокус клавиатуры. paint() пересобирает экран целиком, и без этого после каждой кнопки
  // фокус падал бы в начало страницы: с клавиатуры редактором было бы не поработать.
  const FOCUSABLE = "button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href]";
  let pendingFocus = null;

  function rememberFocus(element = document.activeElement) {
    if (!element || !container.contains(element)) return null;
    const scopes = [];
    for (let node = element.closest("[data-focus-scope]"); node;
      node = node.parentElement ? node.parentElement.closest("[data-focus-scope]") : null) {
      scopes.push(node.dataset.focusScope);
    }
    const scope = element.closest("[data-focus-scope]");
    return { scopes, className: element.className,
      index: scope ? [...scope.querySelectorAll(FOCUSABLE)].indexOf(element) : -1 };
  }

  function restoreFocus(memo) {
    if (!memo || (document.activeElement && document.activeElement.closest(".modal"))) return;
    for (const [depth, name] of memo.scopes.entries()) {
      const scope = container.querySelector(`[data-focus-scope="${name}"]`);
      if (!scope) continue; // строки больше нет (оффер убран совсем) — ищем в карточке потока
      const items = [...scope.querySelectorAll(FOCUSABLE)];
      const sameSpot = depth === 0 && items[memo.index] && items[memo.index].className === memo.className
        ? items[memo.index] : null;
      const target = sameSpot || (depth === 0 && items.find((item) => item.className === memo.className)) || items[0];
      if (target) { target.focus({ preventScroll: true }); return; }
    }
  }

  async function mutate(button, method, path, body, okMessage) {
    // Запоминаем до запроса: занятая кнопка выключается, и браузер снимает с неё фокус.
    pendingFocus = rememberFocus(button || document.activeElement);
    try {
      await withBusy(button, async () => {
        const stream = await api(method, path, body);
        const index = state.campaign.streams.findIndex((s) => s.id === stream.id);
        state.campaign.streams[index] = stream;
        state.campaign.is_dirty = state.campaign.streams.some((s) => s.is_dirty);
        if (okMessage) toast(okMessage);
        paint();
      });
    } catch (error) {
      return error;
    }
    return null;
  }

  async function push(button, stream, extra = {}) {
    const error = await mutate(button, "POST", `/api/streams/${stream.id}/push`, extra,
      `Поток «${stream.name}» опубликован в Keitaro.`);
    if (!error) return;
    if (error.code === "conflict") {
      const names = new Map(stream.offers.map((o) => [o.offer_id, o.offer_name]));
      const list = (rows) => h("ul", {}, rows.length
        ? rows.map((r) => h("li", {}, `[${r.offer_id}] ${names.get(r.offer_id) || ""} — ${r.share}%`))
        : h("li", {}, "офферов нет"));
      const choice = await dialog({
        title: "Поток изменили прямо в Keitaro",
        wide: true,
        body: [h("p", {}, "С последней синхронизации набор офферов в трекере стал другим. "
          + "Публикация перезапишет эти правки."),
        h("p", { class: "label small" }, "Сейчас в Keitaro"), list(error.details.keitaro),
        h("p", { class: "label small" }, "AdRobot ожидал увидеть"), list(error.details.expected)],
        actions: [
          { label: "Отмена", value: null },
          { label: "Fetch: подтянуть из Keitaro", value: "fetch" },
          { label: "Опубликовать поверх", kind: "danger", value: "force" },
        ],
      });
      if (choice === "fetch") await fetchStreams(null);
      if (choice === "force") await push(button, stream, { ...extra, force: true });
    } else if (error.code === "unknown_offers") {
      const sure = await confirmDialog("Опубликовать оффер, которого нет в справочнике?", error.message,
        "Оффер существует, опубликовать", "danger");
      if (sure) await push(button, stream, { ...extra, allow_unknown_offers: true });
    } else if (error.code === "empty_stream") {
      const sure = await confirmDialog("Опубликовать поток без офферов?",
        "После публикации в потоке не останется ни одного активного оффера — трафику будет некуда идти.",
        "Опубликовать пустой поток", "danger");
      if (sure) await push(button, stream, { ...extra, allow_empty: true });
    } else {
      reportError(error);
    }
  }

  async function openHistory(stream) {
    let snapshots = [];
    try { snapshots = await api("GET", `/api/streams/${stream.id}/snapshots`); }
    catch (error) { reportError(error); return; }
    const body = (close) => (snapshots.length ? snapshots.map((snap) => h("div", { class: "banner" },
      h("div", { class: "row" },
        h("strong", { class: "grow" }, `Перед публикацией ${fmtDate(snap.created_at)}`),
        h("button", { class: "btn btn--sm", onclick: async (event) => {
          const error = await mutate(event.currentTarget, "POST",
            `/api/streams/${stream.id}/snapshots/${snap.id}/restore`, undefined,
            "Снимок загружен в черновик. Проверьте доли и нажмите Push to KT.");
          if (error) reportError(error);
          close(null);
        } }, icon("undo"), "Вернуть в черновик")),
      h("div", { class: "small muted" }, snap.offers.length
        ? snap.offers.map((o) => `[${o.offer_id}] ${o.offer_name} — ${o.share}%`).join(" · ")
        : "поток был пуст")))
      : h("p", { class: "muted" }, "Снимков пока нет: они создаются автоматически перед каждым Push."));
    await dialog({ title: `История потока «${stream.name}»`, wide: true, body,
      actions: [{ label: "Закрыть", value: null }] });
  }

  // ---------------------------------------------------------------- отрисовка

  function offerStats(stream, offer) {
    const stats = state.stats;
    if (!stats || !stats.available) return { text: "—", trend: null };
    const row = stats.offers[`${stream.keitaro_id}:${offer.offer_id}`];
    if (!row) return { text: h("span", { class: "muted" }, "нет кликов"), trend: null };
    return {
      text: [h("b", {}, row.clicks), " кл · ", h("b", {}, row.conversions), " конв · CR ", h("b", {}, `${row.cr}%`)],
      trend: row.trend.some((v) => v > 0) ? sparkline(row.trend) : null,
    };
  }

  function shareCell(stream, offer) {
    if (offer.state !== "active") {
      return h("span", { class: "share-cell" },
        h("button", { class: "share-btn", disabled: true }, `${offer.share}%`));
    }
    const cell = h("span", { class: "share-cell" });
    const startEdit = () => {
      const input = h("input", { class: "share-input", type: "number", min: "1", max: "100", step: "1",
        value: String(offer.share), "aria-label": `Доля оффера ${offer.offer_name}, %` });
      let done = false;
      const finish = async (save) => {
        if (done) return;
        done = true;
        const value = Number.parseInt(input.value, 10);
        if (!save || Number.isNaN(value) || value === offer.share) { paint(); return; }
        const error = await mutate(null, "PUT", `/api/streams/${stream.id}/offers/${offer.id}/share`,
          { share: value }, `Доля ${value}% закреплена, остальные пересчитаны.`);
        if (error) { reportError(error); paint(); }
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") finish(true);
        if (event.key === "Escape") finish(false);
      });
      input.addEventListener("blur", () => finish(true));
      mount(cell, input);
      input.focus();
      input.select();
    };
    mount(cell,
      h("button", { class: "share-btn", title: "Задать долю вручную (она закрепится)", onclick: startEdit },
        `${offer.share}%`),
      offer.is_dirty && offer.in_keitaro && offer.kt_share !== offer.share
        ? h("span", { class: "share-was", title: "Сейчас в Keitaro" }, `${offer.kt_share}%`) : null,
      h("button", { class: "pin", "aria-pressed": String(offer.is_pinned),
        title: offer.is_pinned ? "Доля закреплена: пересчёты её не меняют. Нажмите, чтобы открепить."
          : "Закрепить долю: пересчёты перестанут её менять",
        "aria-label": offer.is_pinned ? "Открепить долю" : "Закрепить долю",
        onclick: async (event) => {
          const error = await mutate(event.currentTarget, "PUT",
            `/api/streams/${stream.id}/offers/${offer.id}/pin`, { pinned: !offer.is_pinned });
          if (error) reportError(error);
        } }, icon("pin")));
    return cell;
  }

  function offerRow(stream, offer) {
    const removed = offer.state === "removed";
    const stats = offerStats(stream, offer);
    const tags = [
      removed ? h("span", { class: "badge" }, "removed") : null,
      offer.state === "disabled" ? h("span", { class: "badge", title: "Выключен в Keitaro; AdRobot передаёт его как есть" }, "выключен в KT") : null,
      offer.change === "add" ? h("span", { class: "badge badge--amber" }, offer.was_published ? "вернётся" : "новый") : null,
      !offer.offer_known ? h("span", { class: "badge badge--red", title: "Оффера нет в справочнике: удалён или не виден ключу API" }, "нет в справочнике") : null,
      offer.offer_known && offer.offer_state !== "active" ? h("span", { class: "badge badge--red" }, `оффер ${offer.offer_state}`) : null,
    ];
    const action = removed
      ? h("span", { class: "row", style: "justify-content:flex-end" },
        !offer.in_keitaro ? h("button", { class: "btn btn--sm btn--quiet", title: "Убрать из архива насовсем",
          onclick: async (event) => {
            const error = await mutate(event.currentTarget, "DELETE",
              `/api/streams/${stream.id}/offers/${offer.id}/forget`);
            if (error) reportError(error);
          } }, icon("close")) : null,
        h("button", { class: "btn btn--sm", onclick: async (event) => {
          const error = await mutate(event.currentTarget, "POST",
            `/api/streams/${stream.id}/offers/${offer.id}/bring-back`);
          if (error) reportError(error);
        } }, icon("undo"), "Bring back"))
      : h("button", { class: "btn btn--sm", onclick: async (event) => {
        const error = await mutate(event.currentTarget, "DELETE", `/api/streams/${stream.id}/offers/${offer.id}`);
        if (error) reportError(error);
      } }, icon("trash"), "Remove");

    return h("tr", { class: [removed ? "offer--removed" : "", offer.change === "add" ? "offer--added" : ""],
      "data-focus-scope": `offer-${stream.id}-${offer.id}` },
      h("td", {},
        h("span", { class: "offer__swatch", style: `background:${colorFor(offer.offer_id)}` }),
        h("span", { class: "offer__id" }, `[${offer.offer_id}]`),
        h("span", { class: "offer__name" }, offer.offer_name),
        h("span", { class: "offer__tags" }, tags)),
      h("td", { class: "t-right nowrap" }, shareCell(stream, offer)),
      h("td", { class: "col-stats" }, h("span", { class: "stat" }, stats.text)),
      h("td", { class: "col-trend" }, stats.trend),
      h("td", { class: "t-right nowrap" }, action));
  }

  function streamCard(stream) {
    const active = stream.offers.filter((o) => o.state === "active");
    const visible = stream.offers.filter((o) => state.showArchive || o.state !== "removed");
    const hiddenCount = stream.offers.length - visible.length;
    const names = new Map(stream.offers.map((o) => [o.offer_id, o.offer_name]));
    const clicks = state.stats?.available ? (state.stats.streams[String(stream.keitaro_id)]?.clicks ?? 0) : null;
    const meta = [`#${stream.position}`, `stream_id ${stream.keitaro_id}`, stream.type !== "regular" ? stream.type : null,
      clicks !== null ? `кликов за период: ${clicks}` : null].filter(Boolean).join(" · ");

    const lamp = stream.is_deleted ? ["red", "нет в Keitaro"]
      : stream.problems.length ? ["red", "нужна правка"]
        : stream.is_dirty ? ["amber", "не опубликован"]
          : stream.supports_offers ? ["green", "совпадает с Keitaro"] : ["off", "без офферов"];

    const head = h("div", { class: "stream__head" },
      h("span", { class: "stream__name" }, `Stream: ${stream.name || "без названия"}`),
      h("span", { class: "stream__meta mono" }, meta),
      h("span", { class: "stream__state" },
        stream.supports_offers && !stream.is_deleted ? h("button", { class: "btn btn--sm btn--quiet",
          title: "Предложить доли по статистике. Ничего не публикует: цифры попадут в черновик только по вашей кнопке",
          onclick: () => openAdvisor(stream, state.period, (fresh) => {
            const index = state.campaign.streams.findIndex((s) => s.id === fresh.id);
            state.campaign.streams[index] = fresh;
            state.campaign.is_dirty = state.campaign.streams.some((s) => s.is_dirty);
            toast("Доли из совета лежат в черновике. Проверьте и нажмите Push to KT.");
            paint();
          }) }, "Советник") : null,
        stream.supports_offers ? h("button", { class: "btn btn--sm btn--quiet", title: "Снимки перед публикациями и откат",
          onclick: () => openHistory(stream) }, icon("history"), "История") : null,
        h("span", { class: `lamp lamp--${lamp[0]}`, "aria-hidden": "true" }), lamp[1]));

    if (!stream.supports_offers || stream.is_deleted) {
      const summary = stream.summary || {};
      const filters = (summary.filters || []).map((f) =>
        `${f.name} ${f.mode === "reject" ? "≠" : "="} ${(Array.isArray(f.payload) ? f.payload.join(", ") : "…")}`).join("; ");
      return h("section", { class: ["card", "stream", stream.is_deleted ? "stream--gone" : "stream--plain"] }, head,
        h("div", { class: "stream__summary" }, stream.is_deleted
          ? "Поток удалён в Keitaro. Он останется здесь для истории до следующей очистки базы."
          : [summary.action_payload ? `→ ${summary.action_payload}` : "Поток без офферов",
            filters ? ` · фильтр: ${filters}` : "",
            h("span", { class: "muted" }, " · содержимое таких потоков AdRobot не редактирует")]));
    }

    const draftRows = active.map((o) => ({ key: o.id, share: o.share, pinned: o.is_pinned,
      color: colorFor(o.offer_id), title: `[${o.offer_id}] ${o.offer_name}` }));
    const liveRows = stream.offers.filter((o) => o.in_keitaro && o.kt_share > 0 && o.state !== "disabled")
      .map((o) => ({ key: o.id, share: o.kt_share, pinned: false, color: colorFor(o.offer_id),
        title: `[${o.offer_id}] ${o.offer_name}` }));

    const draftBar = stream.is_dirty ? h("div", { class: "draftbar" },
      h("div", { class: "draftbar__text" },
        h("strong", {}, "Поток не опубликован. "), "В Keitaro пока прежний набор офферов. После Push изменится:",
        h("ul", { class: "diff" }, stream.diff.map((change) => {
          const [sign, text] = DIFF_TEXT[change.type](change, `[${change.offer_id}] ${names.get(change.offer_id) || ""}`);
          return h("li", {}, h("span", { class: "diff__sign" }, sign), text);
        }))),
      // Пустой поток — не ошибка долей: Push доступен, но сервер попросит отдельное подтверждение.
      h("button", { class: "btn btn--primary", disabled: stream.problems.length > 0 && stream.active_count > 0,
        title: stream.problems.length && stream.active_count ? "Сначала исправьте доли" : "Отправить черновик в Keitaro",
        onclick: (event) => push(event.currentTarget, stream) }, icon("upload"), "Push to KT"),
      h("button", { class: "btn", title: "Выбросить неопубликованные изменения",
        onclick: async (event) => {
          const error = await mutate(event.currentTarget, "POST", `/api/streams/${stream.id}/cancel`, undefined,
            "Черновик сброшен: поток снова совпадает с Keitaro.");
          if (error) reportError(error);
        } }, icon("close"), "Cancel")) : null;

    const problems = stream.problems.length ? h("div", { class: "problems" },
      h("div", { class: "problems__text" }, stream.problems.join(" ")),
      // В пустом потоке делить нечего: остаются Add, Bring back, Cancel — кнопки пересчёта прячем.
      !stream.active_count ? null : h("button", { class: "btn btn--sm", title: "Поделить свободный остаток между незакреплёнными",
        onclick: async (event) => {
          const error = await mutate(event.currentTarget, "POST", `/api/streams/${stream.id}/recalculate`, { drop_pins: false });
          if (error) reportError(error);
        } }, "Пересчитать"),
      !stream.active_count ? null : h("button", { class: "btn btn--sm", title: "Снять все закрепления и поделить 100% поровну",
        onclick: async (event) => {
          const error = await mutate(event.currentTarget, "POST", `/api/streams/${stream.id}/recalculate`, { drop_pins: true });
          if (error) reportError(error);
        } }, "Выровнять поровну")) : null;

    const adder = autocomplete({
      placeholder: "Добавить оффер: ID или часть названия",
      ariaLabel: `Поиск оффера для потока ${stream.name}`,
      search: async (query) => {
        const taken = new Set(stream.offers.filter((o) => o.state !== "removed").map((o) => o.offer_id));
        return (await api("GET", `/api/offers?q=${encodeURIComponent(query)}&limit=30`)).filter((o) => !taken.has(o.id));
      },
      onPick: (offer) => { picked = offer; adder.input.value = offer.label; addButton.disabled = false; addButton.focus(); },
    });
    let picked = null;
    adder.input.addEventListener("input", () => { picked = null; addButton.disabled = true; });
    const addButton = h("button", { class: "btn", disabled: true, onclick: async (event) => {
      if (!picked) return;
      const error = await mutate(event.currentTarget, "POST", `/api/streams/${stream.id}/offers`, { offer_id: picked.id });
      if (error) reportError(error);
    } }, icon("plus"), "Add");

    const total = stream.total_share;
    return h("section", { class: ["card", "stream", stream.problems.length ? "stream--problem" : stream.is_dirty ? "stream--draft" : ""],
      "data-focus-scope": `stream-${stream.id}` },
      head, draftBar, problems,
      h("div", { class: "sharebar-wrap" },
        shareBar(draftRows, { label: stream.is_dirty ? "Черновик" : "Доли офферов" }),
        stream.is_dirty ? shareBar(liveRows, { baseline: true, label: "Сейчас в Keitaro" }) : null,
        h("div", { class: "sharebar__legend" },
          h("span", {}, stream.is_dirty ? "сверху — черновик, тонкая полоса — сейчас в Keitaro" : `активных офферов: ${active.length}`),
          h("span", { class: ["sharebar__total", total === 100 || !active.length ? "" : "sharebar__total--bad"] }, `Σ ${total}%`))),
      h("div", { class: "table-wrap" }, h("table", { class: "offers" },
        h("thead", {}, h("tr", {}, h("th", {}, "Оффер"), h("th", { class: "t-right" }, "Share"),
          h("th", { class: "col-stats" }, "Stats"), h("th", { class: "col-trend" }, "Trends"),
          h("th", { class: "t-right" }, "Actions"))),
        h("tbody", {}, visible.length ? visible.map((offer) => offerRow(stream, offer))
          : h("tr", {}, h("td", { colspan: "5", class: "empty" }, "В потоке нет офферов. Добавьте первый ниже."))))),
      hiddenCount ? h("div", { class: "stream__summary muted" }, `В архиве скрыто офферов: ${hiddenCount}`) : null,
      h("div", { class: "addrow" }, adder.root, addButton,
        h("button", { class: "btn btn--quiet", title: "Перечитать справочник офферов из Keitaro (если нужного оффера нет в поиске)",
          onclick: async (event) => {
            try {
              const data = await withBusy(event.currentTarget, () => api("POST", "/api/offers/refresh"));
              toast(`Справочник офферов обновлён: ${data.offers}.`);
            } catch (error) { reportError(error); }
          } }, icon("refresh"), "Обновить офферы")));
  }

  function paint() {
    const focusMemo = pendingFocus || rememberFocus();
    pendingFocus = null;
    const c = state.campaign;
    const head = h("div", { class: "page-head", "data-focus-scope": "head" },
      h("div", { class: "page-head__title" },
        h("div", { class: "crumbs" }, h("a", { href: "#/campaigns" }, "Кампании"), " / ", c.name, " / Keitaro Streams"),
        h("h1", {}, c.name),
        h("div", { class: "meta-line" },
          h("span", { class: "mono" }, `KT #${c.keitaro_id}`),
          c.alias ? h("span", { class: "mono" }, `alias ${c.alias}`) : null,
          c.geo.length ? h("span", { class: "chips" }, c.geo.map((g) => h("span", { class: "chip chip--geo", title: g.name }, g.code))) : null,
          h("span", {}, c.origin === "adrobot" ? "создана в AdRobot" : "импортирована из Keitaro"),
          h("span", {}, `синхронизация: ${fmtDate(c.streams_fetched_at)}`),
          c.campaign_url ? h("a", { href: c.campaign_url, target: "_blank", rel: "noopener noreferrer" }, c.campaign_url) : null)),
      h("div", { class: "page-head__actions" },
        h("button", { class: "btn btn--primary", onclick: onFetchClick, disabled: c.is_deleted }, icon("download"), "Fetch streams from KT"),
        c.admin_url ? h("a", { class: "btn", href: c.admin_url, target: "_blank", rel: "noopener noreferrer" }, icon("external"), "View in KT") : null,
        h("button", { class: "btn", "aria-pressed": String(state.showArchive),
          title: "Показывать или скрывать офферы из архива (removed)",
          onclick: () => { state.showArchive = !state.showArchive; paint(); } },
        state.showArchive ? "Hide removed" : "Show all offers"),
        h("select", { class: "select", style: "width:auto", "aria-label": "Период статистики",
          onchange: (event) => { state.period = event.target.value; loadStats(); } },
        PERIODS.map(([value, label]) => h("option", { value, selected: value === state.period }, `Stats: ${label}`)))));

    const notices = [
      session.readOnly ? h("div", { class: "banner banner--warn small" },
        "Ваш токен — только для чтения: кампании и доли видны, а Fetch, правки и Push вернут отказ.") : null,
      c.is_deleted ? h("div", { class: "banner banner--error" }, "Кампании больше нет в Keitaro (удалена или в архиве). Редактирование недоступно.") : null,
      state.stats && state.stats.available === false && state.stats.reason
        ? h("div", { class: "banner small muted" }, `Статистика недоступна: ${state.stats.reason}`) : null,
    ];
    const streams = c.streams.filter((s) => !s.is_deleted || s.offers.length);
    const ordered = [...streams.filter((s) => s.supports_offers), ...streams.filter((s) => !s.supports_offers)];

    mount(container, head, h("div", { class: "stack" }, notices,
      ordered.length ? ordered.map(streamCard)
        : h("div", { class: "card empty" }, h("strong", {}, "Потоки ещё не загружены"),
          "Нажмите «Fetch streams from KT», чтобы забрать их из Keitaro."),
      h("div", { class: "row", style: "justify-content:flex-end" },
        h("button", { class: "btn btn--danger btn--sm", disabled: c.is_deleted, onclick: async (event) => {
          const button = event.currentTarget; // после await у события currentTarget уже пуст
          const sure = await confirmDialog("Отправить кампанию в архив Keitaro?",
            `«${c.name}» (#${c.keitaro_id}) перестанет принимать трафик. Восстановить можно из архива в админке Keitaro.`,
            "В архив", "danger");
          if (!sure) return;
          try {
            await withBusy(button, () => api("DELETE", `/api/campaigns/${campaignId}`));
            toast("Кампания отправлена в архив Keitaro.");
            location.hash = "#/campaigns";
          } catch (error) { reportError(error); }
        } }, icon("trash"), "Кампанию в архив"))));
    restoreFocus(focusMemo);
  }

  try {
    await load({ autoFetch: true });
  } catch (error) {
    mount(container, h("div", { class: "banner banner--error" }, error.message),
      h("p", {}, h("a", { href: "#/campaigns" }, "← к списку кампаний")));
  }
}
