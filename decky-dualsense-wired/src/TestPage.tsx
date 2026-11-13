import { DialogButton, Focusable, ToggleField } from "@decky/ui";
import { callable } from "@decky/api";
import { CSSProperties, ReactNode, useEffect, useRef, useState } from "react";
import { t } from "./i18n";

export const TEST_ROUTE = "/decky-dualsense-wired/test";

interface InputState {
  buttons: Record<string, boolean>;
  sticks: number[];
  triggers: number[];
  gyro: number[];
  accel: number[];
  touch: { x: number; y: number }[];
  battery: number;
  charging: boolean;
  headphones: boolean;
  headset_mic: boolean;
}

interface TesterState {
  running: boolean;
  error: string | null;
  controller: { name: string; mode: string; mac: string; virtual_hidraw: string; edge: boolean } | null;
  controllers: { mac: string; name: string }[];
  input: InputState | null;
  rate: number;
  capture: boolean;
  exit_capture: boolean;
  exit_hold: number;
  click_mode: boolean;
  audio: { mode: "game" | "deck" | "off"; playing: boolean; error: string | null };
  mic: {
    enabled: boolean; muted: boolean; source: string | null; level: number; recording: boolean;
    playing_back: boolean; frames: number; error: string | null;
  };
}

const testerStart = callable<[], TesterState>("tester_start");
const testerStop = callable<[], void>("tester_stop");
const testerState = callable<[], TesterState>("tester_state");
const testerAction = callable<[action: string, args: Record<string, unknown>], { ok: boolean; error?: string }>(
  "tester_action");
const setAudio = callable<[options: Record<string, unknown>], unknown>("set_audio");

const POLL_MS = 33;

const COLORS = {
  idle: "#3d4450",
  pressed: "#1a9fff",
  seen: "#59bf40",
  text: "#dcdedf",
  dim: "#8b929a",
  panel: "rgba(255,255,255,0.04)",
};

const CHECK_BUTTONS = [
  "cross", "circle", "square", "triangle", "dpad_up", "dpad_down", "dpad_left", "dpad_right",
  "l1", "r1", "l2", "r2", "l3", "r3", "create", "options", "ps", "touchpad", "mute",
];
const EDGE_BUTTONS = ["fn_left", "fn_right", "back_left", "back_right"];

const LABELS: Record<string, string> = {
  cross: "✕", circle: "○", square: "□", triangle: "△",
  dpad_up: "▲", dpad_down: "▼", dpad_left: "◀", dpad_right: "▶",
  l1: "L1", r1: "R1", l2: "L2", r2: "R2", l3: "L3", r3: "R3",
  create: "Create", options: "Options", ps: "PS", touchpad: "Touch", mute: "Mute",
  fn_left: "Fn L", fn_right: "Fn R", back_left: "LB", back_right: "RB",
};

function Key({ name, input, seen }: { name: string; input: InputState | null; seen: Set<string> }) {
  const on = !!input?.buttons[name];
  return (
    <div style={{
      minWidth: "34px", padding: "4px 8px", borderRadius: "6px", textAlign: "center", fontSize: "13px",
      background: on ? COLORS.pressed : COLORS.idle, color: COLORS.text,
      boxShadow: seen.has(name) ? `inset 0 0 0 2px ${COLORS.seen}` : "none",
    }}>{LABELS[name]}</div>
  );
}

function Stick({ x, y, pressed }: { x: number; y: number; pressed: boolean }) {
  const size = 84;
  const px = ((x - 128) / 128) * (size / 2 - 8);
  const py = ((y - 128) / 128) * (size / 2 - 8);
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
      <div style={{
        position: "relative", width: size, height: size, borderRadius: "50%", background: COLORS.idle,
        boxShadow: pressed ? `0 0 0 3px ${COLORS.pressed}` : "none",
      }}>
        <div style={{
          position: "absolute", width: 14, height: 14, borderRadius: "50%", background: COLORS.pressed,
          left: size / 2 - 7 + px, top: size / 2 - 7 + py,
        }} />
      </div>
      <div style={{ fontSize: "11px", color: COLORS.dim }}>{x} · {y}</div>
    </div>
  );
}

function Bar({ value, max, label, vertical = true, color = COLORS.pressed }:
  { value: number; max: number; label: string; vertical?: boolean; color?: string }) {
  const part = Math.max(0, Math.min(1, value / max));
  const box: CSSProperties = vertical
    ? { width: 22, height: 84, display: "flex", alignItems: "flex-end" }
    : { width: "100%", height: 12, display: "flex" };
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px", flex: vertical ? undefined : 1 }}>
      <div style={{ ...box, background: COLORS.idle, borderRadius: 4, overflow: "hidden" }}>
        <div style={vertical ? { width: "100%", height: `${part * 100}%`, background: color }
          : { height: "100%", width: `${part * 100}%`, background: color }} />
      </div>
      <div style={{ fontSize: "11px", color: COLORS.dim }}>{label}</div>
    </div>
  );
}

function Touchpad({ input }: { input: InputState | null }) {
  const w = 220, h = 104;
  return (
    <div style={{
      position: "relative", width: w, height: h, borderRadius: 8, background: COLORS.idle,
      boxShadow: input?.buttons.touchpad ? `0 0 0 3px ${COLORS.pressed}` : "none",
    }}>
      {(input?.touch ?? []).map((p, i) => (
        <div key={i} style={{
          position: "absolute", width: 14, height: 14, borderRadius: "50%", background: COLORS.pressed,
          left: (p.x / 1920) * w - 7, top: (p.y / 1080) * h - 7,
        }} />
      ))}
    </div>
  );
}

function Motion({ label, values }: { label: string; values: number[] }) {
  return (
    <div style={{ display: "flex", gap: "6px", alignItems: "center", fontSize: "12px", color: COLORS.dim }}>
      <span style={{ width: 110 }}>{label}</span>
      {values.map((v, i) => (
        <Bar key={i} value={v + 32768} max={65535} label={`${"XYZ"[i]} ${v}`} vertical={false} />
      ))}
    </div>
  );
}

function InputView({ state, seen }: { state: TesterState; seen: Set<string> }) {
  const input = state.input;
  const s = t().test;
  const sticks = input?.sticks ?? [128, 128, 128, 128];
  const triggers = input?.triggers ?? [0, 0];
  const row: CSSProperties = { display: "flex", gap: "6px", flexWrap: "wrap", justifyContent: "center" };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "10px", padding: "10px", background: COLORS.panel, borderRadius: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "12px" }}>
        <div style={{ display: "flex", gap: "8px", alignItems: "flex-end" }}>
          <Bar value={triggers[0]} max={255} label={`L2 ${triggers[0]}`} />
          <Stick x={sticks[0]} y={sticks[1]} pressed={!!input?.buttons.l3} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "6px" }}>
          <Touchpad input={input} />
          <div style={row}>
            {["create", "ps", "mute", "options"].map((b) => <Key key={b} name={b} input={input} seen={seen} />)}
          </div>
        </div>
        <div style={{ display: "flex", gap: "8px", alignItems: "flex-end" }}>
          <Stick x={sticks[2]} y={sticks[3]} pressed={!!input?.buttons.r3} />
          <Bar value={triggers[1]} max={255} label={`R2 ${triggers[1]}`} />
        </div>
      </div>
      <div style={{ ...row, justifyContent: "space-between" }}>
        <div style={row}>
          {["l1", "l2", "l3", "dpad_up", "dpad_down", "dpad_left", "dpad_right"].map((b) =>
            <Key key={b} name={b} input={input} seen={seen} />)}
        </div>
        <div style={row}>
          {["triangle", "circle", "cross", "square", "r1", "r2", "r3", "touchpad"].map((b) =>
            <Key key={b} name={b} input={input} seen={seen} />)}
        </div>
      </div>
      {state.controller?.edge && (
        <div style={row}>{EDGE_BUTTONS.map((b) => <Key key={b} name={b} input={input} seen={seen} />)}</div>
      )}
      <Motion label={s.gyro} values={input?.gyro ?? [0, 0, 0]} />
      <Motion label={s.accel} values={input?.accel ?? [0, 0, 0]} />
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "14px" }}>
      <div style={{ fontSize: "15px", fontWeight: "bold", color: COLORS.text, textTransform: "uppercase", letterSpacing: "0.5px" }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <div style={{ fontSize: "12px", color: COLORS.dim }}>{children}</div>;
}

function ButtonRow({ buttons }: { buttons: { label: string; onClick: () => void; disabled?: boolean }[] }) {
  return (
    <Focusable style={{ display: "flex", gap: "6px", flexWrap: "wrap" }} flow-children="horizontal">
      {buttons.map((b) => (
        <DialogButton key={b.label} disabled={b.disabled} onClick={b.onClick}
          style={{ width: "auto", minWidth: "110px", padding: "6px 14px", flex: "0 0 auto" }}>
          {b.label}
        </DialogButton>
      ))}
    </Focusable>
  );
}

/** Full-screen capture: only shows input; ○ / ✕ / d-pad don't move or leave the page. */
function CaptureView({ state, seen, onExit }: { state: TesterState; seen: Set<string>; onExit: () => void }) {
  const s = t().test;
  const swallow = (e: Event) => {
    e.stopPropagation();
    e.preventDefault();
  };
  return (
    <Focusable
      style={{ display: "flex", flexDirection: "column", gap: "10px", outline: "none" }}
      noFocusRing preferredFocus
      onCancel={swallow} onCancelButton={swallow} onOKButton={swallow} onSecondaryButton={swallow}
      onOptionsButton={swallow} onMenuButton={swallow} onGamepadDirection={swallow}
      onButtonDown={(e) => e.stopPropagation()}
    >
      <InputView state={state} seen={seen} />
      <div onClick={onExit} style={{
        padding: "10px", borderRadius: 8, background: COLORS.idle, textAlign: "center", color: COLORS.text,
        position: "relative", overflow: "hidden",
      }}>
        <div style={{
          position: "absolute", left: 0, top: 0, bottom: 0, width: `${state.exit_hold * 100}%`,
          background: COLORS.pressed, opacity: 0.5,
        }} />
        <span style={{ position: "relative" }}>{s.captureHint}</span>
      </div>
    </Focusable>
  );
}

export function TestPage() {
  const [state, setState] = useState<TesterState | undefined>();
  const [seen, setSeen] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState<string | undefined>();
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await testerState();
        if (!alive.current) return;
        setState(next);
        const pressed = Object.entries(next.input?.buttons ?? {}).filter(([, v]) => v).map(([k]) => k);
        if (pressed.length) {
          setSeen((old) => pressed.every((p) => old.has(p)) ? old : new Set([...old, ...pressed]));
        }
      } catch {
        // backend reloading: keep trying
      }
      if (alive.current) timer = window.setTimeout(poll, POLL_MS);
    };
    testerStart().then((st) => alive.current && setState(st)).finally(poll);
    return () => {
      alive.current = false;
      window.clearTimeout(timer);
      testerStop();
    };
  }, []);

  const s = t().test;
  const act = async (action: string, args: Record<string, unknown> = {}) => {
    const result = await testerAction(action, args).catch((e) => ({ ok: false, error: String(e) }));
    setMessage(result.ok ? undefined : `${s.error}: ${result.error}`);
  };

  const page: CSSProperties = {
    marginTop: "40px", height: "calc(100% - 40px)", overflowY: "auto", padding: "12px 24px 60px",
    boxSizing: "border-box", color: COLORS.text,
  };

  if (!state) return <div style={page}>{t().loading}</div>;

  if (state.capture) {
    return (
      <div style={page}>
        <CaptureView state={state} seen={seen} onExit={() => act("capture", { on: false })} />
      </div>
    );
  }

  const input = state.input;
  const c = state.controller;
  const checkList = CHECK_BUTTONS.concat(c?.edge ? EDGE_BUTTONS : []);
  const checked = checkList.filter((b) => seen.has(b)).length;
  const header = c
    ? [c.name, c.mac.slice(-5).toUpperCase(), c.mode === "usb-gadget" ? s.virtualUsb : "uhid", s.rate(state.rate),
       input ? `🔋 ${input.battery}%${input.charging ? " ⚡" : ""}` : "",
       input?.headphones ? (input.headset_mic ? s.headsetPlugged : s.headphonesPlugged) : ""]
      .filter(Boolean).join(" · ")
    : s.waiting;
  const audioOff = state.audio.mode === "off" || c?.mode !== "usb-gadget" && state.audio.mode === "game";
  const mic = state.mic;

  return (
    <div style={page}>
      <div style={{ fontSize: "20px", fontWeight: "bold" }}>{s.title}</div>
      <Note>{header}</Note>
      {(message || state.error) && <div style={{ color: "#ff6b6b", fontSize: "13px" }}>{message || state.error}</div>}

      {state.controllers.length > 1 && (
        <Section title={s.controllers}>
          <ButtonRow buttons={state.controllers.map((ctl, i) => ({
            label: `${ctl.mac === c?.mac ? "● " : ""}${s.controllerN(i + 1)}`,
            onClick: () => {
              setSeen(new Set());
              act("select", { mac: ctl.mac });
            },
          }))} />
          <Note>{s.controllersHint}</Note>
        </Section>
      )}

      <Section title={s.input}>
        <InputView state={state} seen={seen} />
        <Note>{s.checked(checked, checkList.length)}</Note>
        <ButtonRow buttons={[
          { label: s.capture, onClick: () => act("capture", { on: true }), disabled: !c },
          { label: s.resetChecked, onClick: () => setSeen(new Set()) },
        ]} />
        <Note>{s.captureDescription}</Note>
      </Section>

      <Section title={s.vibration}>
        <ButtonRow buttons={[
          { label: s.rumbleWeak, onClick: () => act("rumble", { left: 0, right: 90 }) },
          { label: s.rumbleStrong, onClick: () => act("rumble", { left: 255, right: 255 }) },
          { label: s.rumbleLeft, onClick: () => act("rumble", { left: 200, right: 0 }) },
          { label: s.rumbleRight, onClick: () => act("rumble", { left: 0, right: 200 }) },
        ]} />
        <Note>{s.rumbleNote}</Note>
      </Section>

      <Section title={s.triggers}>
        <ButtonRow buttons={(["off", "resistance", "section", "weapon", "vibration"] as const).map((e) => ({
          label: s.effects[e], onClick: () => act("trigger", { which: "both", effect: e }),
        }))} />
        <Note>{s.triggerHint}</Note>
      </Section>

      <Section title={s.lights}>
        <ButtonRow buttons={(["red", "green", "blue", "white", "off"] as const).map((color) => ({
          label: s.colors[color], onClick: () => act("lightbar", { color }),
        }))} />
        <ButtonRow buttons={[
          { label: "1", onClick: () => act("player_leds", { mask: 0x04 }) },
          { label: "2", onClick: () => act("player_leds", { mask: 0x0A }) },
          { label: "3", onClick: () => act("player_leds", { mask: 0x15 }) },
          { label: "4", onClick: () => act("player_leds", { mask: 0x1B }) },
          { label: s.allLeds, onClick: () => act("player_leds", { mask: 0x1F }) },
          { label: s.colors.off, onClick: () => act("player_leds", { mask: 0 }) },
        ]} />
        <Note>{s.playerLeds}</Note>
        <ButtonRow buttons={(["on", "pulse", "off"] as const).map((mode) => ({
          label: s.muteLedModes[mode], onClick: () => act("mute_led", { mode }),
        }))} />
        <Note>{s.muteLed}</Note>
      </Section>

      <Section title={s.sound}>
        <Note>
          {audioOff ? s.soundOff : state.audio.mode === "deck" ? s.soundDeck : s.soundGame}
          {input?.headphones ? ` ${s.toHeadphones}` : ""}
        </Note>
        <ButtonRow buttons={(["tone", "left", "right", "sweep"] as const).map((sound) => ({
          label: s.sounds[sound], onClick: () => act("play", { sound }), disabled: audioOff,
        }))} />
        <ButtonRow buttons={(["haptic_left", "haptic_right", "haptic_sweep"] as const).map((sound) => ({
          label: s.sounds[sound], onClick: () => act("play", { sound }), disabled: audioOff,
        }))} />
        <Focusable>
          <ToggleField label={s.clickMode} description={s.clickDescription} checked={state.click_mode}
            disabled={audioOff} onChange={(on) => act("click_mode", { on })} />
        </Focusable>
      </Section>

      <Section title={s.mic}>
        {!mic.enabled ? (
          <>
            <Note>{s.micOff}</Note>
            <ButtonRow buttons={[{ label: s.micEnable, onClick: () => setAudio({ mic: true }), disabled: audioOff }]} />
          </>
        ) : !mic.source ? (
          <Note>{s.micStarting}</Note>
        ) : (
          <>
            <Note>
              {mic.muted ? s.micMuted : s.micLive}
              {` · ${s.micFrames(mic.frames)}`}
              {input?.headset_mic ? ` · ${s.headsetMic}` : ""}
            </Note>
            <Bar value={mic.level} max={1} label={s.micLevel} vertical={false}
              color={mic.level > 0.9 ? "#ff6b6b" : COLORS.seen} />
            <ButtonRow buttons={[{
              label: mic.recording ? s.recording : mic.playing_back ? s.playingBack : s.micRecord,
              onClick: () => act("mic_record"), disabled: mic.recording || mic.playing_back || audioOff,
            }]} />
            <Note>{s.micHint}</Note>
          </>
        )}
        {mic.error && <Note>{mic.error}</Note>}
      </Section>
    </div>
  );
}
