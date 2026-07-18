"use client";

// EDL timeline view (M3): four stacked lanes on a shared time scale —
// VIDEO segments, BEAT ticks, CAPTIONS bar, MUSIC bar — plus the segment list
// (the explainability surface: every cut shows its reason).

import { useMemo, useState } from "react";
import type { Edl, Segment } from "@/lib/api";

const CLIP_COLORS = [
  "#3b82f6",
  "#22c55e",
  "#f59e0b",
  "#ec4899",
  "#8b5cf6",
  "#14b8a6",
  "#ef4444",
  "#eab308",
];

const LANE_LABEL_W = 84;

function segmentDuration(s: Segment): number {
  const speed = s.speed > 0 ? s.speed : 1;
  return (s.out - s.in) / speed;
}

function laneRowStyle(): React.CSSProperties {
  return {
    display: "flex",
    alignItems: "stretch",
    gap: 8,
    marginBottom: 6,
  };
}

function laneLabelStyle(): React.CSSProperties {
  return {
    width: LANE_LABEL_W,
    flexShrink: 0,
    fontSize: 10,
    letterSpacing: 1,
    textTransform: "uppercase",
    color: "#8b93a3",
    display: "flex",
    alignItems: "center",
    justifyContent: "flex-end",
    paddingRight: 4,
  };
}

export function Timeline({ edl }: { edl: Edl }) {
  const [hovered, setHovered] = useState<string | null>(null);

  const total = useMemo(
    () => edl.timeline.reduce((acc, s) => acc + segmentDuration(s), 0) || 1,
    [edl.timeline]
  );

  const clipColor = useMemo(() => {
    const index = new Map<string, string>();
    for (const s of edl.timeline) {
      if (!index.has(s.source_clip)) {
        index.set(
          s.source_clip,
          CLIP_COLORS[index.size % CLIP_COLORS.length]
        );
      }
    }
    return index;
  }, [edl.timeline]);

  const syncMap = edl.music?.sync_map ?? [];
  const captionStyle = edl.captions?.style ?? "none";
  const trackId = edl.music?.track_id ?? null;
  const offsetS = edl.music?.offset_s ?? 0;

  const overlays = edl.overlays ?? [];
  const titleCard = edl.graphics?.title_card ?? null;
  const lowerThirds = edl.graphics?.lower_thirds ?? [];
  const hasGraphics = titleCard !== null || lowerThirds.length > 0;
  const showOverlayLane = overlays.length > 0 || hasGraphics;

  return (
    <section style={{ marginTop: 28 }}>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>
        Timeline · EDL v{edl.version} · {total.toFixed(1)}s
      </h2>
      <p style={{ fontSize: 12, color: "#8b93a3", margin: "0 0 12px" }}>
        {edl.brief?.platform} · target {edl.brief?.target_duration_s}s ·{" "}
        {edl.brief?.tone}
      </p>

      {/* VIDEO lane */}
      <div style={laneRowStyle()}>
        <div style={laneLabelStyle()}>Video</div>
        <div
          style={{
            flex: 1,
            display: "flex",
            height: 48,
            borderRadius: 6,
            overflow: "hidden",
            border: "1px solid #1f2937",
            background: "#101420",
          }}
        >
          {edl.timeline.map((s) => {
            const width = (segmentDuration(s) / total) * 100;
            const color = clipColor.get(s.source_clip) ?? CLIP_COLORS[0];
            return (
              <div
                key={s.id}
                title={`${s.id} — ${s.reason}`}
                onMouseEnter={() => setHovered(s.id)}
                onMouseLeave={() =>
                  setHovered((h) => (h === s.id ? null : h))
                }
                style={{
                  width: `${width}%`,
                  background: color,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 4,
                  fontSize: 11,
                  fontWeight: 600,
                  color: "#0b0d12",
                  overflow: "hidden",
                  whiteSpace: "nowrap",
                  borderRight: "1px solid rgba(11,13,18,0.4)",
                  outline:
                    hovered === s.id ? "2px solid #e7ecf3" : "none",
                  outlineOffset: -2,
                  cursor: "default",
                }}
              >
                <span
                  style={{
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {s.source_clip}
                </span>
                {s.cut_snapped_to_beat && (
                  <span aria-label="cut snapped to beat">✂♪</span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* OVERLAYS lane (b-roll cutaways + graphics markers; hidden when empty) */}
      {showOverlayLane && (
        <div style={laneRowStyle()}>
          <div style={laneLabelStyle()}>Overlays</div>
          <div
            style={{
              flex: 1,
              position: "relative",
              height: 30,
              borderRadius: 6,
              border: "1px solid #1f2937",
              background: "#101420",
              overflow: "hidden",
            }}
          >
            {overlays.map((o) => {
              const duration = Math.max(o.out - o.in, 0);
              const left = (o.timeline_start_s / total) * 100;
              const width = (duration / total) * 100;
              if (left >= 100) return null;
              return (
                <div
                  key={o.id}
                  title={`${o.id} — ${o.reason}`}
                  style={{
                    position: "absolute",
                    left: `${left}%`,
                    width: `${Math.min(width, 100 - left)}%`,
                    top: 3,
                    bottom: 3,
                    borderRadius: 4,
                    border: "1px dashed #8b5cf6",
                    background:
                      "repeating-linear-gradient(45deg, rgba(139,92,246,0.28) 0, rgba(139,92,246,0.28) 4px, rgba(139,92,246,0.08) 4px, rgba(139,92,246,0.08) 8px)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 9,
                    color: "#c4b5fd",
                    overflow: "hidden",
                    whiteSpace: "nowrap",
                    textOverflow: "ellipsis",
                    cursor: "default",
                  }}
                >
                  {o.source_clip}
                </div>
              );
            })}

            {titleCard && (
              <span
                title={`title card "${titleCard.text}" @ ${titleCard.start_s.toFixed(1)}s`}
                style={{
                  position: "absolute",
                  left: `${Math.min((titleCard.start_s / total) * 100, 98)}%`,
                  top: 0,
                  fontSize: 11,
                  lineHeight: "10px",
                  color: "#f59e0b",
                  cursor: "default",
                }}
              >
                ▔
              </span>
            )}
            {lowerThirds.map((lt, i) => (
              <span
                key={`lt-${i}-${lt.start_s}`}
                title={`lower third "${lt.text}" @ ${lt.start_s.toFixed(1)}s`}
                style={{
                  position: "absolute",
                  left: `${Math.min((lt.start_s / total) * 100, 98)}%`,
                  bottom: 0,
                  fontSize: 11,
                  lineHeight: "10px",
                  color: "#14b8a6",
                  cursor: "default",
                }}
              >
                ▁
              </span>
            ))}
          </div>
        </div>
      )}

      {/* BEAT lane */}
      <div style={laneRowStyle()}>
        <div style={laneLabelStyle()}>Beats</div>
        <div
          style={{
            flex: 1,
            position: "relative",
            height: 22,
            borderRadius: 6,
            border: "1px solid #1f2937",
            background: "#101420",
            overflow: "hidden",
          }}
        >
          {syncMap.map((p, i) => {
            const left = (p.timeline_s / total) * 100;
            if (left < 0 || left > 100) return null;
            return (
              <div
                key={`${i}-${p.timeline_s}`}
                title={`beat @ ${p.timeline_s.toFixed(2)}s${
                  p.is_downbeat ? " (downbeat)" : ""
                }`}
                style={{
                  position: "absolute",
                  left: `${left}%`,
                  bottom: 0,
                  width: p.is_downbeat ? 2 : 1,
                  height: p.is_downbeat ? "100%" : "55%",
                  background: p.is_downbeat ? "#3b82f6" : "#4b5563",
                }}
              />
            );
          })}
          {syncMap.length === 0 && (
            <span
              style={{
                fontSize: 10,
                color: "#5b6472",
                padding: "4px 8px",
                display: "inline-block",
              }}
            >
              no beat map
            </span>
          )}
        </div>
      </div>

      {/* CAPTIONS lane (hidden when style is "none") */}
      {captionStyle !== "none" && (
        <div style={laneRowStyle()}>
          <div style={laneLabelStyle()}>Captions</div>
          <div
            style={{
              flex: 1,
              height: 18,
              borderRadius: 6,
              border: "1px solid #1f2937",
              background: "rgba(59,130,246,0.18)",
              display: "flex",
              alignItems: "center",
              paddingLeft: 8,
              fontSize: 10,
              letterSpacing: 0.5,
              color: "#9fc1f7",
            }}
          >
            captions: {captionStyle}
          </div>
        </div>
      )}

      {/* MUSIC lane */}
      {trackId && (
        <div style={laneRowStyle()}>
          <div style={laneLabelStyle()}>Music</div>
          <div
            style={{
              flex: 1,
              height: 18,
              borderRadius: 6,
              border: "1px solid #1f2937",
              background: "rgba(34,197,94,0.18)",
              display: "flex",
              alignItems: "center",
              paddingLeft: 8,
              fontSize: 10,
              letterSpacing: 0.5,
              color: "#8fe0ae",
            }}
          >
            ♪ {trackId} @ +{offsetS}s
          </div>
        </div>
      )}

      {/* Segment list — explainability surface */}
      <h3 style={{ fontSize: 14, margin: "20px 0 6px" }}>Cut decisions</h3>
      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {edl.timeline.map((s) => (
          <li
            key={s.id}
            onMouseEnter={() => setHovered(s.id)}
            onMouseLeave={() => setHovered((h) => (h === s.id ? null : h))}
            style={{
              padding: "10px 8px",
              borderBottom: "1px solid #1f2937",
              fontSize: 13,
              background:
                hovered === s.id ? "rgba(59,130,246,0.08)" : "transparent",
              borderRadius: 4,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: 2,
                  background:
                    clipColor.get(s.source_clip) ?? CLIP_COLORS[0],
                  display: "inline-block",
                }}
              />
              <strong>{s.id}</strong>
              <span style={{ color: "#8b93a3" }}>{s.source_clip}</span>
              <span>
                {s.in.toFixed(2)}→{s.out.toFixed(2)}s
              </span>
              {s.speed !== 1 && (
                <span style={{ color: "#f59e0b" }}>×{s.speed}</span>
              )}
              <span style={{ color: "#8b93a3" }}>{s.transition_in}</span>
              {s.cut_snapped_to_beat && (
                <span
                  style={{
                    fontSize: 10,
                    padding: "2px 6px",
                    borderRadius: 999,
                    background: "rgba(59,130,246,0.2)",
                    color: "#9fc1f7",
                  }}
                  title={
                    s.snapped_beat_s !== null
                      ? `snapped to beat @ ${s.snapped_beat_s.toFixed(2)}s`
                      : "snapped to beat"
                  }
                >
                  ♪ snapped
                  {s.snapped_beat_s !== null
                    ? ` @ ${s.snapped_beat_s.toFixed(2)}s`
                    : ""}
                </span>
              )}
            </div>
            <div style={{ marginTop: 4, color: "#b7c0cd" }}>{s.reason}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}
