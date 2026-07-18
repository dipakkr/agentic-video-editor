"use client";

// Collapsible "Agent settings" panel used inside the brief form (M5).
// Every knob maps to Brief.agent_config; the parent passes the emitted
// AgentConfig to createProject. Defaults mirror the backend schema defaults
// so an untouched panel sends an equivalent-to-empty config.

import { useState } from "react";
import type { AgentConfig } from "@/lib/api";

const CAPTION_STYLES = [
  "karaoke_bold",
  "phrase_pop",
  "clean_subtitle",
  "none",
] as const;

const TRANSITION_STYLES = ["default", "hard", "crossfade", "whip"] as const;

const inputStyle: React.CSSProperties = {
  background: "#101420",
  color: "#e7ecf3",
  border: "1px solid #1f2937",
  borderRadius: 6,
  padding: "8px 10px",
  fontSize: 13,
  fontFamily: "inherit",
};

const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 4,
  fontSize: 12,
  color: "#8b93a3",
};

interface SettingsState {
  captionStyle: string; // "" = agent default
  transitionStyle: string; // "default" = agent default
  duckDb: number;
  targetLufs: string; // raw text so the field can be cleared
  musicGenrePin: string;
  enableBroll: boolean;
  enableGraphics: boolean;
}

const INITIAL: SettingsState = {
  captionStyle: "",
  transitionStyle: "default",
  duckDb: -14,
  targetLufs: "",
  musicGenrePin: "",
  enableBroll: true,
  enableGraphics: true,
};

function toAgentConfig(s: SettingsState): AgentConfig {
  const cfg: AgentConfig = {
    enable_broll: s.enableBroll,
    enable_graphics: s.enableGraphics,
  };
  if (s.captionStyle) cfg.caption_style = s.captionStyle;
  if (s.transitionStyle && s.transitionStyle !== "default") {
    cfg.transition_style = s.transitionStyle;
  }
  if (s.duckDb !== -14) cfg.duck_db = s.duckDb;
  const lufs = Number(s.targetLufs);
  if (s.targetLufs.trim() !== "" && Number.isFinite(lufs)) {
    cfg.target_lufs = lufs;
  }
  if (s.musicGenrePin.trim()) cfg.music_genre_pin = s.musicGenrePin.trim();
  return cfg;
}

export function SettingsPanel({
  disabled,
  onChange,
}: {
  disabled?: boolean;
  onChange: (config: AgentConfig) => void;
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<SettingsState>(INITIAL);

  function update(patch: Partial<SettingsState>) {
    setState((prev) => {
      const next = { ...prev, ...patch };
      onChange(toAgentConfig(next));
      return next;
    });
  }

  return (
    <div
      style={{
        marginTop: 16,
        border: "1px solid #1f2937",
        borderRadius: 8,
        background: "rgba(16,20,32,0.5)",
      }}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "none",
          border: "none",
          color: "#8b93a3",
          padding: "10px 12px",
          fontSize: 12,
          letterSpacing: 0.5,
          textTransform: "uppercase",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <span
          style={{
            display: "inline-block",
            transform: open ? "rotate(90deg)" : "none",
            transition: "transform 120ms",
          }}
        >
          ▸
        </span>
        Agent settings
      </button>

      {open && (
        <div style={{ padding: "4px 12px 14px" }}>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <label style={labelStyle}>
              Caption style
              <select
                value={state.captionStyle}
                onChange={(e) => update({ captionStyle: e.target.value })}
                disabled={disabled}
                style={inputStyle}
              >
                <option value="">auto</option>
                {CAPTION_STYLES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>

            <label style={labelStyle}>
              Transition style
              <select
                value={state.transitionStyle}
                onChange={(e) => update({ transitionStyle: e.target.value })}
                disabled={disabled}
                style={inputStyle}
              >
                {TRANSITION_STYLES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>

            <label style={{ ...labelStyle, minWidth: 160 }}>
              Duck depth · {state.duckDb} dB
              <input
                type="range"
                min={-24}
                max={0}
                step={1}
                value={state.duckDb}
                onChange={(e) => update({ duckDb: Number(e.target.value) })}
                disabled={disabled}
                style={{ accentColor: "#3b82f6" }}
              />
            </label>

            <label style={labelStyle}>
              Target LUFS
              <input
                type="number"
                step={0.5}
                placeholder="-14"
                value={state.targetLufs}
                onChange={(e) => update({ targetLufs: e.target.value })}
                disabled={disabled}
                style={{ ...inputStyle, width: 100 }}
              />
            </label>

            <label style={labelStyle}>
              Music genre pin
              <input
                type="text"
                placeholder="e.g. lo-fi hip hop"
                value={state.musicGenrePin}
                onChange={(e) => update({ musicGenrePin: e.target.value })}
                disabled={disabled}
                style={{ ...inputStyle, width: 160 }}
              />
            </label>
          </div>

          <div style={{ display: "flex", gap: 20, marginTop: 12 }}>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "#8b93a3",
                cursor: disabled ? "default" : "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={state.enableBroll}
                onChange={(e) => update({ enableBroll: e.target.checked })}
                disabled={disabled}
                style={{ accentColor: "#3b82f6" }}
              />
              B-roll overlays
            </label>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "#8b93a3",
                cursor: disabled ? "default" : "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={state.enableGraphics}
                onChange={(e) => update({ enableGraphics: e.target.checked })}
                disabled={disabled}
                style={{ accentColor: "#3b82f6" }}
              />
              Graphics (title card / lower thirds)
            </label>
          </div>
        </div>
      )}
    </div>
  );
}
