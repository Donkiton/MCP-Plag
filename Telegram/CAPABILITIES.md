# Возможности @tg и сравнение с chigwell

Сравнение выполнено по исходникам, без входа в реальный Telegram-аккаунт. Оно показывает объявленные команды и различия контрактов, а не подтверждает все операции на аккаунте владельца.

## Зафиксированные источники и количество

| Набор | Ревизия / состояние | Определений tools |
| --- | --- | ---: |
| [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp/tree/686d58793455a5620477a2a588660102c499ac39/telegram_mcp/tools) | `686d58793455a5620477a2a588660102c499ac39` | **147** |
| [mcp-telegram-cloud](https://github.com/mcp-telegram/mcp-telegram-cloud/tree/d2548becf2a124481313635b6c6d888cd3903fe3/src/tools) | База `d2548becf2a124481313635b6c6d888cd3903fe3` | **184** |
| Локальный @tg | Та же база + переименование аккаунтов + три локальных инструмента псевдонимов | **188** |

Числа получены подсчётом `@mcp.tool` в десяти модулях chigwell и уникальных `name: "telegram-…"` в модулях @tg. README upstream могут содержать прежние округлённые числа. Из 188 определений часть включается переменными окружения: 2 команды групповых звонков, 2 команды быстрых ответов и 10 команд Stars/платных реакций. При выключении всех этих категорий остаются 174; при включённых звонках и быстрых ответах, но выключенных Stars — 178. Фактически обнаруженный каталог развёрнутого сервера нужно проверять отдельно.

Наличие tool в каталоге не гарантирует доступность действия: Telegram может требовать права администратора, Premium или доступ к конкретному чату. Опасные действия дополнительно зависят от серверных разрешений. Локальные добавления должны быть включены в `server/src/tools.ts` и проверены в `tools/list` перед объявлением их установленными.

## Как нормализованы дубли

Префикс `telegram-` и замена `_`/`-` дают 42 совпадения имён между chigwell и текущим @tg, включая три новых псевдонима. Это только механическая сверка. Семантическое пересечение шире: одна команда с параметром часто заменяет две именованные команды. Параметры, формат результатов и ограничения проверяются отдельно.

| Функции chigwell | Представление в @tg | Оценка и отличия |
| --- | --- | --- |
| `get_chats`, `list_chats` | `telegram-list-chats` | Один базовый список диалогов; отдельные имена не складываются в новые функции. |
| `get_chat`, `get_full_chat` | `telegram-get-chat-info` | Общие сведения о чате есть. Сырые полные структуры Telethon и все дополнительные поля не обещаются. |
| `get_messages`, `list_messages`, `get_history` | `telegram-read-messages` | Объединённое чтение с лимитом, `offsetId` и датами. Выбор произвольного набора message IDs и специальный контекст — отдельные пробелы ниже. |
| `search_messages`, `search_global` | Одноимённые команды с префиксом `telegram-` | Базовый поиск по чату/глобально есть. Параметры следует брать из схемы @tg. |
| `search_public_chats`, `resolve_username` | `telegram-search-chats`, `telegram-get-chat-info` | Поиск по имени/username и сведения доступны. Это не отдельный гарантированно эквивалентный raw-resolver. |
| `send_message`, `reply_to_message` | `telegram-send-message`, параметр `replyTo` | Ответ не требует второго инструмента. Есть `topicId`, цитата `quoteText`, обычные `md`/`html` и поддерживаемый Premium effect. |
| `forward_message`, `forward_messages` | `telegram-forward-message`, массив `messageIds` | Одна и пакетная пересылка объединены. |
| `edit_message`, `delete_message` | `telegram-edit-message`, `telegram-delete-message` | Основные действия есть; массовая очистка истории отдельно не обещается. |
| `send_scheduled_message`, `get_scheduled_messages`, `delete_scheduled_message` | `telegram-send-scheduled`, `telegram-get-scheduled`, `telegram-delete-scheduled` | Различаются названия, а не три новых отсутствующих возможности. |
| `pin_message`, `unpin_message` | Одноимённые команды @tg | Основные действия есть. Получить все закреплённые / снять все одним вызовом — отдельные пробелы. |
| `send_reaction`, `remove_reaction`, `get_message_reactions` | `telegram-send-reaction`, `telegram-get-reactions` | Удаление реакции — отсутствие `emoji` в команде отправки; отдельный tool не нужен. |
| `create_poll`, `vote_poll` | Одноимённые команды @tg | Дополнительно cloud предоставляет результаты, голосовавших и закрытие опроса. |
| `save_draft`, `get_drafts`, `clear_draft` | `telegram-save-draft`, `telegram-get-drafts`, `telegram-clear-drafts` | Основные операции есть. Перед очисткой нужно проверить, что выбранная область совпадает с намерением пользователя: единственное/множественное имя не доказывает одинаковый охват. |
| `mark_as_read`, `send_typing` | Одноимённые команды @tg | Есть; это изменения состояния, хотя текст сообщения не отправляется. |
| `mute_chat`, `unmute_chat` | `telegram-mute-chat`, `muted=true/false` | Две операции объединены параметром. |
| `archive_chat`, `unarchive_chat` | `telegram-archive-chat`, параметр архивации | Две операции объединены. |
| `subscribe_public_channel`, `join_chat_by_link`, `import_chat_invite` | `telegram-join-chat` | Один вход по username, публичной или пригласительной ссылке. |
| `leave_chat` | `telegram-leave-group` | Команда cloud документирована для группы или канала. |
| `get_invite_link`, `export_chat_invite` | `telegram-get-invite-links`, `telegram-create-invite-link` | Есть чтение и создание ссылок; отзыв также доступен. Не все raw-флаги Telethon эквивалентны. |
| `create_group`, `invite_to_group` | Одноимённые команды @tg | Есть. Создание именно канала и преобразование группы не включаются сюда автоматически. |
| `edit_chat_title`, `edit_chat_about` | `telegram-edit-group`, поля `title`/`description` | Объединены. Фото группы этим инструментом не меняется. |
| `get_participants` | `telegram-get-chat-members` | Получение участников есть, с собственными лимитами/правами. |
| `promote_admin`, `demote_admin` | `telegram-set-admin`, `telegram-remove-admin` | Основные назначения/снятия есть. Детальное редактирование всех прав не считается полностью покрытым. |
| `ban_user`, `unban_user`, `remove_user` | `telegram-ban-user`, `telegram-unban-user`, `telegram-kick-user` | Базовая модерация есть. |
| `set_default_chat_permissions`, `toggle_slow_mode`, `enable_forum_topics` | `telegram-set-chat-permissions`, `telegram-set-slow-mode`, `telegram-toggle-forum-mode` | Нормализованы по действию, а не буквальному имени. |
| `list_topics`, `create_forum_topic`, `edit_forum_topic`, `delete_forum_topic` | `telegram-list-topics`, `telegram-create-topic`, `telegram-edit-topic`, `telegram-delete-topic` | Основные темы есть; cloud также читает сообщения конкретной темы. |
| `get_recent_actions` | `telegram-get-admin-log` | Журнал административных действий есть. |
| `list_contacts`, `get_contact_ids` | `telegram-get-contacts` | Список уже содержит ID. Отдельный список ID не добавляет новую сетевую возможность. |
| `search_contacts` | Получение `telegram-get-contacts` и фильтрация клиентом | Только композиция для полученного списка; исчерпывающий серверный поиск контактов с гарантированной полнотой не заявлен. |
| `add_contact`, `block_user`, `unblock_user`, `send_contact` | Одноимённые команды @tg | Есть. Импорт/экспорт адресной книги и удаление контакта — отдельные пробелы. |
| `set_contact_alias`, `list_contact_aliases`, `delete_contact_alias` | Три новых `telegram-…-contact-alias…` команды | Реализована локальная точная память имён. Она не подменяет адресата автоматически и не копирует fuzzy-поведение chigwell. |
| `list_folders`, `get_folder` | `telegram-get-chat-folders` | Список/сведения о папках есть. Полный снимок и лимиты не добавлены автоматически. |
| `create_folder`, `update_folder`, `delete_folder`, `reorder_folders` | `telegram-create-folder`, `telegram-edit-folder`, `telegram-delete-folder`, `telegram-reorder-folders` | Основные изменения есть. Добавление/удаление одного чата через read-modify-write требует отдельного учёта конкурирующих изменений. |
| `send_file`, `send_album`, `send_voice`, `send_sticker`, `download_media` | Одноимённые команды @tg | Возможности есть, но локальный путь другого компьютера не является путём VPS. Cloud использует upload ID/HTTPS-источник и ограниченное приватное скачивание. |
| `get_sticker_sets` | `telegram-get-installed-stickers`, `telegram-get-sticker-set`, `telegram-search-sticker-sets` | Поддержаны основные наборы/поиск. Это не равнозначно визуальному превью custom emoji. |
| `transcribe_voice` | `telegram-transcribe-audio`, `telegram-get-transcription` | В cloud используется поддерживаемая Telegram-транскрипция с её ограничениями. Локальный Whisper/API-провайдер из chigwell не перенесён. |
| `list_inline_buttons`, `press_inline_button` | `telegram-get-message-buttons`, `telegram-press-button` | Получение кнопок и callback есть. Cloud также поддерживает inline query/send. |
| `get_message_link`, `get_message_read_by` | `telegram-get-message-link`, `telegram-get-message-read-participants` | Есть с ограничениями Telegram на доступность read receipts. |
| `get_me`, `get_full_user` | `telegram-status`, `telegram-get-profile` | Основная идентификация/профиль есть. Не обещаются все сырые поля FullUser или отдельный real-time статус. |
| `update_profile`, `set_profile_photo`, `delete_profile_photo` | Одноимённые команды @tg | Есть. История/листы пользовательских фото требуют отдельной сверки. |
| `set_privacy_settings` | `telegram-set-privacy` | Изменение поддерживаемых правил есть; полный getter правил с контрактом chigwell не перенесён. |
| `list_accounts` | `telegram-accounts-list` и пять команд управления | В @tg добавлены браузерное подключение, имена основного/вторичных аккаунтов и явная маршрутизация каждого вызова. |

Исходники сопоставления: [чтение](server/src/tools/read.ts), [сообщения](server/src/tools/messaging.ts), [чаты](server/src/tools/chats.ts), [профиль](server/src/tools/profile.ts), [папки/стикеры/события](server/src/tools/misc.ts), [медиа](server/src/tools/uploads.ts), [аккаунты](server/src/tools/accounts.ts). В таблице «есть» означает наличие реализации и схемы в коде; реальные права, сеть и результат проверяются после входа.

## Полезные уникальные пробелы chigwell

Эти функции не следует рекламировать как реализованные в @tg. При расширении можно добавить удобные команды поверх того же GramJS, сохранив одну сессию на аккаунт.

| Возможность | Конкретные команды chigwell / отличие | Состояние @tg |
| --- | --- | --- |
| Ожидание законченной серии сообщений и постоянный входящий feed | `wait_for_events`, `get_messages_after`, `wait_for_new_message`, `wait_for_settled_message`, `enable_incoming_feed`, `disable_incoming_feed`, `incoming_feed_status` | Есть низкоуровневые `get-state`/`get-updates`/`get-channel-updates`. Они не заменяют готовое ожидание с debounce или постоянный монитор. |
| Контекст сообщения и явные наборы ID | `get_message_context`, расширенные `get_messages`, `get_messages_after` | Нет отдельного готового контекстного/пакетного интерфейса chigwell. Чтение по странице и ответы доступны. |
| Полный экспорт непрочитанных | `export_unread_messages` | Возможна последовательность get-unread → read-messages; единой экспортной операции нет. |
| Работа с закреплёнными пачкой | `get_pinned_messages`, `unpin_all_messages` | Одиночные pin/unpin есть; эти удобные batch-инструменты не перенесены. |
| Массовое удаление и очистка истории | `delete_messages_bulk`, `delete_chat_history` | Не считать одиночный delete-message эквивалентом. Нужны отдельные проверяемые границы действия. |
| Отправитель от имени канала | `get_send_as`, `set_default_send_as` | Выбор Telegram-аккаунта реализован, но это другая функция. Готового интерфейса send-as из chigwell нет. |
| Богатое форматирование | `parse_mode=rich/rich_markdown/rich_html`, таблицы/формулы/разворачиваемые блоки и date-chip у chigwell | @tg объявляет обычные md/html; данный серверный rich-конвейер не перенесён. |
| Контактные связи и импорт/экспорт | `get_common_chats`, `get_contact_chats`, `get_last_interaction`, `get_direct_chat_by_contact`, `delete_contact`, `import_contacts`, `export_contacts`, `get_blocked_users` | Часть можно составить из чтения, но специальных полноценных контрактов этих команд нет. |
| Фото/документы с визуальной подготовкой | `list_photos`, `open_photo`, `get_photo_sheet`, `inspect_document`, `get_media_info` | Скачивание есть. Контактный лист, рендер документа и данные этих helper-команд не перенесены. |
| Custom emoji с превью | `get_custom_emoji_packs`, `get_custom_emoji_pack`, `preview_custom_emoji` | Отдельных инструментов нет. Обычные stickers не объявляются полным эквивалентом. |
| GIF-поиск и удобная отправка | `get_gif_search`, `send_gif` | Возможна отправка допустимого медиа-источника, но готовая пара поиска/выбора GIF не реализована. |
| Специальные папки | `get_folder_limits`, `get_folder_snapshot`, `get_chatlist_invite`, `add_chat_to_folder`, `remove_chat_from_folder` | Базовые папки есть; эти helpers и их атомарность не перенесены. |
| Расширенное управление группой | `create_channel`, `upgrade_group_to_supergroup`, `edit_chat_photo`, `delete_chat_photo`, `edit_admin_rights`, `get_admins`, `get_member_admin_status`, `get_banned_users`, `list_forum_topic_icons` | Есть базовые группы/права/темы, но эти отдельные интерфейсы отсутствуют или покрыты лишь частично. |
| Дополнительные сведения профиля/бота | `get_privacy_settings`, `get_bot_info`, `set_bot_commands`, `get_user_photos`, `get_user_status` | Общие профиль/фото/privacy-настройки есть. Полные перечисленные инструменты не перенесены. |

## Что cloud предоставляет дополнительно

У cloud есть отдельные категории Stories, Business, boosts и статистики, глобальной приватности, fact-check, переводов, location/venue/dice, быстрых ответов, групповых звонков и Stars. В наборе chigwell 147 нет симметричного одноимённого каталога этих категорий. Возможности остаются ограничены Telegram, аккаунтом и серверными feature-флагами; платные действия нельзя считать включёнными только по наличию исходника.

## Новая локальная память контактов

`telegram-set-contact-alias` принимает точное имя, явный `@username` либо числовой ID и необязательный `replace`. `telegram-list-contact-aliases` читает сохранённое, при необходимости по точному имени. `telegram-delete-contact-alias` забывает только выбранную запись.

- Unicode NFKC, регистр и последовательности пробелов нормализуются. «Лена» и «Леня» остаются разными именами; fuzzy matching нет.
- Переназначение существующего имени требует `replace=true`. Целью нельзя сделать другое свободное имя, URL, телефон или ещё один псевдоним.
- Данные разделены по владельцу, account ID и конкретной привязке аккаунта. Переименования аккаунта не меняют запись; замена привязки не переносит старые имена на нового отправителя.
- Изменения выполняются только в локальной SQLite, параметризованными запросами. Telegram-контакты не меняются, сетевых запросов и отправок нет.
- Это **lookup**, а не автоматическая подстановка в `chatId`. Клиент сначала читает точную запись, проверяет адресата при необходимости и затем явно передаёт полученный ID/username нужному инструменту.
- Сохранённый username не является доказательством текущего владельца имени. Само сохранение не проверяет существование адресата. Для одного аккаунта установлен предел 500 псевдонимов.

Реализация написана отдельно для SQLite/GramJS-модели @tg; код Telethon и файловый формат хранилища chigwell не переносятся. [Модуль](server/src/tools/tg-contact-aliases.ts) и [тесты](server/src/__tests__/tg-contact-aliases.test.ts) проверяют точность, Unicode, недопустимые цели, запрет неявного переназначения, SQL-параметры, разделение владельцев/аккаунтов/привязок и отсутствие Telegram-вызовов.
