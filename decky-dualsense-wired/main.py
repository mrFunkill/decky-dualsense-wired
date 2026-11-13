import asyncio
import json
import os

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code repo
# and add the `decky-loader/plugin/imports` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky

from dualsense_bridge import Manager, remove_hide_rule
from dualsense_tester import Tester
from proton_prefixes import uninstall_all_detached
from updater import Updater
import report

SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
STEAM_ROOT = os.path.join(decky.DECKY_USER_HOME, ".local", "share", "Steam")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)


class Plugin:
    manager: Manager = None
    updater: Updater = None
    tester: Tester = None

    # Current state: global toggle and the list of detected BT DualSense controllers.
    async def get_status(self) -> dict:
        return self.manager.status()

    async def set_enabled(self, enabled: bool) -> dict:
        # Stopping bridges and streams joins threads; keep Decky's event loop free.
        await asyncio.to_thread(self.manager.set_enabled, enabled)
        save_settings({**load_settings(), "enabled": enabled})
        return self.manager.status()

    # Audio -> controller speaker/haptics (Bluetooth controllers).
    # options: any of game_audio (games' controller audio), enabled (all Deck audio),
    # speaker, haptics, volume (0..100), haptics_gain (0..300).
    async def set_audio(self, options: dict) -> dict:
        await asyncio.to_thread(self.manager.set_audio, options)
        settings = load_settings()
        save_settings({**settings, "audio": {**settings.get("audio", {}), **options}})
        return self.manager.status()

    # Updates from GitHub releases (the plugin is installed from a URL, so Decky does not update it).
    async def check_update(self, force: bool = False) -> dict:
        return await asyncio.to_thread(self.updater.check, force)

    async def install_update(self) -> dict:
        try:
            version = await asyncio.to_thread(self.updater.install)
            return {"ok": True, "version": version}
        except Exception as e:  # noqa: BLE001 - shown in the panel
            decky.logger.exception("Update failed")
            return {"ok": False, "error": str(e)}

    # Diagnostic report for bug reports: a zip in ~/Downloads (see report.py).
    async def collect_report(self) -> dict:
        try:
            path = await asyncio.to_thread(
                report.build, os.path.join(decky.DECKY_USER_HOME, "Downloads"), decky.DECKY_USER,
                decky.DECKY_PLUGIN_LOG_DIR, SETTINGS_FILE, self.manager.status(),
                {"Plugin": decky.DECKY_PLUGIN_VERSION, "Decky Loader": getattr(decky, "DECKY_VERSION", "?")},
                decky.DECKY_USER)
            decky.logger.info("Diagnostic report written to %s", path)
            return {"ok": True, "path": path, "size": os.path.getsize(path)}
        except Exception as e:  # noqa: BLE001 - shown in the panel
            decky.logger.exception("Collecting the diagnostic report failed")
            return {"ok": False, "error": str(e)}

    # Controller test page: works with the virtual controller like a game does.
    async def tester_start(self) -> dict:
        self.tester.start()
        return self.tester.state()

    async def tester_stop(self):
        await asyncio.to_thread(self.tester.stop)

    async def tester_state(self) -> dict:
        return self.tester.state()

    async def tester_action(self, action: str, args: dict) -> dict:
        """One test action; returns {"ok": True} or {"ok": False, "error": ...} for the page."""
        handlers = {
            "rumble": lambda: self.tester.rumble(args.get("left", 0), args.get("right", 0)),
            "trigger": lambda: self.tester.trigger(args.get("which", "both"), args["effect"]),
            "lightbar": lambda: self.tester.lightbar(args["color"]),
            "player_leds": lambda: self.tester.player_leds(args["mask"]),
            "mute_led": lambda: self.tester.mute_led(args["mode"]),
            "reset": lambda: self.tester.reset_outputs(),
            "play": lambda: self.tester.play(args["sound"]),
            "click_mode": lambda: self.tester.set_click_mode(args["on"]),
            "capture": lambda: self.tester.set_capture(args["on"]),
            "mic_record": lambda: self.tester.mic_record(),
            "select": lambda: self.tester.select(args.get("mac")),
        }
        try:
            await asyncio.to_thread(handlers[action])
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 - shown on the page
            decky.logger.info("Test action %s failed: %s", action, e)
            return {"ok": False, "error": str(e)}

    async def _main(self):
        self.updater = Updater("decky-dualsense-wired", decky.DECKY_PLUGIN_VERSION, decky.DECKY_PLUGIN_DIR, decky.logger)
        settings = load_settings()
        loop = asyncio.get_running_loop()

        def notify_low_battery(name, capacity):
            # Called from the manager thread; the frontend shows a Steam toast.
            asyncio.run_coroutine_threadsafe(decky.emit("low_battery", name, capacity), loop)

        self.manager = Manager(decky.logger, enabled=settings.get("enabled", True),
                               audio=settings.get("audio"), user=decky.DECKY_USER,
                               steam_root=STEAM_ROOT, notify_low_battery=notify_low_battery)
        self.manager.start()
        self.tester = Tester(self.manager, decky.logger, user=decky.DECKY_USER)
        decky.logger.info("DualSense Wired started")

    async def _unload(self):
        if self.tester:
            await asyncio.to_thread(self.tester.stop)
        if self.manager:
            self.manager.shutdown()
        decky.logger.info("DualSense Wired stopped")

    async def _uninstall(self):
        # _unload already removed it; this is a safety net so the BT controller is never left hidden.
        remove_hide_rule()
        # Take the helper service back out of the Proton prefixes (in a separate process:
        # Decky kills this one and deletes the plugin directory right after).
        uninstall_all_detached(STEAM_ROOT, decky.logger)
