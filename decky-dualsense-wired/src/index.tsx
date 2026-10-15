import {
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
} from "@decky/api"
import { useEffect, useRef, useState } from "react";
import { FaPlaystation } from "react-icons/fa";

interface AudioStreamStatus {
  state: "starting" | "active" | "stopped" | "error";
  error: string | null;
  source: "system" | "controller";
  sent: number;
  underruns: number;
  dropped: number;
}

interface ControllerStatus {
  hidraw: string;
  name: string;
  mac: string;
  connection: "bluetooth" | "usb";
  // usb-gadget: virtual USB device with its own audio device; uhid: HID only
  mode?: "usb-gadget" | "uhid";
  state: "idle" | "starting" | "active" | "stopped" | "error";
  error: string | null;
  virtual_hidraw: string | null;
  game_audio_sink?: boolean;
  audio?: AudioStreamStatus | null;
  battery?: { capacity: number; status: string } | null;
}

const BATTERY_STATUS: Record<string, string> = {
  Discharging: "",
  Charging: ", заряжается",
  Full: ", заряжен",
};

function batteryText(battery: ControllerStatus["battery"]) {
  if (!battery) return "";
  return ` · 🔋 ${battery.capacity}%${BATTERY_STATUS[battery.status] ?? ""}`;
}

interface AudioOptions {
  enabled: boolean;      // all Deck audio to the controller
  game_audio: boolean;   // games' controller audio (speaker, HD haptics) to the controller
  speaker: boolean;
  haptics: boolean;
  volume: number;
  haptics_gain: number;
}

interface Status {
  enabled: boolean;
  uhid_available: boolean;
  is_root: boolean;
  audio: AudioOptions;
  controllers: ControllerStatus[];
}

const getStatus = callable<[], Status>("get_status");
const setEnabled = callable<[enabled: boolean], Status>("set_enabled");
const setAudio = callable<[options: Partial<AudioOptions>], Status>("set_audio");

const STATE_LABELS: Record<ControllerStatus["state"], string> = {
  idle: "Не эмулируется",
  starting: "Запуск…",
  active: "Эмулируется как проводной",
  stopped: "Остановлен",
  error: "Ошибка",
};

function AudioSection({ status, onStatus }: { status: Status; onStatus: (s: Status) => void }) {
  const [audio, setLocal] = useState<AudioOptions>(status.audio);
  const timer = useRef<number | undefined>(undefined);

  // Follow the backend unless a slider change is still waiting to be sent.
  useEffect(() => {
    if (timer.current === undefined) setLocal(status.audio);
  }, [status.audio]);

  const apply = (change: Partial<AudioOptions>, debounce = false) => {
    setLocal((a) => ({ ...a, ...change }));
    if (timer.current !== undefined) window.clearTimeout(timer.current);
    const send = async () => {
      timer.current = undefined;
      onStatus(await setAudio(change));
    };
    if (debounce) timer.current = window.setTimeout(send, 400);
    else send();
  };

  const streams = status.controllers.filter((c) => c.connection === "bluetooth" && c.audio);
  const bluetooth = status.controllers.some((c) => c.connection === "bluetooth");
  const streaming = audio.enabled || audio.game_audio;

  return (
    <PanelSection title="Звук и вибрация">
      <PanelSectionRow>
        <ToggleField
          label="Звук игр на контроллер"
          description="Динамик и HD-вибрация в играх с поддержкой DualSense, как по проводу"
          checked={audio.game_audio}
          disabled={!status.enabled}
          onChange={(value) => apply({ game_audio: value })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="Весь звук Deck на контроллер"
          description="Вместо звука игр: всё, что играет Deck, вибрация из звука"
          checked={audio.enabled}
          disabled={!status.enabled}
          onChange={(value) => apply({ enabled: value })}
        />
      </PanelSectionRow>
      {streaming && (
        <>
          <PanelSectionRow>
            <ToggleField label="Динамик контроллера" checked={audio.speaker}
              onChange={(value) => apply({ speaker: value })} />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField label="Громкость" value={audio.volume} min={0} max={100} step={5}
              showValue valueSuffix="%" disabled={!audio.speaker}
              onChange={(value) => apply({ volume: value }, true)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField label="Вибрация" checked={audio.haptics}
              onChange={(value) => apply({ haptics: value })} />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField label="Сила вибрации" value={audio.haptics_gain} min={0} max={300} step={10}
              showValue valueSuffix="%" disabled={!audio.haptics}
              onChange={(value) => apply({ haptics_gain: value }, true)} />
          </PanelSectionRow>
          {!bluetooth && (
            <PanelSectionRow>
              <Field label="Нет DualSense по Bluetooth" description="Звук передаётся только по Bluetooth" />
            </PanelSectionRow>
          )}
          {streams.map((c) => (
            <PanelSectionRow key={c.hidraw}>
              <Field label={`${c.audio!.source === "controller" ? "Звук игр" : "Звук Deck"}: ` +
                  (c.audio!.state === "error" ? "ошибка" : c.audio!.state)}
                description={c.audio!.error ??
                  `пакетов ${c.audio!.sent} · пропусков ${c.audio!.underruns}`} />
            </PanelSectionRow>
          ))}
        </>
      )}
    </PanelSection>
  );
}

function Content() {
  const [status, setStatus] = useState<Status | undefined>();

  useEffect(() => {
    let alive = true;
    const refresh = () => getStatus().then((s) => alive && setStatus(s)).catch(() => {});
    refresh();
    const timer = setInterval(refresh, 2000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  if (!status) {
    return <PanelSection><PanelSectionRow>Загрузка…</PanelSectionRow></PanelSection>;
  }

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ToggleField
            label="Эмуляция проводного подключения"
            description="DualSense по Bluetooth будет виден играм как подключённый по USB"
            checked={status.enabled}
            disabled={!status.uhid_available}
            onChange={async (value) => setStatus(await setEnabled(value))}
          />
        </PanelSectionRow>
        {!status.is_root && (
          <PanelSectionRow>
            <Field label="Бэкенд запущен не от root" description="Нужен флаг &quot;root&quot; в plugin.json" />
          </PanelSectionRow>
        )}
        {!status.uhid_available && (
          <PanelSectionRow>
            <Field label="/dev/uhid недоступен" description="Модуль ядра uhid не загружен" />
          </PanelSectionRow>
        )}
      </PanelSection>
      {/* Right under the main toggle: state and battery visible without scrolling. */}
      <PanelSection title="Контроллеры">
        {status.controllers.length === 0 ? (
          <PanelSectionRow>
            <Field label="DualSense не найден" />
          </PanelSectionRow>
        ) : (
          status.controllers.map((c) => (
            <PanelSectionRow key={c.hidraw}>
              <Field
                label={c.name}
                description={
                  c.state === "error"
                    ? `${STATE_LABELS[c.state]}: ${c.error}`
                    : `${STATE_LABELS[c.state]} · ${c.connection === "usb" ? "USB" : "Bluetooth"}` +
                      batteryText(c.battery) +
                      (c.mode === "usb-gadget" ? " · виртуальный USB со звуком" : "") +
                      ` · ${c.mac}`
                }
              />
            </PanelSectionRow>
          ))
        )}
      </PanelSection>
      <AudioSection status={status} onStatus={setStatus} />
      {/* Keeps the last rows clear of the Quick Access Menu's bottom bar. */}
      <div style={{ height: "48px" }} />
    </>
  );
};

export default definePlugin(() => {
  // Steam shows no battery for the virtual wired controller; warn about a low one ourselves.
  const onLowBattery = addEventListener<[name: string, capacity: number]>("low_battery", (name, capacity) =>
    toaster.toast({ title: "DualSense: низкий заряд", body: `${name}: ${capacity}%` }));
  return {
    onDismount() {
      removeEventListener("low_battery", onLowBattery);
    },
    // The name shown in various decky menus
    name: "DualSense Wired",
    // The element displayed at the top of your plugin's menu
    titleView: <div className={staticClasses.Title}>DualSense Wired</div>,
    // The content of your plugin's menu
    content: <Content />,
    // The icon displayed in the plugin list
    icon: <FaPlaystation />,
  };
});
