// UI strings in English and Russian. English is the default; Russian only when the Steam
// client's UI language is Russian.

const en = {
  loading: "Loading…",
  emulation: "Wired connection emulation",
  emulationDescription: "Games see a Bluetooth DualSense as connected over USB",
  notRoot: "Backend is not running as root",
  notRootDescription: "plugin.json needs the \"root\" flag",
  noUhid: "/dev/uhid is not available",
  noUhidDescription: "The uhid kernel module is not loaded",

  controllers: "Controllers",
  noController: "No DualSense found",
  state: {
    idle: "Not emulated",
    starting: "Starting…",
    active: "Emulated as wired",
    stopped: "Stopped",
    error: "Error",
  },
  virtualUsb: "virtual USB with audio",
  charging: ", charging",
  full: ", charged",

  audio: "Audio and haptics",
  gameAudio: "Game audio to controller",
  gameAudioDescription: "Speaker and HD haptics in games with DualSense support, like over a cable",
  deckAudio: "All Deck audio to controller",
  deckAudioDescription: "Instead of game audio: everything the Deck plays, haptics from the sound",
  speaker: "Controller speaker",
  volume: "Volume",
  haptics: "Haptics",
  hapticsGain: "Haptics strength",
  latency: "Controller audio buffer",
  latencyDescription: "Less: less delay. More: fewer dropouts when Bluetooth is busy",
  ms: " ms",
  noBluetooth: "No DualSense over Bluetooth",
  noBluetoothDescription: "Audio is only sent over Bluetooth",
  streamGame: "Game audio",
  streamDeck: "Deck audio",
  streamState: {
    starting: "starting",
    active: "active",
    stopped: "stopped",
    error: "error",
  },
  streamStats: (sent: number, underruns: number) => `packets ${sent} · gaps ${underruns}`,

  lowBattery: "DualSense: low battery",

  updates: "Updates",
  version: (v: string) => `Version ${v}`,
  checkButton: "Check for updates",
  checking: "Checking…",
  checkFailed: "Could not check",
  upToDate: "This is the latest version",
  updateAvailable: (v: string) => `Update available: v${v}`,
  updateDescription: "Downloads the release from GitHub. The controller reconnects: don't update during a game.",
  updateButton: (v: string) => `Update to v${v}`,
  updating: "Updating…",
  updateFailed: "Update failed",
  updateInstalled: "Installed. If the panel still shows the old version, restart Steam",
};

type Strings = typeof en;

const ru: Strings = {
  loading: "Загрузка…",
  emulation: "Эмуляция проводного подключения",
  emulationDescription: "DualSense по Bluetooth будет виден играм как подключённый по USB",
  notRoot: "Бэкенд запущен не от root",
  notRootDescription: "Нужен флаг \"root\" в plugin.json",
  noUhid: "/dev/uhid недоступен",
  noUhidDescription: "Модуль ядра uhid не загружен",

  controllers: "Контроллеры",
  noController: "DualSense не найден",
  state: {
    idle: "Не эмулируется",
    starting: "Запуск…",
    active: "Эмулируется как проводной",
    stopped: "Остановлен",
    error: "Ошибка",
  },
  virtualUsb: "виртуальный USB со звуком",
  charging: ", заряжается",
  full: ", заряжен",

  audio: "Звук и вибрация",
  gameAudio: "Звук игр на контроллер",
  gameAudioDescription: "Динамик и HD-вибрация в играх с поддержкой DualSense, как по проводу",
  deckAudio: "Весь звук Deck на контроллер",
  deckAudioDescription: "Вместо звука игр: всё, что играет Deck, вибрация из звука",
  speaker: "Динамик контроллера",
  volume: "Громкость",
  haptics: "Вибрация",
  hapticsGain: "Сила вибрации",
  latency: "Буфер звука в контроллере",
  latencyDescription: "Меньше — меньше задержка, больше — меньше прерываний при загруженном Bluetooth",
  ms: " мс",
  noBluetooth: "Нет DualSense по Bluetooth",
  noBluetoothDescription: "Звук передаётся только по Bluetooth",
  streamGame: "Звук игр",
  streamDeck: "Звук Deck",
  streamState: {
    starting: "запуск",
    active: "идёт",
    stopped: "остановлен",
    error: "ошибка",
  },
  streamStats: (sent: number, underruns: number) => `пакетов ${sent} · пропусков ${underruns}`,

  lowBattery: "DualSense: низкий заряд",

  updates: "Обновления",
  version: (v: string) => `Версия ${v}`,
  checkButton: "Проверить обновления",
  checking: "Проверка…",
  checkFailed: "Не удалось проверить",
  upToDate: "Установлена последняя версия",
  updateAvailable: (v: string) => `Доступно обновление: v${v}`,
  updateDescription: "Загрузит релиз с GitHub. Контроллер переподключится: не обновляйте во время игры.",
  updateButton: (v: string) => `Обновить до v${v}`,
  updating: "Обновление…",
  updateFailed: "Не удалось обновить",
  updateInstalled: "Установлено. Если панель показывает старую версию, перезапустите Steam",
};

// Steam client's localization; the first locale is the UI language ("russian", "english", ...).
type SteamLocalization = { m_rgLocalesToUse?: string[] };

function uiLanguage(): string {
  const manager = (window as unknown as { LocalizationManager?: SteamLocalization }).LocalizationManager;
  // Unknown → English, even if the system locale says otherwise: it is the Steam UI that counts.
  return (manager?.m_rgLocalesToUse?.[0] || "english").toLowerCase();
}

/** Strings for the current Steam UI language (read on each call: the language can change). */
export function t(): Strings {
  const lang = uiLanguage();
  return lang === "russian" || lang.startsWith("ru") ? ru : en;
}
