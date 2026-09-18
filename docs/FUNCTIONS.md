# Справочник функций

Что делает каждая публичная функция, зачем она нужна и каким тестом закреплена. Порядок идёт от ядра логики к маршрутам. Общая картина слоёв описана в [ARCHITECTURE.md](ARCHITECTURE.md).

Сокращения в колонке «Тест»:

| Сокращение | Файл |
|---|---|
| weights | `tests/test_weights.py` |
| video | `tests/test_editor_video_scenario.py` |
| failures | `tests/test_editor_failures.py` |
| more | `tests/test_editor_more.py` |
| creator | `tests/test_creator.py` |
| client | `tests/test_client.py` |
| meta | `tests/test_api_meta.py` |
| stats | `tests/test_stats.py` |
| security | `tests/test_security.py` |
| tools | `tests/test_tools.py` |
| advisor | `tests/test_advisor.py` |
| roles | `tests/test_auth_roles.py` |
| races | `tests/test_concurrency.py` |
| e2e | `scripts/live_e2e.py`, живой прогон на настоящем трекере |

Пометка «автотеста нет» означает: на момент написания функция проверяется только вручную или живым прогоном.

## app/services/weights.py

Калькулятор долей. Чистая арифметика: ни базы, ни сети. Правила с примерами приведены в [README](../README.md#правила-пересчёта-долей).

| Имя | Что делает | Тест |
|---|---|---|
| `WeightItem(key, share, pinned, order)` | Активный оффер глазами калькулятора. `key` задаёт любой уникальный номер, `order` задаёт порядок активации: больше значит позже. Объект неизменяемый | weights |
| `WeightsError(code, message)` | Распределить доли нельзя. Коды: `pinned_out_of_range`, `not_enough_for_free`, `share_out_of_range`, `unknown_item`, `pinned_sum_mismatch` | weights |
| `rebalance(items)` | Главная функция. Возвращает `{key: доля}` для всех переданных офферов. Закреплённые доли возвращает как есть. Остаток делит между свободными нацело, лишние проценты отдаёт последним по `order`. Если свободных нет, ничего не меняет. Если свободным не хватает по 1 %, бросает `not_enough_for_free`. Вход не изменяет | weights: `TestNumbersFromVideo`, `TestInvariants`, `TestPinnedEdgeCases` |
| `set_share(items, key, value)` | Ручной ввод доли. Проверяет диапазон 1–100, ставит значение, закрепляет его и вызывает `rebalance` для остальных. Если закреплены все и сумма не 100, бросает `pinned_sum_mismatch` | weights: `TestSetShare` |
| `equalize(items)` | Снимает закрепления и делит 100 % поровну. Редактор делает то же через `recalculate(drop_pins=True)` и эту функцию не вызывает. Она покрыта тестами как часть калькулятора | weights: `TestEqualizeAndCheck` |
| `check_distribution(items)` | Список проблем текущего распределения, пустой список значит порядок. Находит сумму не 100, долю 0 у активного оффера, пустой поток. Если закреплены все, добавляет подсказку про закрепления | weights: `TestEqualizeAndCheck`; failures |

## app/services/editor.py

Редактор потока: черновик в нашей базе, публикация одной кнопкой. Все функции правок меняют только базу AdRobot. В Keitaro пишет только `push`.

### Загрузка и импорт

| Имя | Что делает | Тест |
|---|---|---|
| `EditorError(code, message, http_status, details)` | Ошибка бизнес-логики с текстом для человека. Превращается в ответ API обработчиком в `app/main.py` | failures |
| `StreamLocks` | Прежнее имя замка, оставлено как синоним `CampaignLocks` из `app/services/locks.py` | tools: `TestCampaignLocks` |
| `load_campaign(session, campaign_id)` | Кампания с потоками и привязками одним запросом. Всегда перечитывает строки из базы (`populate_existing`). Нет кампании: `campaign_not_found`, 404 | video, failures |
| `load_stream(session, stream_id)` | Поток с привязками и кампанией. Нет потока: `stream_not_found`, 404 | video, failures |
| `import_campaigns(session, client)` | Кнопка «Обновить из Keitaro». Сводит шапки кампаний с трекером, потоки не трогает. Кампании, пропавшие из выдачи, помечает `is_deleted`, вернувшиеся возвращает в список. Возвращает `{total, created, gone}`. Двойной клик или второй воркер вставляют те же кампании одновременно: проигравший откатывается и проходит ещё раз, уже видя строки соседа, до трёх попыток | meta: `TestImport`; races: `test_double_click_on_import` |
| `get_or_import_campaign(session, client, keitaro_id)` | «Открыть по ID». Если кампании у нас нет, читает её из трекера и сохраняет шапку. Повторное открытие в трекер не ходит. Если строку успел вставить соседний запрос, перечитывает её вместо ошибки | meta: `TestOpenCampaign`; races: `test_same_campaign_opened_several_times_at_once` |

### Fetch streams

| Имя | Что делает | Тест |
|---|---|---|
| `fetch_streams(session, client, campaign, discard_draft=False)` | Забирает потоки кампании одним запросом. Обновляет шапки потоков и вызывает `_sync_bindings` для офферов. Поток, которого нет в выдаче, помечает `is_deleted`, но не удаляет. Возвращает `{streams, gone_streams, archived_offers}` | video: шаги 0:36 и 4:30; failures: `TestConflicts`; more: `TestRepeatedFetch`, `TestSchemaSwitchedInKeitaro` |
| `_sync_bindings(stream, kt_offers, discard_draft)` | Сводит привязки с трекером. Без черновика зеркалит Keitaro: новые офферы появляются со своей долей, пропавшие уходят в архив с 0 %. Доли не пересчитывает. При черновике работает трёхстороннее слияние: изменённые привязки сохраняют черновик, нетронутые принимают состояние трекера, опубликованная половина обновляется у всех. С `discard_draft=True` черновик выбрасывается | failures: `test_fetch_keeps_draft_and_merges_untouched_offers`, `test_fetch_with_discard_draft_mirrors_keitaro` |
| `_kt_offer_rows(row)` | Приводит офферы из ответа трекера к виду `{offer_id, share, state, binding_id}`. Состояние сводится к `active` или `disabled`, нечисловая доля становится 0, мусорные строки пропускаются | more: `TestStrangeDataFromKeitaro` |
| `_stream_summary(row)` | Короткая справка о потоке для показа: куда ведёт, какие фильтры, сколько лендингов. Логика редактора её не читает | автотеста нет |

### Правки черновика

| Имя | Что делает | Тест |
|---|---|---|
| `add_offer(session, dictionaries, stream, offer_id)` | Add. Отказывает, если оффер уже в потоке (`offer_already_in_stream`). Оффер из архива потока возвращает через `bring_back` с его проверками. Новый оффер обязан быть в справочнике и быть активным, иначе `offer_not_usable`. Новую привязку ставит в конец порядка активации и пересчитывает доли. При ошибке пересчёта транзакция откатывается, следов не остаётся | video: шаг 2:01; failures: `TestOfferValidation`, `test_add_when_everything_is_pinned_to_100`; races: `TestOneStream` |
| `remove_offer(session, stream, binding_id)` | Remove. Опубликованный оффер получает состояние `removed` и долю 0. Закрепление не сбрасывается, чтобы отмена Remove вернула оффер прежним. Оффер, которого в трекере ещё не было, удаляется совсем. Остальные пересчитываются. Если пересчёт невозможен из-за закреплений, доли остаются как есть, а проблему покажет `stream_problems` | video: шаг 3:25; failures: `test_all_pinned_after_remove_blocks_push_with_hint` |
| `bring_back(session, dictionaries, stream, binding_id, check_offer=True)` | Bring back. Оффер снова активен, получает новый `sort_index`, то есть встаёт последним в порядке активации. Старое закрепление снимается: доля считается заново. Перед этим оффер проверяется через `_offer_problems` | video: шаги 3:56 и 5:21; tools: `TestInvisibleOffers`; more: `test_bring_back_does_not_resurrect_old_pin` |
| `set_pin(session, stream, binding_id, pinned)` | Закрепляет или открепляет долю активного оффера. Цифр не меняет, поток не становится жёлтым | video: шаг 5:01; meta: `test_pin_alone_is_not_a_draft`; more: `test_unpinned_share_joins_next_rebalance` |
| `set_share(session, stream, binding_id, value)` | Ручная доля: значение закрепляется, остальные незакреплённые пересчитываются. Ошибки калькулятора превращаются в `EditorError` с тем же кодом | failures: `TestWeightsGuards` |
| `recalculate(session, stream, drop_pins=False)` | «Пересчитать» делит остаток с учётом закреплений. С `drop_pins=True` это «Выровнять поровну»: закрепления снимаются. Без активных офферов отвечает `no_active_offers` | failures: `test_all_pinned_after_remove_blocks_push_with_hint`, `test_keitaro_sum_not_100_is_mirrored_with_warning_and_fixable` |
| `cancel(session, stream)` | Cancel. Привязки, которых никогда не было в трекере, удаляются. Оффер, возвращённый из архива, уходит обратно в архив. Остальные получают опубликованные состояние и долю. Pin и unpin не откатываются: закрепление не входит в черновик. Отмена Remove возвращает оффер вместе с закреплением, потому что Remove его не снимал. Возвращает число отменённых изменений | video: шаг 2:55, `test_cancel_restores_removed_offer_and_its_share`, `test_cancel_after_bring_back_returns_offer_to_archive` |
| `_offer_problems(session, dictionaries, bindings)` | Почему офферы этих привязок нельзя отправить в трекер, пустой список значит можно. Оффер, не видимый ключу API, пропускается, если привязка уже бывала в Keitaro: значит, он существует. Оффер, который справочник знает как выключенный или удалённый, блокируется всегда | tools: `TestInvisibleOffers` |
| `apply_shares(session, stream, shares)` | Применяет готовый набор долей в черновик, им пользуется советник. Ключи набора: ID привязок. Закрепления не ставит и не снимает. Отказывает, если в наборе есть неактивная привязка (`binding_not_found`), меняется закреплённая доля (`pinned_share`) или сумма вместе с нетронутыми офферами не равна 100 (`invalid_distribution`) | tools: `test_apply_goes_to_draft_then_usual_push`, `test_apply_shares_guards` |
| `forget_binding(session, stream, binding_id)` | Убирает оффер из архива потока насовсем. Разрешено только для архивного оффера, которого уже нет в трекере, иначе `not_archived` | failures: `test_forget_archived_offer`, `test_forget_is_refused_for_live_offer` |

### Сравнение и публикация

| Имя | Что делает | Тест |
|---|---|---|
| `stream_is_dirty(stream)` | Есть ли у потока неопубликованные изменения. Флаг не хранится, он вычисляется по привязкам. Добавил оффер и сразу убрал: поток снова чистый | video: `test_remove_of_unpublished_offer_then_push_never_reaches_keitaro` |
| `stream_problems(stream)` | Что мешает публикации. Для потока без офферов и для пустого чистого потока возвращает пустой список. Иначе вызывает `check_distribution` | failures |
| `stream_diff(stream)` | Список «было в Keitaro, станет после Push». Типы изменений: `add`, `remove`, `share`, `state` | meta: `test_push_record_keeps_diff_and_published_set`; more: `test_diff_lists_every_kind_of_change` |
| `push(session, client, dictionaries, stream, force=False, allow_empty=False)` | Push to KT. Шаги: поток редактируем и изменён, доли сходятся, новые офферы проходят `_offer_problems`, трекер не менялся с последней синхронизации, снимок, `PUT`, сверка ответа, запись опубликованного состояния. Снимок коммитится до отправки `PUT`: если ответ потеряется, точка отката уже есть. `force=True` публикует поверх чужих правок. `allow_empty=True` разрешает пустой поток. Коды отказов: `nothing_to_push`, `empty_stream`, `invalid_distribution`, `offer_not_usable`, `conflict`, `push_mismatch`. Число хранимых снимков задаёт `SNAPSHOTS_PER_STREAM` | video: `test_push_sends_single_partial_put_with_only_offers`; failures: `TestConflicts`, `TestNetworkFailures`; races: `test_two_simultaneous_pushes_publish_once` |
| `_push_payload(stream)` | Тело публикации: активные и выключенные офферы в порядке активации. Архивные не входят, так они исчезают из трекера. Лендинги потока в тело не попадают и в трекере остаются | video; more: `TestLandingsSurvivePush` |
| `_signature(rows)` | Отсортированный список троек `(offer_id, share, state)`. Сравнение подписей отвечает на вопрос, совпадают ли два набора офферов | failures |
| `list_snapshots(session, stream_id)` | Снимки потока, новые сверху. Хранится `SNAPSHOTS_PER_STREAM` последних на каждый поток, лишние удаляет `_trim_snapshots` при Push | failures: `TestSnapshotsAndRollback`; more: `TestSnapshots` |
| `restore_snapshot(session, stream, snapshot_id)` | Откат. Снимок загружается в черновик, закрепления снимаются. Офферов, которых не было в снимке, ждёт архив либо удаление. В трекер состояние уйдёт только после Push | failures: `test_rollback_goes_through_draft_not_straight_to_keitaro`; more: `TestSnapshots`, `TestForeignIds` |
| `stream_view(stream, offers)` | Представление потока для интерфейса: строки офферов с признаками `is_dirty`, `in_keitaro`, `offer_known`, список изменений, проблемы, сумма долей. Порядок строк: активные по убыванию доли, затем выключенные, затем архив | video: проверки `editor.order()` |

## app/services/creator.py

Создаватор: имя, гео и оффер превращаются в кампанию с двумя потоками.

| Имя | Что делает | Тест |
|---|---|---|
| `CreatorError(code, message, http_status, errors, details)` | Ошибка создания. `errors` хранит все проблемы формы разом | creator |
| `CampaignPlan` | Что уйдёт в трекер для одной кампании: тела трёх запросов, предупреждения, признак своего alias. `as_dict()` отдаёт план для dry-run | creator: `test_dry_run_sends_nothing` |
| `generate_alias(length=8)` | Случайный alias из латиницы и цифр. Keitaro требует alias при создании и не терпит повторов | creator |
| `validate_redirect_url(url)` | Принимает только полный адрес с `http` или `https`. Отсекает `javascript:` и адреса без схемы. Этой же функцией проверяются настройки | creator: `test_garbage_is_rejected_before_any_write` |
| `split_shares(offer_ids)` | Доли офферов второго потока по тем же правилам, что в редакторе: три оффера дадут 33/33/34 | creator: `test_several_offers_split_evenly` |
| `CampaignCreator.build_plans(session, request)` | Проверяет ввод и готовит планы, в трекер не пишет. Разбирает гео, проверяет офферы, группу, источник, домен и адрес редиректа. Собирает все ошибки и бросает одну `validation`. При `split_by_geo` делает план на каждую страну. Подстановка `{geo}` в названии работает всегда: код страны либо коды через плюс для одной кампании на несколько стран. Без подстановки к названию дописывается `[код]`, но только при разбиении по странам. Параметры источника копирует в кампанию | creator: `TestHappyPath`, `TestValidationBeforeNetwork`; meta: `TestGeoPlaceholderInName` |
| `CampaignCreator.request_hash(request)` | SHA-256 от тела запроса без поля `dry_run`. По нему отличают повтор от чужого запроса с тем же ключом | creator: `test_same_key_with_other_body_is_conflict` |
| `CampaignCreator.find_previous(session, key, request)` | Ищет прежний результат по ключу идемпотентности. Другое тело даёт `idempotency_mismatch`, незавершённый запрос даёт `in_progress`, готовый результат возвращается с `replayed: true`. Отметка «выполняется» старше пяти минут считается брошенной: запись удаляется, запрос идёт заново | creator: `TestIdempotencyAndCompensation`; races: `TestCreator` |
| `CampaignCreator.create(session, request, idempotency_key=None)` | Весь сценарий: чистка ключей старше `IDEMPOTENCY_TTL_DAYS`, повтор по ключу, занятие ключа, планы, проверка названия, создание, сохранение результата. Ключ занимается раньше проверок, чтобы отставший дубль получил `in_progress`, а не «название занято». При `dry_run` ключ не занимается. При неудаче ключ освобождается, чтобы попытку можно было повторить | creator; races: `TestCreator`; e2e |
| `_reserve_key`, `_release_key` | Ключ записывается в базу до первого запроса в трекер. Уникальность первичного ключа пропускает только один из двух одновременных дублей | creator: `test_failed_attempt_releases_key_for_retry` |
| `_ensure_names_free(plans)` | Сверяет названия с кампаниями трекера без учёта регистра. Совпадение даёт `duplicate_name`, 409. Обходится полем `allow_duplicate_name` | creator: `test_duplicate_name_needs_confirmation` |
| `_create_one(session, plan)` | Сага одной кампании: `POST /campaigns`, два `POST /streams`, сверка, сохранение у нас. Сбой на потоках запускает `_compensate` и даёт `stream_creation_failed` | creator: `test_stream_failure_archives_half_created_campaign`, `test_second_stream_failure_also_compensated` |
| `_post_campaign(plan)` | Создаёт кампанию. Если случайный alias занят, берёт другой, до четырёх попыток. Свой alias не подменяет: отвечает `alias_taken` | creator: `test_custom_alias_conflict_is_reported_not_silently_replaced` |
| `_verify(plan, geo_stream, offer_stream)` | Сверяет ответ трекера с запросом: страны фильтра, схема первого потока, офферы и доли второго | creator |
| `_compensate(keitaro_campaign_id)` | Отправляет недосозданную кампанию в архив. Возвращает, удалось ли это | creator: `test_failed_compensation_is_reported_loudly` |
| `_save_locally(...)` | Сохраняет кампанию и оба потока в нашу базу сразу как опубликованные. Редактор готов без Fetch | creator: `test_result_has_links_and_editor_is_ready_without_fetch` |

## app/services/dictionaries.py

Справочники трекера и значения по умолчанию.

| Имя | Что делает | Тест |
|---|---|---|
| `Lookups` | Снимок справочников: группы, источники, домены, признак видимости доменов, угаданный домен, предупреждения. Методы `group_ids()`, `source_ids()`, `domain_ids()`, `source_by_id()` | creator |
| `DictionaryService.refresh_offers(session, force=False)` | Обновляет кэш офферов в таблице `offers`, если он старше `DICTIONARY_TTL_SECONDS`. Грузит справочник один запрос за раз, остальные ждут замка и берут готовое. Офферы, пропавшие из выдачи, помечает `is_missing`. Возвращает число офферов либо −1, если кэш свежий. Признак «ещё не загружали» хранится как `None`, а не нулём: время `time.monotonic()` идёт от старта машины, и ноль на свежей машине выглядел бы свежей загрузкой | meta: `TestOffersRefresh`; meta: `TestDictionaryCacheOnFreshlyBootedMachine`; races: `test_first_requests_after_install_do_not_collide_on_offers_cache`, `test_autocomplete_burst_loads_offers_from_keitaro_once` |
| `DictionaryService.search_offers(session, query, limit=20, include_inactive=False)` | Поиск для автокомплита: префикс ID и подстрока названия без учёта регистра. Фильтрует в Python, потому что `LIKE` в SQLite не умеет кириллицу без регистра. Сначала точный ID, затем префикс ID, затем начало названия | meta: `TestOfferSearch` |
| `DictionaryService.get_offers_map(session, offer_ids)` | Офферы из кэша по списку ID, нужны названия в редакторе | video, failures |
| `DictionaryService.ensure_offers_usable(session, offer_ids)` | Проверка перед отправкой в трекер. Если оффер не найден, справочник перечитывается принудительно и проверка повторяется. Возвращает список проблем: «не найден» или «в состоянии deleted» | creator: `test_archived_offer_is_rejected`; failures: `TestOfferValidation` |
| `DictionaryService.get_lookups(force=False)` | Группы кампаний, активные источники и активные домены, кэш в памяти. Если список доменов пуст или запрещён, включает режим скрытых доменов и угадывает домен по кампаниям | meta: `TestLookups`; creator: `test_domain_hidden_from_api_key_is_inferred_from_campaigns` |
| `_infer_domain_id()` | Самый частый `domain_id` среди кампаний, видимых ключу | meta: `test_most_popular_domain_wins_the_inference` |
| `get_overrides`, `save_overrides` | Чтение и запись настроек из интерфейса, таблица `app_settings`. Пустое значение означает «вернуть автоматическое». Первое сохранение из двух вкладок сразу не даёт ошибки: проигравшая попытка откатывается и повторяется как обновление | meta: `TestSettings` |
| `DictionaryService.resolve_defaults(session)` | Итоговые значения по умолчанию и источник каждого: «настройки», «.env», «авто» или «не задано». Внутри вызывает `get_lookups`, поэтому при устаревшем кэше обращается к трекеру | meta: `TestSettings`; creator |
| `DictionaryService.tracking_domain_url(session)` | Адрес трекинг-домена для ссылок кампаний: настройка из интерфейса, иначе значение из `.env`. В сеть не ходит, поэтому кампания открывается и при недоступном трекере | meta: `TestCampaignViewIsLocal` |

## app/services/stats.py

| Имя | Что делает | Тест |
|---|---|---|
| `PERIODS` | Сколько дней показывать для периода интерфейса, считая сегодня: `today` даёт 1, `7d` даёт 7, `30d` даёт 30. Незнакомый период заменяется на `7d`, в ответе тоже | stats: `TestPeriods` |
| `campaign_stats(client, keitaro_campaign_id, period="7d")` | Два отчёта на кампанию: итоги по парам поток и оффер, клики по дням для тренда. Период задаётся явными датами `from` и `to` в UTC, а не пресетами Keitaro: пресет вида `7_days_ago` захватывает лишний день, и итог перестаёт сходиться с суммой тренда. Ответ: `{available, period, days, offers, streams}`. Ключ оффера имеет вид `"<stream_id>:<offer_id>"`. Любая ошибка трекера даёт `available: false` с причиной, редактор при этом работает | stats: `TestTotals`, `TestTrend`, `TestReportsUnavailable` |

`_number(value)` приводит значение отчёта к числу. Строки, пустые значения, `nan` и `inf` превращаются в ноль: `int()` на них падает, а в JSON их не передать. Тест: stats, `test_not_finite_numbers_do_not_crash_stats`.

## app/services/audit.py

| Имя | Что делает | Тест |
|---|---|---|
| `current_actor` | Контекстная переменная с именем того, кто выполняет запрос. Её выставляет middleware из заголовка `X-AdRobot-User`: значение раскодируется, обрезается до 64 символов и не переходит на следующий запрос | security: `TestActorHeader` |
| `record(session, action, ...)` | Добавляет запись журнала без коммита и дублирует её в лог. Поля: действие, итог, кампания, поток, сводка, детали, ошибка, длительность | creator: `test_operations_log_records_success_and_failure` |
| `record_failure(session, action, error, ...)` | Откатывает незавершённую транзакцию и сохраняет запись об ошибке | failures: `test_failures_are_written_to_operations_log` |

## app/services/locks.py

Замок на кампанию: правки одной кампании идут строго по очереди, в том числе между процессами. Устройство описано в [ARCHITECTURE.md](ARCHITECTURE.md#замок-на-кампанию).

| Имя | Что делает | Тест |
|---|---|---|
| `LOCK_TTL`, `ACQUIRE_TIMEOUT_SECONDS` | Срок жизни строки-замка, 180 секунд, и предел ожидания своей очереди, 20 секунд | tools: `TestCampaignLocks` |
| `LockBusyError` | Дождаться замка не удалось. Код `campaign_busy`, статус 409, текст просит повторить через несколько секунд | tools: `test_busy_campaign_answers_409` |
| `CampaignLocks(acquire_timeout=20.0)` | Выдаёт замки по ID кампании. Один экземпляр на приложение | races; tools |
| `CampaignLocks.for_campaign(campaign_id)` | Асинхронный контекстный менеджер. Сначала берёт `asyncio.Lock` процесса, затем вставляет строку в `campaign_locks`. На выходе удаляет строку по метке владельца. Сбой при снятии замка операцию не ломает: строка истечёт сама | tools: `test_second_process_waits_then_gives_up`; races: `TestOneStream` |
| `_acquire(campaign_id, owner)` | Удаляет просроченную строку кампании и вставляет свою. Занятый ключ и занятая база означают «подождать»: попытка повторяется каждые 0,15 секунды до предела ожидания, затем `LockBusyError`. Ошибка «нет таблицы» пробрасывается сразу: миграции не применены, ждать бессмысленно | tools: `test_abandoned_lock_expires` |

## app/services/advisor.py

Советник долей. Чистая функция: ни базы, ни сети. Ничего не публикует и не меняет черновик.

| Имя | Что делает | Тест |
|---|---|---|
| `SMOOTHING_CLICKS` | Вес среднего по потоку в оценке каждого оффера, 50 кликов | advisor |
| `AdvisorInput(key, share, pinned, clicks, conversions, revenue)` | Оффер глазами советника: ID привязки, текущая доля, закрепление и статистика за период | advisor |
| `Advice(key, current, proposed, score, reason)` | Строка предложения: было, предлагается, сглаженная оценка и объяснение словами | advisor |
| `AdvisorResult(ready, message, metric, items)` | Итог. `ready=False` значит, что совета нет, причина лежит в `message`. Свойство `changed` отвечает, отличается ли предложение от текущих долей | advisor |
| `advise(items, metric="cr", floor=5, cap=80, max_step=15, min_clicks=100, min_offer_clicks=30)` | Строит предложение. Оценка оффера: конверсии или выручка на клик, сглаженные к среднему по потоку по формуле `(успехи + среднее × 50) / (клики + 50)`. Целевые доли пропорциональны оценкам. Каждому офферу не меньше `floor` и не больше `cap` процентов, за раз доля сдвигается не больше чем на `max_step` пунктов. Закреплённые доли не трогает, сумма всегда 100. Оффер с выборкой меньше `min_offer_clicks` не двигается вовсе и в расчёте ведёт себя как закреплённый. Отказывает с объяснением, если кликов на поток меньше `min_clicks`, данных хватает меньше чем по двум офферам, незакреплённых офферов меньше двух, нет ни конверсий, ни выручки либо закрепления не оставляют места. Незнакомая метрика заменяется на `cr`. Вход не изменяет, результат детерминирован | advisor: `TestReadiness`, `TestProposal`, `TestInvariants` |
| `_fit(raw, bounds, budget, priority)` | Подбирает целые доли как можно ближе к расчётным, строго в границах и с суммой ровно `budget`. Недостающие пункты отдаёт лучшим офферам, лишние снимает с худших. Если границы шага несовместны с бюджетом, ослабляет их: сумма важнее шага | advisor: `test_sum_is_always_100_and_bounds_hold`, `test_many_pins_leave_small_budget` |

## app/services/usage.py

Работа с одним оффером сразу по многим кампаниям. Новой логики долей здесь нет: всё идёт через функции редактора.

| Имя | Что делает | Тест |
|---|---|---|
| `MAX_BULK_STREAMS` | Предел потоков в одной массовой операции, 100 | tools |
| `offer_usage(session, offer_id)` | Где стоит оффер по данным базы AdRobot. Возвращает `used` (потоки, где оффер активен, выключен, ждёт удаления или добавлен в черновик) и `available` (потоки с офферами, где его нет либо он лежит в спокойном архиве). Потоки без офферов, удалённые потоки и скрытые кампании не учитываются. `has_unsynced_campaigns` предупреждает, что часть кампаний ещё ни разу не загружалась и список может быть неполным | tools: `TestOfferUsage` |
| `bulk_add(session, dictionaries, locks, offer_id, stream_ids)` | Добавляет оффер в черновики выбранных потоков. Оффер проверяется один раз в начале. Каждый поток берёт замок своей кампании и проходит обычный `editor.add_offer`. Итог по каждому потоку отдельно: `added` с новой долей либо `skipped` с кодом и текстом причины. В трекер ничего не уходит | tools: `test_bulk_add_writes_only_drafts`, `test_bulk_add_reports_skipped_streams`, `test_bulk_add_of_unknown_offer_is_refused` |
| `push_many(session, client, dictionaries, locks, stream_ids)` | Публикует потоки по очереди. Каждый проходит обычный `editor.push`: проверка долей, сверка конфликта, снимок, сверка ответа. Режимы `force` и `allow_empty` здесь недоступны. Конфликт, сбой трекера или занятый замок дают статус `error` у этого потока и остальным не мешают | tools: `test_push_many_publishes_each_stream_with_usual_checks` |
| `sync_all(session, client, locks, only_missing=False)` | Обновляет список кампаний и выполняет Fetch по каждой, черновики сохраняются. С `only_missing=True` обходит только кампании, которые ещё не загружались. Если трекер недоступен или отверг ключ, обход останавливается на первой же кампании. Возвращает `{synced, failed}` | tools: `test_sync_all_stops_hammering_dead_tracker` |
| `dirty_stream_ids(session)` | ID потоков с неопубликованными изменениями | tools |

## app/keitaro/client.py

Тонкая обёртка над Admin API. Бизнес-логики нет. Особенности самого API описаны в [KEITARO_API_NOTES.md](KEITARO_API_NOTES.md).

| Имя | Что делает | Тест |
|---|---|---|
| `KeitaroClient(base_url, api_key, timeout, max_retries, max_concurrency, user_agent, transport, backoff_base)` | Создаёт HTTP-клиент с заголовками `Api-Key` и `User-Agent`. Редиректам не следует, поэтому ключ не уйдёт на чужой хост. `transport` подменяет сеть в тестах. Семафор ограничивает число одновременных запросов | client: `TestConfiguration`, `TestConcurrencyLimit` |
| `configured`, `aclose()` | Заданы ли адрес и ключ. Закрытие соединений при остановке приложения | client: `TestConfiguration` |
| `_request(method, path, json, params, idempotent)` | Единая точка отправки. `GET`, `PUT` и `DELETE` повторяются при сетевой ошибке и ответах 429, 502, 503, 504. Пауза растёт вдвое, учитывает `Retry-After` и не превышает 15 секунд. `POST` не повторяется. При таймауте и при обрыве на чтении ответа текст ошибки предупреждает, что запрос на создание мог выполниться. При отказе в соединении такого предупреждения нет: запрос до трекера не дошёл. Без настроек сразу бросает `KeitaroNotConfiguredError` | client: `TestRetries`; failures: `test_put_applied_but_connection_lost_is_healed_by_retry`; creator: `test_timeout_on_create_is_not_retried_and_warns` |
| `_error_from_response(response, method, path)` | Разбирает ошибку любого формата: JSON `{"error": ...}`, JSON `{поле: [сообщения]}`, простой текст, страница Cloudflare. Возвращает исключение нужного класса с русским текстом | client: `TestErrorMapping`; failures: `test_cloudflare_block_is_explained` |
| `_scrub(error)` | Вырезает ключ API из текста и деталей ошибки. Нужен на случай, когда прокси или отладочная страница перед трекером возвращает в теле заголовки запроса. Ключ короче шести символов не вырезается, иначе пострадал бы обычный текст | client: `TestKeyNeverLeaks` |
| `_parse(response, method, path)` | Успешный ответ превращает в JSON. Не-JSON при коде 200 считает ошибкой `keitaro_bad_response`: так выглядит неверный адрес трекера. Ответ 3xx тоже ошибка протокола: Admin API не перенаправляет, редирект значит, что перед нами страница входа или прокси | client: `test_html_with_status_200_is_protocol_error`; failures: `test_html_instead_of_json` |
| `_expect_list`, `_expect_dict` | Проверяют форму ответа. Список из одного объекта принимается как объект: так отвечают некоторые методы трекера. Строки списка, не являющиеся объектами, отбрасываются | client: `TestErrorMapping` |
| `ping()` | Самый лёгкий запрос для проверки связи и ключа: группы кампаний | meta: `TestHealth` |
| `list_offers()`, `list_groups(type)`, `list_traffic_sources()`, `list_domains()` | Справочники целиком. У этих методов трекера нет ни фильтров, ни страниц | creator, failures |
| `list_campaigns()` | Все кампании ключа страницами по 500 через `limit` и `offset`. Если трекер игнорирует `limit` и отдаёт всё сразу, хватает одного запроса. Страница без единой новой кампании останавливает обход: значит, трекер игнорирует и `offset`. Дубли по ID убираются | client: `TestCampaignPages` |
| `get_campaign(id)`, `create_campaign(payload)` | Чтение и создание кампании | creator, video |
| `archive_campaign(id)` | `DELETE /campaigns/{id}`, в Keitaro это перенос в архив. Ответ 404 считается успехом: цель достигнута. Другие ошибки не глотаются | client: `TestArchive` |
| `get_campaign_streams(id)`, `get_stream(id)`, `create_stream(payload)` | Потоки кампании с вложенными офферами и фильтрами, один поток, создание потока | creator, video |
| `update_stream_offers(stream_id, offers)` | `PUT /streams/{id}` с телом `{"offers": [...]}`. Остальные поля потока трекер не трогает | video: `test_push_sends_single_partial_put_with_only_offers` |
| `build_report(payload)` | `POST /report/build`. Это чтение методом POST, поэтому повторяется как идемпотентный запрос | client: `test_report_build_is_post_but_retried_because_it_only_reads` |

## app/keitaro/errors.py

У каждой ошибки есть машинный код и HTTP-статус, которым AdRobot отвечает своему интерфейсу.

| Класс | Код | Статус | Когда |
|---|---|---|---|
| `KeitaroNotConfiguredError` | `keitaro_not_configured` | 503 | Не заданы адрес или ключ |
| `KeitaroAuthError` | `keitaro_unauthorized` | 502 | Трекер ответил 401 |
| `KeitaroForbiddenError` | `keitaro_forbidden` | 502 | 403 по правам, блокировка Cloudflare, ответ 402: Admin API недоступно, потому что лицензия не оплачена либо в этой редакции трекера API не предусмотрено |
| `KeitaroNotFoundError` | `keitaro_not_found` | 404 | Объекта нет в трекере |
| `KeitaroValidationError` | `keitaro_validation` | 422 | Трекер отверг данные, в `details` лежит словарь по полям |
| `KeitaroRateLimitError` | `keitaro_rate_limited` | 503 | Ответ 429, когда повторы исчерпаны или запрещены |
| `KeitaroServerError` | `keitaro_server_error` | 502 | Ответ 5xx |
| `KeitaroNetworkError` | `keitaro_unreachable` | 504 | Таймаут, обрыв, DNS |
| `KeitaroProtocolError` | `keitaro_bad_response` | 502 | Пришёл не JSON или JSON неожиданной формы |

## app/keitaro/countries.py

Справочник стран для фильтра потока. Нужен потому, что Keitaro принимает в фильтре любые строки.

| Имя | Что делает | Тест |
|---|---|---|
| `COUNTRIES` | 249 присвоенных кодов ISO 3166-1 alpha-2 с названиями на английском и русском. Выведенных из обращения и пользовательских кодов нет | creator косвенно |
| `ALIASES` | Коды alpha-3 и частые неверные обозначения: `UK`, `EL`, `UAE` и другие | creator: `test_uk_alias_is_fixed_to_gb` |
| `normalize_country_code(raw)` | Код alpha-2 для кода, алиаса или полного названия, иначе `None`. Не учитывает регистр, «ё», кавычки, точки и диакритику | creator |
| `parse_geo_input(raw)` | Разбирает строку вида `mx, au; Румыния` на коды и нераспознанные куски. Сначала делит по запятым, точкам с запятой, слэшам и переводам строк, затем по пробелам. Соседние слова склеивает, пока они дают название страны. Дубли убирает, порядок ввода сохраняет | meta: `TestGeoParse`; creator: `test_multi_geo_in_one_campaign` |
| `country_name(code, lang="ru")` | Название страны по коду. Для незнакомого кода возвращает сам код | creator |
| `search_countries(query, limit=20)` | Поиск стран для автокомплита: точное совпадение, префикс кода, префикс названия, вхождение. Интерфейс сейчас пользуется только разбором строки, поиск доступен через API | meta: `TestCountries` |

## app/api/deps.py

| Имя | Что делает | Тест |
|---|---|---|
| `get_client`, `get_dictionaries`, `get_creator`, `get_locks`, `get_app_settings` | Достают общие объекты из `app.state`. Они создаются один раз при старте. `get_locks` отдаёт `CampaignLocks` | все тесты |
| `get_session` из `app/db.py` | Одна сессия базы на запрос. Коммит делает сервисный слой | все тесты |
| `require_token(request, credentials)` | Проверка доступа к `/api`. Токен описан схемой безопасности `HTTPBearer`, поэтому в Swagger UI есть кнопка Authorize и Try it out отправляет токен. Принимает общий `ADROBOT_AUTH_TOKEN` и именованные `ADROBOT_AUTH_TOKENS`, оба режима работают вместе. Если ничего не задано, доступ открыт. Сравнение идёт за постоянное время со всеми токенами без раннего выхода. Нет совпадения: 401 с заголовком `WWW-Authenticate`. Имя именованного токена записывается в `current_actor` и становится проверенным автором в журнале. Токен с пометкой `ro` получает 403 на любой метод, кроме `GET`, `HEAD` и `OPTIONS`. Токен в строке запроса не принимается | security: `TestTokenMode`; roles |
| `SessionDep`, `ClientDep`, `DictionariesDep`, `CreatorDep`, `LocksDep`, `SettingsDep` | Короткие имена зависимостей для сигнатур маршрутов | все тесты |

## app/api/routes_meta.py

| Маршрут и функция | Что делает | Тест |
|---|---|---|
| `GET /api/health`, `health` | Состояние базы, признак настройки и доступность трекера. Итог: `ok`, `setup_required`, `degraded` или `error`. С `deep=false` в трекер не ходит. Базу проверяет по одной таблице из каждой миграции, `campaigns` и `campaign_locks`. Так видно и «забыли upgrade», и «обновили код, но не базу». Если таблиц нет, поле `database` подсказывает `alembic upgrade head`, а интерфейс показывает экран «База данных не готова» | meta: `TestHealth` |
| `GET /api/meta/lookups`, `lookups` | Справочники формы создания и значения по умолчанию. `refresh=true` сбрасывает кэш. Параметры источников наружу не отдаются | meta: `TestLookups` |
| `GET /api/meta/countries`, `countries` | Поиск стран | meta: `TestCountries` |
| `GET /api/meta/geo/parse`, `geo_parse` | Разбор строки гео на коды с названиями и нераспознанные куски. Форма вызывает его на лету | meta: `TestGeoParse` |
| `GET /api/offers`, `offers` | Автокомплит офферов. Строка ответа: `id`, `name`, `state`, `country`, `label` | meta: `TestOfferSearch` |
| `POST /api/offers/refresh`, `offers_refresh` | Принудительно перечитывает справочник офферов | meta: `TestOffersRefresh` |
| `GET /api/settings`, `settings_get` | Значения по умолчанию с источниками | meta: `TestSettings` |
| `PUT /api/settings`, `settings_put` | Сохраняет значения из интерфейса. Адреса проверяет той же функцией, что создаватор | meta: `TestSettings`; security: `test_settings_accept_only_http_urls` |
| `GET /api/operations`, `operations` | Журнал: фильтры по кампании и итогу, страницы, новые сверху | meta: `TestOperationsJournal` |

## app/api/routes_campaigns.py

| Маршрут и функция | Что делает | Тест |
|---|---|---|
| `audited(session, action, **ids)` | Контекстный менеджер. Оборачивает операцию записью в журнал при успехе и при ошибке, считает длительность, делает коммит. Операция может выставить `info["status"]` и `info["error"]` сама: так в журнал попадает неудача, которая не бросила исключения | creator, failures |
| `DIRTY_BINDING` | SQL-двойник свойства `StreamOffer.is_dirty`. Находит кампании с черновиками одним запросом | meta: `test_only_drafts_shows_campaigns_with_unpublished_changes` |
| `campaign_view(session, campaign, dictionaries, settings)` | Представление кампании для интерфейса: шапка, гео с названиями, ссылки, потоки через `stream_view`. Адрес трекинг-домена берёт из `tracking_domain_url`, в сеть не ходит | video, creator |
| `GET /api/campaigns`, `list_campaigns` | Список из нашей базы: поиск по названию, alias и началу KT ID, фильтр по происхождению, только с черновиками, страницы | meta: `TestCampaignList` |
| `POST /api/campaigns/import`, `import_campaigns` | Кнопка «Обновить из Keitaro» | meta: `TestImport` |
| `POST /api/campaigns`, `create_campaign` | Создаватор. Читает заголовок `Idempotency-Key`. Проверка без создания пишется в журнал отдельным действием. Ответ всегда 200 со списком `results`: при разбиении по странам возможен частичный успех. Если не создано ничего, запись журнала получает статус `error` и текст ошибки | creator; races: `TestCreator`; e2e |
| `POST /api/campaigns/open/{keitaro_id}`, `open_campaign` | Открыть кампанию трекера по ID | meta: `TestOpenCampaign`; races: `TestCampaigns` |
| `GET /api/campaigns/{id}`, `get_campaign` | Кампания с потоками из базы, без обращения к трекеру. Открывается и при недоступном Keitaro | video, creator; meta: `TestCampaignViewIsLocal` |
| `POST /api/campaigns/{id}/fetch`, `fetch_streams` | Fetch под замком кампании. После него обновляются названия офферов. Сбой справочника Fetch не роняет. Возвращает сводку и свежий вид кампании | video, failures |
| `GET /api/campaigns/{id}/stats`, `campaign_stats` | Статистика для колонок Stats и Trends. Следов в журнале и черновике не оставляет | stats |
| `DELETE /api/campaigns/{id}`, `archive_campaign` | Отправляет кампанию в архив трекера и скрывает её у нас. При сбое трекера кампания остаётся видимой | meta: `TestArchiveCampaign`; races: `test_double_click_on_archive` |

## app/api/routes_streams.py

Все правки работают одинаково: замок кампании, загрузка потока, операция внутри `audited`, в ответ свежий вид потока. Интерфейс всегда показывает то, что лежит в базе.

| Маршрут и функция | Что делает | Тест |
|---|---|---|
| `GET /api/streams/{id}`, `get_stream` | Поток с офферами, долями, списком изменений и проблемами. Работает без обращения к трекеру | meta: `test_single_stream_view_works_offline_too` |
| `POST /api/streams/{id}/offers`, `add_offer` | Add | video, failures |
| `DELETE /api/streams/{id}/offers/{binding_id}`, `remove_offer` | Remove | video |
| `POST .../offers/{binding_id}/bring-back`, `bring_back` | Bring back | video |
| `PUT .../offers/{binding_id}/pin`, `pin` | Закрепить или открепить. В журнале это действия `pin` и `unpin` | video |
| `PUT .../offers/{binding_id}/share`, `share` | Ручная доля от 1 до 100, диапазон проверяет схема запроса | failures |
| `DELETE .../offers/{binding_id}/forget`, `forget` | Убрать оффер из архива насовсем | failures |
| `POST /api/streams/{id}/recalculate`, `recalculate` | «Пересчитать» либо «Выровнять поровну» при `drop_pins: true`. В журнале это `recalculate` и `equalize` | failures |
| `POST /api/streams/{id}/push`, `push` | Push to KT. В журнал пишутся список изменений и опубликованный набор | video, failures; e2e |
| `POST /api/streams/{id}/cancel`, `cancel` | Cancel | video |
| `GET /api/streams/{id}/snapshots`, `snapshots` | Снимки с названиями офферов | failures; e2e |
| `POST .../snapshots/{snapshot_id}/restore`, `restore` | Загрузить снимок в черновик | failures |

## app/api/routes_tools.py

Инструменты сверх оригинала. Ни один не обходит обычный рабочий цикл: массовое добавление пишет только в черновики, публикация идёт через тот же Push, советник лишь предлагает цифры. Роутер подключён раньше `routes_streams.py`, иначе точные пути `/streams/drafts` и `/streams/push-many` перехватил бы шаблон `/streams/{stream_id}`.

| Маршрут и функция | Что делает | Тест |
|---|---|---|
| `GET /api/offers/{offer_id}/usage`, `offer_usage` | Где используется оффер и куда его можно добавить. В ответ добавлено поле `offer` с названием и состоянием из справочника. Для незнакомого оффера название заменяется на «Оффер #N» | tools: `TestOfferUsage` |
| `POST /api/offers/{offer_id}/bulk-add`, `bulk_add` | Добавить оффер в черновики нескольких потоков. Тело: `stream_ids`, от 1 до 100. В журнал пишется одна запись с итогом по каждому потоку | tools: `TestBulkOperations` |
| `GET /api/streams/drafts`, `drafts` | ID потоков с неопубликованными изменениями. Интерфейс этим методом пока не пользуется, он оставлен для скриптов | tools |
| `POST /api/streams/push-many`, `push_many` | Опубликовать несколько потоков подряд. Тело: `stream_ids`. Ответ: итог по каждому потоку и число опубликованных | tools: `TestBulkOperations` |
| `POST /api/campaigns/sync-all`, `sync_all` | Fetch по всем кампаниям трекера. Тело: `only_missing` | tools: `TestBulkOperations` |
| `GET /api/streams/{stream_id}/advice`, `advice` | Совет по долям потока. Параметры: `period` (`today`, `7d`, `30d`) и `metric` (`cr`, `epc`). Читает статистику кампании, собирает вход для `advisor.advise` по активным офферам и возвращает строки «сейчас, предлагается, почему». Ничего не меняет. Недоступные отчёты дают ответ `ready: false` с причиной, а не ошибку | tools: `TestAdvisorRoutes` |
| `POST /api/streams/{stream_id}/apply-shares`, `apply_shares` | Применить набор долей в черновик под замком кампании. Тело: `shares`, словарь «ID привязки: доля от 1 до 100». В ответ приходит свежий вид потока | tools: `TestAdvisorRoutes` |

Схемы запросов этого роутера лежат в том же файле: `StreamIdsRequest`, `ApplySharesRequest`, `SyncAllRequest`. Все ID в путях и телах ограничены значением 2 147 483 647.

## Остальные модули

| Файл | Что в нём |
|---|---|
| `app/main.py` | `create_app(settings, transport)` собирает приложение: создаёт клиента, сервисы и `CampaignLocks` при старте, подключает четыре группы маршрутов под проверкой токена, ставит заголовки безопасности, приводит любые ошибки к виду `{"error": {code, message, details}}`. Страницу `/docs` отдаёт сам: Swagger UI берётся из `app/web/static/vendor/swagger-ui`, адреса относительные. Настройка `ROOT_PATH` передаётся в `FastAPI(root_path=...)`. Параметр `transport` подменяет сеть эмулятором. Тесты: security, `TestSecurityHeaders`, `TestInputHardening`; meta, `TestNotFoundFormat`; tools, `TestOfflineDocs` |
| `app/config.py` | `Settings` читает окружение и `.env`. Адрес трекера нормализует: отрезает всё, начиная с сегмента `/admin` или `/admin_api`, параметры и `#`-маршрут, приводит схему к нижнему регистру, сохраняет нестандартный базовый путь. Пустые ID превращает в «не задано». `auth_tokens()` разбирает `ADROBOT_AUTH_TOKENS` в тройки «имя, токен, только чтение» и пропускает кривые записи и токены короче восьми символов. `auth_enabled` отвечает, включена ли защита. `campaign_admin_url()` строит ссылку View in KT. Тесты: security, `TestSettingsNormalization`; roles |
| `app/db.py` | `create_engine()` включает в SQLite внешние ключи, WAL и ожидание блокировки 5 секунд. `override_engine()` подменяет движок в тестах |
| `app/models.py` | Таблицы и свойство `StreamOffer.is_dirty`. `UTCDateTime` всегда возвращает время с поясом UTC, потому что SQLite пояс теряет |
| `app/schemas.py` | Схемы запросов. Лишние поля запрещены. Любой ID ограничен константой `MAX_ID`, равной 2 147 483 647. `CampaignCreateRequest` сливает `offer_id` и `offer_ids` в один список без дублей. Тесты: security, `test_huge_integers_are_client_errors_not_500` |
| `app/logging_conf.py` | `SecretRedactingFilter` заменяет на `***` известные секреты и пары вида `api-key=...` и `authorization: ...`. Чистит сообщение записи, текст исключения, traceback и стек. Traceback собирается заранее, потому что стандартный форматтер дописывает его уже после фильтров. Тесты: security, `TestLogRedaction` |
| `tests/fake_keitaro.py` | `FakeKeitaro` эмулирует Admin API на `httpx.MockTransport`. `fail_next()` включает сбои: код ответа, исключение, пропуск первых запросов, сбой после выполнения |
| `scripts/live_e2e.py` | Живой сквозной прогон, описан в [README](../README.md#проверка-своими-руками-за-10-минут) |
| `scripts/demo_server.py` | Приложение на эмуляторе без трекера и ключа. В эмуляторе две кампании, шесть активных офферов, один удалённый и правдоподобная статистика, одинаковая при каждом запуске, поэтому Stats, Trends и советник показывают данные |
