import {
  ButtonItem,
  Field,
  Navigation,
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
  routerHook,
  toaster,
} from "@decky/api"
import { useEffect, useRef, useState } from "react";
import { FaPlaystation } from "react-icons/fa";
import { t } from "./i18n";
import { TEST_ROUTE, TestPage } from "./TestPage";

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

function batteryText(battery: ControllerStatus["battery"]) {
  if (!battery) return "";
  const status = battery.status === "Charging" ? t().charging : battery.status === "Full" ? t().full : "";
  return ` · 🔋 ${battery.capacity}%${status}`;
}

interface AudioOptions {
  enabled: boolean;      // all Deck audio to the controller
  game_audio: boolean;   // games' controller audio (speaker, HD haptics) to the controller
  speaker: boolean;
  haptics: boolean;
  volume: number;
  haptics_gain: number;
  latency: number;       // ms of audio the controller keeps buffered
  separate_outputs: boolean;  // game output (triggers, LEDs) as its own reports
  mic: boolean;               // controller microphone as a system audio source
}

interface Status {
  enabled: boolean;
  uhid_available: boolean;
  is_root: boolean;
  audio: AudioOptions;
  gadget_error?: string | null;   // no virtual USB controller: no game audio device, no microphone
  controllers: ControllerStatus[];
}

const getStatus = callable<[], Status>("get_status");
const setEnabled = callable<[enabled: boolean], Status>("set_enabled");
const setAudio = callable<[options: Partial<AudioOptions>], Status>("set_audio");

interface UpdateInfo {
  current: string;
  latest: string | null;
  available: boolean;
  error?: string;
}
const checkUpdate = callable<[force: boolean], UpdateInfo>("check_update");
const installUpdate = callable<[], { ok: boolean; version?: string; error?: string }>("install_update");

const collectReport = callable<[], { ok: boolean; path?: string; size?: number; error?: string }>("collect_report");

/** Collects logs and system details into a zip for a bug report. */
function ReportSection() {
  const [state, setState] = useState<"idle" | "busy">("idle");
  const [result, setResult] = useState<string | undefined>();
  const s = t();
  return (
    <PanelSection title={s.report}>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={state === "busy"} description={result ?? s.reportDescription}
          onClick={async () => {
            setState("busy");
            const r = await collectReport().catch((e) => ({ ok: false, error: String(e) } as
              { ok: boolean; path?: string; size?: number; error?: string }));
            setResult(r.ok && r.path ? s.reportSaved(r.path, Math.ceil((r.size ?? 0) / 1024))
              : `${s.reportFailed}: ${r.error}`);
            setState("idle");
          }}>
          {state === "busy" ? s.reportBusy : s.reportButton}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

/** Current version, a manual check against GitHub releases, and the update itself. */
function UpdateSection() {
  const [info, setInfo] = useState<UpdateInfo | undefined>();
  const [state, setState] = useState<"idle" | "checking" | "updating" | "installed" | "failed">("idle");
  const [error, setError] = useState<string | undefined>();
  const check = async (force: boolean) => {
    setState("checking");
    const result = await checkUpdate(force).catch((e) => ({ current: "", latest: null, available: false, error: String(e) }));
    setInfo(result);
    setState("idle");
  };
  useEffect(() => {
    check(false);
  }, []);
  const s = t();
  const status =
    state === "checking" ? s.checking :
    state === "failed" ? `${s.updateFailed}: ${error}` :
    state === "installed" ? s.updateInstalled :
    !info ? "" :
    info.error ? `${s.checkFailed}: ${info.error}` :
    info.available && info.latest ? s.updateAvailable(info.latest) :
    info.latest ? s.upToDate : "";
  return (
    <PanelSection title={s.updates}>
      <PanelSectionRow>
        <Field label={info?.current ? s.version(info.current) : s.updates} description={status} />
      </PanelSectionRow>
      {info?.available && info.latest ? (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={state === "updating" || state === "installed"}
            description={s.updateDescription}
            onClick={async () => {
              setState("updating");
              // On success Decky reloads the plugin, and this panel with it.
              const result = await installUpdate().catch((e) => ({ ok: false, error: String(e) }));
              if (!result.ok) {
                setError(result.error);
                setState("failed");
              } else {
                // Normally the reload replaces this panel well before that.
                window.setTimeout(() => setState("installed"), 15000);
              }
            }}>
            {state === "updating" ? s.updating : s.updateButton(info.latest)}
          </ButtonItem>
        </PanelSectionRow>
      ) : (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={state === "checking"} onClick={() => check(true)}>
            {s.checkButton}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

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
  const s = t();

  return (
    <PanelSection title={s.audio}>
      <PanelSectionRow>
        <ToggleField
          label={s.gameAudio}
          description={s.gameAudioDescription}
          checked={audio.game_audio}
          disabled={!status.enabled}
          onChange={(value) => apply({ game_audio: value })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label={s.deckAudio}
          description={s.deckAudioDescription}
          checked={audio.enabled}
          disabled={!status.enabled}
          onChange={(value) => apply({ enabled: value })}
        />
      </PanelSectionRow>
      {streaming && (
        <>
          <PanelSectionRow>
            <ToggleField label={s.speaker} checked={audio.speaker}
              onChange={(value) => apply({ speaker: value })} />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField label={s.volume} value={audio.volume} min={0} max={100} step={5}
              showValue valueSuffix="%" disabled={!audio.speaker}
              onChange={(value) => apply({ volume: value }, true)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField label={s.haptics} checked={audio.haptics}
              onChange={(value) => apply({ haptics: value })} />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField label={s.hapticsGain} value={audio.haptics_gain} min={0} max={300} step={10}
              showValue valueSuffix="%" disabled={!audio.haptics}
              onChange={(value) => apply({ haptics_gain: value }, true)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField label={s.latency} description={s.latencyDescription} value={audio.latency}
              min={16} max={64} step={8} showValue valueSuffix={s.ms}
              onChange={(value) => apply({ latency: value }, true)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField label={s.separateOutputs} description={s.separateOutputsDescription}
              checked={audio.separate_outputs} onChange={(value) => apply({ separate_outputs: value })} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField label={s.mic} description={s.micDescription}
              checked={audio.mic} onChange={(value) => apply({ mic: value })} />
          </PanelSectionRow>
          {status.gadget_error && (
            <PanelSectionRow>
              <Field label={s.noGadget} description={`${s.noGadgetDescription} (${status.gadget_error})`} />
            </PanelSectionRow>
          )}
          {!bluetooth && (
            <PanelSectionRow>
              <Field label={s.noBluetooth} description={s.noBluetoothDescription} />
            </PanelSectionRow>
          )}
          {streams.map((c) => (
            <PanelSectionRow key={c.hidraw}>
              <Field label={`${c.audio!.source === "controller" ? s.streamGame : s.streamDeck}: ` +
                  s.streamState[c.audio!.state]}
                description={c.audio!.error ?? s.streamStats(c.audio!.sent, c.audio!.underruns)} />
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

  const s = t();
  if (!status) {
    return <PanelSection><PanelSectionRow>{s.loading}</PanelSectionRow></PanelSection>;
  }

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ToggleField
            label={s.emulation}
            description={s.emulationDescription}
            checked={status.enabled}
            disabled={!status.uhid_available}
            onChange={async (value) => setStatus(await setEnabled(value))}
          />
        </PanelSectionRow>
        {!status.is_root && (
          <PanelSectionRow>
            <Field label={s.notRoot} description={s.notRootDescription} />
          </PanelSectionRow>
        )}
        {!status.uhid_available && (
          <PanelSectionRow>
            <Field label={s.noUhid} description={s.noUhidDescription} />
          </PanelSectionRow>
        )}
      </PanelSection>
      {/* Right under the main toggle: state and battery visible without scrolling. */}
      <PanelSection title={s.controllers}>
        {status.controllers.length === 0 ? (
          <PanelSectionRow>
            <Field label={s.noController} />
          </PanelSectionRow>
        ) : (
          status.controllers.map((c) => (
            <PanelSectionRow key={c.hidraw}>
              <Field
                label={c.name}
                description={
                  c.state === "error"
                    ? `${s.state[c.state]}: ${c.error}`
                    : `${s.state[c.state]} · ${c.connection === "usb" ? "USB" : "Bluetooth"}` +
                      batteryText(c.battery) +
                      (c.mode === "usb-gadget" ? ` · ${s.virtualUsb}` : "") +
                      ` · ${c.mac}`
                }
              />
            </PanelSectionRow>
          ))
        )}
      </PanelSection>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" description={s.testButtonDescription}
            onClick={() => {
              Navigation.CloseSideMenus();
              Navigation.Navigate(TEST_ROUTE);
            }}>
            {s.testButton}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <AudioSection status={status} onStatus={setStatus} />
      <ReportSection />
      <UpdateSection />
      {/* Keeps the last rows clear of the Quick Access Menu's bottom bar. */}
      <div style={{ height: "48px" }} />
    </>
  );
};

export default definePlugin(() => {
  // Steam shows no battery for the virtual wired controller; warn about a low one ourselves.
  const onLowBattery = addEventListener<[name: string, capacity: number]>("low_battery", (name, capacity) =>
    toaster.toast({ title: t().lowBattery, body: `${name}: ${capacity}%` }));
  routerHook.addRoute(TEST_ROUTE, TestPage, { exact: true });
  return {
    onDismount() {
      removeEventListener("low_battery", onLowBattery);
      routerHook.removeRoute(TEST_ROUTE);
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
