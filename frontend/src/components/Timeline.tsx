// Minimal EDL timeline visualization (M1). The M3 version renders beat markers, the
// caption + music tracks, a preview player, and inline per-segment feedback.

type Segment = {
  id: string;
  source_clip: string;
  in: number;
  out: number;
  transition_in: string;
  reason: string;
};

type EDL = {
  version: number;
  timeline: Segment[];
};

const CLIP_COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#ec4899", "#8b5cf6", "#14b8a6"];

export function Timeline({ edl }: { edl: EDL }) {
  const total = edl.timeline.reduce((acc, s) => acc + (s.out - s.in), 0) || 1;
  const clipIndex = new Map<string, number>();

  return (
    <section style={{ marginTop: 24 }}>
      <h2 style={{ fontSize: 18 }}>EDL v{edl.version} · {total.toFixed(1)}s</h2>
      <div style={{ display: "flex", height: 56, borderRadius: 8, overflow: "hidden", border: "1px solid #1f2937" }}>
        {edl.timeline.map((s) => {
          if (!clipIndex.has(s.source_clip)) clipIndex.set(s.source_clip, clipIndex.size);
          const color = CLIP_COLORS[clipIndex.get(s.source_clip)! % CLIP_COLORS.length];
          const width = ((s.out - s.in) / total) * 100;
          return (
            <div key={s.id} title={`${s.id} · ${s.reason}`}
              style={{ width: `${width}%`, background: color, display: "flex", alignItems: "center",
                justifyContent: "center", fontSize: 11, color: "#0b0d12", fontWeight: 600 }}>
              {s.source_clip}
            </div>
          );
        })}
      </div>
      <ul style={{ marginTop: 16, listStyle: "none", padding: 0 }}>
        {edl.timeline.map((s) => (
          <li key={s.id} style={{ padding: "8px 0", borderBottom: "1px solid #1f2937", fontSize: 13 }}>
            <strong>{s.id}</strong> · {s.source_clip} · {s.in.toFixed(2)}→{s.out.toFixed(2)}s
            · <span style={{ opacity: 0.5 }}>{s.transition_in}</span>
            <div style={{ opacity: 0.7 }}>{s.reason}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}
