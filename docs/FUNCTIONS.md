# Справочник функций

Что делает каждая публичная функция, зачем она нужна и каким тестом закреплена. Порядок идёт от ядра логики к маршрутам. Общая картина слоёв описана в [ARCHITECTURE.md](ARCHITECTURE.md).

Сокращения в колонке «Тест»:

| Сокращение | Файл |
|---|---|
| weights | `tests/test_weights.py` |
| video | `tests/test_editor_video_scenario.py` |
| failures | `tests/test_editor_failures.py` |
| creator | `tests/test_creator.py` |
| client | `tests/test_client.py` |
| meta | `tests/test_api_meta.py` |
| stats | `tests/test_stats.py` |
| security | `tests/test_security.py` |
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
| `StreamLocks.for_campaign(campaign_id)` | Выдаёт asyncio-замок кампании. Fetch и все правки её потоков идут по очереди, поэтому двойной клик и две вкладки не дают гонки пересчётов | автотеста нет |
| `load_campaign(session, campaign_id)` | Кампания с потоками и привязками одним запросом. Всегда перечитывает строки из базы (`populate_existing`). Нет кампании: `campaign_not_found`, 404 | video, failures |
| `load_stream(session, stream_id)` | Поток с привязками и кампанией. Нет потока: `stream_not_found`, 404 | video, failures |
| `import_campaigns(session, client)` | Кнопка «Обновить из Keitaro». Сводит шапки кампаний с трекером, потоки не трогает. Кампании, пропавшие из выдачи, помечает `is_deleted`, вернувшиеся возвращает в список. Возвращает `{total, created, gone}` | meta: `TestImport` |
| `get_or_import_campaign(session, client, keitaro_id)` | «Открыть по ID». Если кампании у нас нет, читает её из трекера и сохраняет шапку. Повторное открытие в трекер не ходит | meta: `TestOpenCampaign` |

### Fetch streams

| Имя | Что делает | Тест |
|---|---|---|
| `fetch_streams(session, client, campaign, discard_draft=False)` | Забирает потоки кампании одним запросом. Обновляет шапки потоков и вызывает `_sync_bindings` для офферов. Поток, которого нет в выдаче, помечает `is_deleted`, но не удаляет. Возвращает `{streams, gone_streams, archived_offers}` | video: шаги 0:36 и 4:30; failures: `TestConflicts` |
| `_sync_bindings(stream, kt_offers, discard_draft)` | Сводит привязки с трекером. Без черновика зеркалит Keitaro: новые офферы появляются со своей долей, пропавшие уходят в архив с 0 %. Доли не пересчитывает. При черновике работает трёхстороннее слияние: изменённые привязки сохраняют черновик, нетронутые принимают состояние трекера, опубликованная половина обновляется у всех. С `discard_draft=True` черновик выбрасывается | failures: `test_fetch_keeps_draft_and_merges_untouched_offers`, `test_fetch_with_discard_draft_mirrors_keitaro` |
| `_kt_offer_rows(row)` | Приводит офферы из ответа трекера к виду `{offer_id, share, state, binding_id}`. Состояние сводится к `active` или `disabled`, нечисловая доля становится 0 | video, failures |
| `_stream_summary(row)` | Короткая справка о потоке для показа: куда ведёт, какие фильтры, сколько лендингов. Логика редактора её не читает | автотеста нет |

### Правки черновика

| Имя | Что делает | Тест |
|---|---|---|
| `add_offer(session, dictionaries, stream, offer_id)` | Add. Отказывает, если оффер уже в потоке (`offer_already_in_stream`) или его нельзя использовать (`offer_not_usable`). Оффер из архива потока возвращает через `bring_back`. Новую привязку ставит в конец порядка активации и пересчитывает доли. При ошибке пересчёта транзакция откатывается, следов не остаётся | video: шаг 2:01; failures: `TestOfferValidation`, `test_add_when_everything_is_pinned_to_100` |
| `remove_offer(session, stream, binding_id)` | Remove. Опубликованный оффер получает состояние `removed`, долю 0 и теряет закрепление. Оффер, которого в трекере ещё не было, удаляется совсем. Остальные пересчитываются. Если пересчёт невозможен из-за закреплений, доли остаются как есть, а проблему покажет `stream_problems` | video: шаг 3:25; failures: `test_all_pinned_after_remove_blocks_push_with_hint` |
| `bring_back(session, dictionaries, stream, binding_id, check_offer=True)` | Bring back. Оффер снова активен, получает новый `sort_index`, то есть встаёт последним в порядке активации, доли пересчитываются. Перед этим оффер проверяется по справочнику | video: шаги 3:56 и 5:21 |
| `set_pin(session, stream, binding_id, pinned)` | Закрепляет или открепляет долю активного оффера. Цифр не меняет, поток не становится жёлтым | video: шаг 5:01; meta: `test_pin_alone_is_not_a_draft` |
| `set_share(session, stream, binding_id, value)` | Ручная доля: значение закрепляется, остальные незакреплённые пересчитываются. Ошибки калькулятора превращаются в `EditorError` с тем же кодом | failures: `TestWeightsGuards` |
| `recalculate(session, stream, drop_pins=False)` | «Пересчитать» делит остаток с учётом закреплений. С `drop_pins=True` это «Выровнять поровну»: закрепления снимаются. Без активных офферов отвечает `no_active_offers` | failures: `test_all_pinned_after_remove_blocks_push_with_hint`, `test_keitaro_sum_not_100_is_mirrored_with_warning_and_fixable` |
| `cancel(session, stream)` | Cancel. Привязки, которых никогда не было в трекере, удаляются. Оффер, возвращённый из архива, уходит обратно в архив. Остальные получают опубликованные состояние и долю. Закрепления не восстанавливаются. Возвращает число отменённых изменений | video: шаг 2:55, `test_cancel_restores_removed_offer_and_its_share`, `test_cancel_after_bring_back_returns_offer_to_archive` |
| `forget_binding(session, stream, binding_id)` | Убирает оффер из архива потока насовсем. Разрешено только для архивного оффера, которого уже нет в трекере, иначе `not_archived` | failures: `test_forget_archived_offer`, `test_forget_is_refused_for_live_offer` |

### Сравнение и публикация

| Имя | Что делает | Тест |
|---|---|---|
| `stream_is_dirty(stream)` | Есть ли у потока неопубликованные изменения. Флаг не хранится, он вычисляется по привязкам. Добавил оффер и сразу убрал: поток снова чистый | video: `test_remove_of_unpublished_offer_then_push_never_reaches_keitaro` |
| `stream_problems(stream)` | Что мешает публикации. Для потока без офферов и для пустого чистого потока возвращает пустой список. Иначе вызывает `check_distribution` | failures |
| `stream_diff(stream)` | Список «было в Keitaro, станет после Push». Типы изменений: `add`, `remove`, `share`, `state` | meta: `test_push_record_keeps_diff_and_published_set` |
| `push(session, client, dictionaries, stream, force=False, allow_empty=False)` | Push to KT. Шаги: поток редактируем и изменён, доли сходятся, новые офферы существуют, трекер не менялся с последней синхронизации, снимок, `PUT`, сверка ответа, запись опубликованного состояния. `force=True` публикует поверх чужих правок. `allow_empty=True` разрешает пустой поток. Коды отказов: `nothing_to_push`, `empty_stream`, `invalid_distribution`, `offer_not_usable`, `conflict`, `push_mismatch` | video: `test_push_sends_single_partial_put_with_only_offers`; failures: `TestConflicts`, `TestNetworkFailures` |
| `_push_payload(stream)` | Тело публикации: активные и выключенные офферы в порядке активации. Архивные не входят, так они исчезают из трекера | video |
| `_signature(rows)` | Отсортированный список троек `(offer_id, share, state)`. Сравнение подписей отвечает на вопрос, совпадают ли два набора офферов | failures |
| `list_snapshots(session, stream_id)` | Снимки потока, новые сверху | failures: `TestSnapshotsAndRollback` |
| `restore_snapshot(session, stream, snapshot_id)` | Откат. Снимок загружается в черновик, закрепления снимаются. Офферов, которых не было в снимке, ждёт архив либо удаление. В трекер состояние уйдёт только после Push | failures: `test_rollback_goes_through_draft_not_straight_to_keitaro` |
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
| `CampaignCreator.build_plans(session, request)` | Проверяет ввод и готовит планы, в трекер не пишет. Разбирает гео, проверяет офферы, группу, источник, домен и адрес редиректа. Собирает все ошибки и бросает одну `validation`. При `split_by_geo` делает план на каждую страну: к названию добавляется `[код]` либо подставляется `{geo}`. Параметры источника копирует в кампанию | creator: `TestHappyPath`, `TestValidationBeforeNetwork` |
| `CampaignCreator.request_hash(request)` | SHA-256 от тела запроса без поля `dry_run`. По нему отличают повтор от чужого запроса с тем же ключом | creator: `test_same_key_with_other_body_is_conflict` |
| `CampaignCreator.find_previous(session, key, request)` | Ищет прежний результат по ключу идемпотентности. Другое тело даёт `idempotency_mismatch`, незавершённый запрос даёт `in_progress`, готовый результат возвращается с `replayed: true` | creator: `TestIdempotencyAndCompensation` |
| `CampaignCreator.create(session, request, idempotency_key=None)` | Весь сценарий: повтор по ключу, планы, проверка названия, dry-run, занятие ключа, создание, сохранение результата. При неудаче ключ освобождается, чтобы попытку можно было повторить | creator; e2e |
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
| `DictionaryService.refresh_offers(session, force=False)` | Обновляет кэш офферов в таблице `offers`, если он старше `DICTIONARY_TTL_SECONDS`. Офферы, пропавшие из выдачи, помечает `is_missing`. Возвращает число офферов либо −1, если кэш свежий | meta: `TestOffersRefresh`; failures: `test_offer_invisible_to_api_key_is_shown_with_placeholder` |
| `DictionaryService.search_offers(session, query, limit=20, include_inactive=False)` | Поиск для автокомплита: префикс ID и подстрока названия без учёта регистра. Фильтрует в Python, потому что `LIKE` в SQLite не умеет кириллицу без регистра. Сначала точный ID, затем префикс ID, затем начало названия | meta: `TestOfferSearch` |
| `DictionaryService.get_offers_map(session, offer_ids)` | Офферы из кэша по списку ID, нужны названия в редакторе | video, failures |
| `DictionaryService.ensure_offers_usable(session, offer_ids)` | Проверка перед отправкой в трекер. Если оффер не найден, справочник перечитывается принудительно и проверка повторяется. Возвращает список проблем: «не найден» или «в состоянии deleted» | creator: `test_archived_offer_is_rejected`; failures: `TestOfferValidation` |
| `DictionaryService.get_lookups(force=False)` | Группы кампаний, активные источники и активные домены, кэш в памяти. Если список доменов пуст или запрещён, включает режим скрытых доменов и угадывает домен по кампаниям | meta: `TestLookups`; creator: `test_domain_hidden_from_api_key_is_inferred_from_campaigns` |
| `_infer_domain_id()` | Самый частый `domain_id` среди кампаний, видимых ключу | meta: `test_most_popular_domain_wins_the_inference` |
| `get_overrides`, `save_overrides` | Чтение и запись настроек из интерфейса, таблица `app_settings`. Пустое значение означает «вернуть автоматическое» | meta: `TestSettings` |
| `DictionaryService.resolve_defaults(session)` | Итоговые значения по умолчанию и источник каждого: «настройки», «.env», «авто» или «не задано». Внутри вызывает `get_lookups`, поэтому при устаревшем кэше обращается к трекеру | meta: `TestSettings`; creator |

## app/services/stats.py

| Имя | Что делает | Тест |
|---|---|---|
| `PERIODS` | Соответствие периода интерфейса пресету Keitaro: `today`, `7_days_ago`, `1_month_ago`. Незнакомый период заменяется неделей | stats: `TestPeriods` |
| `campaign_stats(client, keitaro_campaign_id, period="7d")` | Два отчёта на кампанию: итоги по парам поток и оффер, клики по дням для тренда. Ответ: `{available, period, days, offers, streams}`. Ключ оффера имеет вид `"<stream_id>:<offer_id>"`. Любая ошибка трекера даёт `available: false` с причиной, редактор при этом работает | stats: `TestTotals`, `TestTrend`, `TestReportsUnavailable` |

## app/services/audit.py

| Имя | Что делает | Тест |
|---|---|---|
| `current_actor` | Контекстная переменная с именем того, кто выполняет запрос. Её выставляет middleware из заголовка `X-AdRobot-User`: значение раскодируется, обрезается до 64 символов и не переходит на следующий запрос | security: `TestActorHeader` |
| `record(session, action, ...)` | Добавляет запись журнала без коммита и дублирует её в лог. Поля: действие, итог, кампания, поток, сводка, детали, ошибка, длительность | creator: `test_operations_log_records_success_and_failure` |
| `record_failure(session, action, error, ...)` | Откатывает незавершённую транзакцию и сохраняет запись об ошибке | failures: `test_failures_are_written_to_operations_log` |
| `Stopwatch` | Секундомер операции для блока `with`. Сейчас не используется: маршруты считают время сами внутри `audited` | автотеста нет |

## app/keitaro/client.py

Тонкая обёртка над Admin API. Бизнес-логики нет. Особенности самого API описаны в [KEITARO_API_NOTES.md](KEITARO_API_NOTES.md).

| Имя | Что делает | Тест |
|---|---|---|
| `KeitaroClient(base_url, api_key, timeout, max_retries, max_concurrency, user_agent, transport, backoff_base)` | Создаёт HTTP-клиент с заголовками `Api-Key` и `User-Agent`. Редиректам не следует, поэтому ключ не уйдёт на чужой хост. `transport` подменяет сеть в тестах. Семафор ограничивает число одновременных запросов | client: `TestConfiguration`, `TestConcurrencyLimit` |
| `configured`, `aclose()` | Заданы ли адрес и ключ. Закрытие соединений при остановке приложения | client: `TestConfiguration` |
| `_request(method, path, json, params, idempotent)` | Единая точка отправки. `GET`, `PUT` и `DELETE` повторяются при сетевой ошибке и ответах 429, 502, 503, 504. Пауза растёт вдвое, учитывает `Retry-After` и не превышает 15 секунд. `POST` не повторяется, текст ошибки предупреждает, что запрос мог выполниться. Без настроек сразу бросает `KeitaroNotConfiguredError` | client: `TestRetries`; failures: `test_put_applied_but_connection_lost_is_healed_by_retry`; creator: `test_timeout_on_create_is_not_retried_and_warns` |
| `_error_from_response(response, method, path)` | Разбирает ошибку любого формата: JSON `{"error": ...}`, JSON `{поле: [сообщения]}`, простой текст, страница Cloudflare. Возвращает исключение нужного класса с русским текстом | client: `TestErrorMapping`; failures: `test_cloudflare_block_is_explained` |
| `_parse(response, method, path)` | Успешный ответ превращает в JSON. Не-JSON при коде 200 считает ошибкой `keitaro_bad_response`: так выглядит неверный адрес трекера | client: `test_html_with_status_200_is_protocol_error`; failures: `test_html_instead_of_json` |
| `_expect_list`, `_expect_dict` | Проверяют форму ответа. Список из одного объекта принимается как объект: так отвечают некоторые методы трекера. Строки списка, не являющиеся объектами, отбрасываются | client: `TestErrorMapping` |
| `ping()` | Самый лёгкий запрос для проверки связи и ключа: группы кампаний | meta: `TestHealth` |
| `list_offers()`, `list_groups(type)`, `list_traffic_sources()`, `list_domains()` | Справочники целиком. У этих методов трекера нет ни фильтров, ни страниц | creator, failures |
| `list_campaigns()` | Все кампании ключа страницами по 500 через `limit` и `offset`. Если трекер игнорирует `limit` и отдаёт всё сразу, хватает одного запроса. Дубли по ID убираются | client: `TestCampaignPages` |
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
| `KeitaroForbiddenError` | `keitaro_forbidden` | 502 | 403 по правам, 402 по лицензии, блокировка Cloudflare |
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
| `get_client`, `get_dictionaries`, `get_creator`, `get_locks`, `get_app_settings` | Достают общие объекты из `app.state`. Они создаются один раз при старте | все тесты |
| `get_session` из `app/db.py` | Одна сессия базы на запрос. Коммит делает сервисный слой | все тесты |
| `require_token(request, authorization)` | Если задан `ADROBOT_AUTH_TOKEN`, пропускает только запросы с `Authorization: Bearer <токен>`. Сравнение идёт за постоянное время. Иначе отвечает 401 с заголовком `WWW-Authenticate`. Токен в строке запроса не принимается | security: `TestTokenMode` |
| `SessionDep`, `ClientDep`, `DictionariesDep`, `CreatorDep`, `LocksDep`, `SettingsDep` | Короткие имена зависимостей для сигнатур маршрутов | все тесты |

## app/api/routes_meta.py

| Маршрут и функция | Что делает | Тест |
|---|---|---|
| `GET /api/health`, `health` | Состояние базы, признак настройки и доступность трекера. Итог: `ok`, `setup_required`, `degraded` или `error`. С `deep=false` в трекер не ходит. Если таблиц нет, поле `database` подсказывает `alembic upgrade head` | meta: `TestHealth` |
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
| `audited(session, action, **ids)` | Контекстный менеджер. Оборачивает операцию записью в журнал при успехе и при ошибке, считает длительность, делает коммит | creator, failures |
| `DIRTY_BINDING` | SQL-двойник свойства `StreamOffer.is_dirty`. Находит кампании с черновиками одним запросом | meta: `test_only_drafts_shows_campaigns_with_unpublished_changes` |
| `campaign_view(session, campaign, dictionaries, settings)` | Представление кампании для интерфейса: шапка, гео с названиями, ссылки, потоки через `stream_view`. Адрес трекинг-домена берёт из `resolve_defaults` | video, creator |
| `GET /api/campaigns`, `list_campaigns` | Список из нашей базы: поиск по названию, alias и началу KT ID, фильтр по происхождению, только с черновиками, страницы | meta: `TestCampaignList` |
| `POST /api/campaigns/import`, `import_campaigns` | Кнопка «Обновить из Keitaro» | meta: `TestImport` |
| `POST /api/campaigns`, `create_campaign` | Создаватор. Читает заголовок `Idempotency-Key`. Проверка без создания пишется в журнал отдельным действием | creator; e2e |
| `POST /api/campaigns/open/{keitaro_id}`, `open_campaign` | Открыть кампанию трекера по ID | meta: `TestOpenCampaign` |
| `GET /api/campaigns/{id}`, `get_campaign` | Кампания с потоками из базы. Ради ссылки кампании вызывает `resolve_defaults`, а тот при устаревшем кэше справочников идёт в трекер. Если трекер в этот момент недоступен, ответом будет ошибка связи | video, creator; meta: `TestCampaignViewIsLocal` |
| `POST /api/campaigns/{id}/fetch`, `fetch_streams` | Fetch под замком кампании. После него обновляются названия офферов. Сбой справочника Fetch не роняет. Возвращает сводку и свежий вид кампании | video, failures |
| `GET /api/campaigns/{id}/stats`, `campaign_stats` | Статистика для колонок Stats и Trends. Следов в журнале и черновике не оставляет | stats |
| `DELETE /api/campaigns/{id}`, `archive_campaign` | Отправляет кампанию в архив трекера и скрывает её у нас. При сбое трекера кампания остаётся видимой | meta: `TestArchiveCampaign` |

## app/api/routes_streams.py

Все правки работают одинаково: замок кампании, загрузка потока, операция внутри `audited`, в ответ свежий вид потока. Интерфейс всегда показывает то, что лежит в базе.

| Маршрут и функция | Что делает | Тест |
|---|---|---|
| `GET /api/streams/{id}`, `get_stream` | Поток с офферами, долями, списком изменений и проблемами. Работает без обращения к трекеру | meta: `test_single_stream_view_already_works_offline` |
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

## Остальные модули

| Файл | Что в нём |
|---|---|
| `app/main.py` | `create_app(settings, transport)` собирает приложение: создаёт клиента и сервисы при старте, подключает маршруты под проверкой токена, ставит заголовки безопасности, приводит любые ошибки к виду `{"error": {code, message, details}}`. Параметр `transport` подменяет сеть эмулятором. Тесты: security, `TestSecurityHeaders`, `TestInputHardening`; meta, `TestNotFoundFormat` |
| `app/config.py` | `Settings` читает окружение и `.env`. Чистит адрес трекера от хвостов `/admin` и `/admin_api`, пустые ID превращает в «не задано». `campaign_admin_url()` строит ссылку View in KT. Тесты: security, `TestSettingsNormalization` |
| `app/db.py` | `create_engine()` включает в SQLite внешние ключи, WAL и ожидание блокировки 5 секунд. `override_engine()` подменяет движок в тестах |
| `app/models.py` | Таблицы и свойство `StreamOffer.is_dirty`. `UTCDateTime` всегда возвращает время с поясом UTC, потому что SQLite пояс теряет |
| `app/schemas.py` | Схемы запросов. Лишние поля запрещены. `CampaignCreateRequest` сливает `offer_id` и `offer_ids` в один список без дублей |
| `app/logging_conf.py` | `SecretRedactingFilter` заменяет на `***` известные секреты и пары вида `api-key=...` и `authorization: ...` в сообщении записи лога. Тесты: security, `TestLogRedaction` |
| `tests/fake_keitaro.py` | `FakeKeitaro` эмулирует Admin API на `httpx.MockTransport`. `fail_next()` включает сбои: код ответа, исключение, пропуск первых запросов, сбой после выполнения |
| `scripts/live_e2e.py` | Живой сквозной прогон, описан в [README](../README.md#проверка-своими-руками-за-10-минут) |
| `scripts/demo_server.py` | Приложение на эмуляторе без трекера и ключа |
