import asyncio
import json
import os

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code repo
# and add the `decky-loader/plugin/imports` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky

from dualsense_bridge import Manager, remove_hide_rule
from proton_prefixes import uninstall_all_detached

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

    async def _main(self):
        settings = load_settings()
        loop = asyncio.get_running_loop()

        def notify_low_battery(name, capacity):
            # Called from the manager thread; the frontend shows a Steam toast.
            asyncio.run_coroutine_threadsafe(decky.emit("low_battery", name, capacity), loop)

        self.manager = Manager(decky.logger, enabled=settings.get("enabled", True),
                               audio=settings.get("audio"), user=decky.DECKY_USER,
                               steam_root=STEAM_ROOT, notify_low_battery=notify_low_battery)
        self.manager.start()
        decky.logger.info("DualSense Wired started")

    async def _unload(self):
        if self.manager:
            self.manager.shutdown()
        decky.logger.info("DualSense Wired stopped")

    async def _uninstall(self):
        # _unload already removed it; this is a safety net so the BT controller is never left hidden.
        remove_hide_rule()
        # Take the helper service back out of the Proton prefixes (in a separate process:
        # Decky kills this one and deletes the plugin directory right after).
        uninstall_all_detached(STEAM_ROOT, decky.logger)
