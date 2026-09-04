# Грабли окружения и техника (пополняется каждым проектом)

## Техника длинных команд (сборки, тесты) — в КАЖДЫЙ бриф сабагенту
- Вывод в файл, один вызов, итог сразу:
  `cmd > /tmp/x.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/x.log` — таймаут щедрый
  (600000 мс). Иначе сабагент виснет на потоке вывода (watchdog killed).
- Сабагент НЕ завершает ход, пока команда идёт. Формулировка в бриф:
  «дождись итога, не завершай ход с "тесты идут"». Заснувшего — будить
  сообщением (контекст сохраняется), не пересоздавать.

## Shell (zsh, macOS)
- `log` — встроенная команда zsh! Системный журнал вызывать ТОЛЬКО как
  `/usr/bin/log`. Симптом: «too many arguments» или тихие пустые выводы.
- cwd между вызовами может сбрасываться — абсолютные пути всегда.
- GUI-приложения не читают PATH из ~/.zshrc — абсолютные пути к тулчейну.

## Xcode / симуляторы
- Первый прогон на холодном симуляторе — 10–15 минут; таймауты щедро.
- Флейк «Timed out waiting for AX loaded notification» (XCTDaemonErrorDomain 18):
  simctl shutdown → erase → boot → bootstatus -b, повторить один раз.
- Имя симулятора не хардкодить: `xcrun simctl list devices available`.
- XcodeGen через симлинк теряет SettingPresets («No "base" settings found»,
  цели без SDKROOT) → симлинк ~/.local/share/xcodegen на share из дистрибутива.
- UIPasteboard-тесты на симуляторе флейкают (round-trip → nil) — retry-обвязка.
- Device-сборка без подписи для проверки компиляции:
  `-destination generic/platform=iOS CODE_SIGNING_ALLOWED=NO`.
- Установка на устройство: `-allowProvisioningUpdates
  -allowProvisioningDeviceRegistration DEVELOPMENT_TEAM=<ID>` + `xcrun devicectl
  device install app` + `device process launch`. Developer Mode на устройстве
  включает только человек (Settings → Privacy & Security), потом перезагрузка.

## SwiftData / CloudKit
- Правила модели для CloudKit: inline-дефолты, Optional-связи, без .unique,
  порядок только явным sortIndex. Нарушение всплывает поздно.
- CloudKit без entitlement НЕ бросает из init — роняет процесс NSException'ом
  на фоновой очереди; на macOS проверять entitlement заранее
  (SecTaskCopyValueForEntitlement) и фолбэчить на локальный store ТОГО ЖЕ файла.
- macOS ad-hoc подпись несовместима с iCloud-entitlements (restricted).
- Push-ключ различается: iOS `aps-environment`, macOS
  `com.apple.developer.aps-environment` — отдельные entitlements-файлы.
- Доставка dev-пушей на Mac ненадёжна (инфраструктура Apple); рабочие каналы —
  импорт при запуске и при активации приложения.
- CloudKit-импорт минует ModelContext.didSave → SwiftData не мёржит
  в mainContext → UI устаревает до перезапуска. Лечение: наблюдатель
  .NSPersistentStoreRemoteChange + дебаунс + refetch моделей (rollback()
  и refreshAllObjects() НЕ помогают — row cache).
- External storage BLOB'ов: файлы в .default_SUPPORT/_EXTERNAL_DATA,
  в колонке — ссылка 0x02+UUID.

## Права и безопасность агента
- Агент не может выдать права сам себе (запись в .claude/settings.json
  блокируется) — allowlist применяет человек на фазе 2.
- «Красные» операции (rm по переменной пути, скриншот экрана, запуск с флагом
  очистки данных) требуют подтверждения всегда — не планировать в автономные фазы.
- Запуск приложения с флагом, стирающим данные (-UITestMode), классификатор
  может заблокировать — обходить не пытаться, менять план.

## Реальные данные
- Синтетика систематически «оптимистичнее» реальности (камера: шум сенсора
  и фактура бумаги ≈ ×1.5–2 к плотности байт). Калибровать fixtures по числам
  из реальных образцов (плотность байт/Мп в ±25%).
- Попиксельный шум не выживает даунскейл — усредняется; нужен многослойный
  (пиксель/средние блоки/крупные блоки).

## Каталог интерфейсных компонентов (MCP shadcn)

- **Поиск без проекта.** Без `components.json` поиск падает с «No registries are
  configured». Лечится указанием реестра прямо в запросе (`registries: ["@shadcn"]`) —
  тогда работает и на фазе проектирования, когда проекта ещё нет.
- **Команда установки в выдаче поиска печатается как `[object Promise]`**
  (проверено 2026-09-04). Скопировать её нельзя — запрашивать отдельным вызовом
  `get_add_command_for_items`.
- **MCP ничего не устанавливает.** Он возвращает команду; выполняет её агент
  в терминале. Значит установка компонента — обычная задача фазы 3 со своим
  тестом, ревью и коммитом, а не побочный эффект поиска.
