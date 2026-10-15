# DualSense Wired for Steam Deck

Плагин [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader), с которым DualSense,
подключённый к Steam Deck по Bluetooth, работает в играх как подключённый кабелем — включая звук
из динамика геймпада, HD-вибрацию и адаптивные триггеры в играх под Proton.

*A Decky Loader plugin that makes a Bluetooth DualSense work like a wired one on the Steam Deck,
including the controller speaker, HD haptics and adaptive triggers in Proton games.*

**Установка:** архив плагина — в [релизах](https://github.com/mrFunkill/decky-dualsense-wired/releases/latest),
инструкция — в [README плагина](decky-dualsense-wired/README.md#установка)
([English](decky-dualsense-wired/README.en.md#installation)).

| Папка | Что внутри |
|---|---|
| [`decky-dualsense-wired/`](decky-dualsense-wired/) | Плагин: описание, устройство, сборка и установка — в его [README](decky-dualsense-wired/README.md) |
| [`research/`](research/) | Исследование Bluetooth-протокола звука и вибрации DualSense и того, как игры под Wine находят звуковое устройство контроллера: [FINDINGS.md](research/FINDINGS.md), инструкция по захвату [CAPTURE.md](research/CAPTURE.md), скрипты разбора |

Протокол восстановлен по наблюдаемому поведению: захватам Bluetooth-трафика, документации ядра
Linux и Wine, экспериментам на Deck. Код сторонних программ (в том числе DSX) не использовался.
Исходные захваты `.pcap` и списки устройств автора в репозиторий не входят.

Лицензия — BSD 3-Clause (см. [`decky-dualsense-wired/LICENSE`](decky-dualsense-wired/LICENSE)).
