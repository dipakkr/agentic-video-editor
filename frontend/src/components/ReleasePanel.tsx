"use client";

// Release kit panel (M5): title options, hashtags, description preview,
// thumbnail candidates, and multi-preset export. The /release and /export
// endpoints are being added in parallel — the panel hides itself entirely if
// the release kit can't be fetched, and export errors render inline.

import { useEffect, useState } from "react";
import {
  exportProject,
  getReleaseKit,
  type ExportPresetResult,
  type ReleaseKit,
} from "@/lib/api";

const EXPORT_PRESETS = [
  "youtube",
  "shorts",
  "reels",
  "tiktok",
  "square",
] as const;

function formatTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

const thStyle: React.CSSProperties = {
  textAlign: "left",
  padding: "4px 8px",
  fontSize: 10,
  letterSpacing: 0.5,
  textTransform: "uppercase",
  color: "#8b93a3",
  borderBottom: "1px solid #1f2937",
};

const tdStyle: React.CSSProperties = {
  padding: "6px 8px",
  fontSize: 12,
  borderBottom: "1px solid #161c29",
  verticalAlign: "top",
};

export function ReleasePanel({
  pid,
  refreshKey,
}: {
  pid: string;
  /** Bump to refetch (e.g. after a feedback revision). */
  refreshKey?: number;
}) {
  const [kit, setKit] = useState<ReleaseKit | null>(null);
  const [selectedTitle, setSelectedTitle] = useState(0);
  const [descOpen, setDescOpen] = useState(false);

  // Export state
  const [presets, setPresets] = useState<Set<string>>(new Set(["youtube"]));
  const [exporting, setExporting] = useState(false);
  const [exportResults, setExportResults] = useState<Record<
    string,
    ExportPresetResult
  > | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getReleaseKit(pid)
      .then((res) => {
        if (cancelled) return;
        setKit(res && Array.isArray(res.titles) ? res : null);
      })
      .catch(() => {
        if (!cancelled) setKit(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pid, refreshKey]);

  if (!kit) return null;

  function togglePreset(p: string) {
    setPresets((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  }

  async function runExport() {
    if (presets.size === 0 || exporting) return;
    setExporting(true);
    setExportError(null);
    setExportResults(null);
    try {
      const res = await exportProject(pid, Array.from(presets));
      setExportResults(res?.results ?? {});
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(false);
    }
  }

  const hashtags = kit.hashtags ?? [];
  const chapters = kit.chapters ?? [];
  const thumbnails = kit.thumbnails ?? [];

  return (
    <section
      style={{
        marginTop: 24,
        border: "1px solid #1f2937",
        borderRadius: 8,
        padding: 16,
      }}
    >
      <h2 style={{ fontSize: 15, margin: "0 0 12px" }}>Release kit</h2>

      {/* Title options */}
      {kit.titles.length > 0 && (
        <div>
          <h3 style={{ fontSize: 12, color: "#8b93a3", margin: "0 0 6px" }}>
            Title
          </h3>
          {kit.titles.map((t, i) => (
            <label
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "6px 8px",
                borderRadius: 6,
                fontSize: 13,
                cursor: "pointer",
                background:
                  selectedTitle === i ? "rgba(59,130,246,0.12)" : "transparent",
                border:
                  selectedTitle === i
                    ? "1px solid rgba(59,130,246,0.5)"
                    : "1px solid transparent",
                marginBottom: 4,
              }}
            >
              <input
                type="radio"
                name="release-title"
                checked={selectedTitle === i}
                onChange={() => setSelectedTitle(i)}
                style={{ accentColor: "#3b82f6" }}
              />
              {t}
            </label>
          ))}
        </div>
      )}

      {/* Hashtags */}
      {hashtags.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
            marginTop: 12,
          }}
        >
          {hashtags.map((h, i) => (
            <span
              key={`${h}-${i}`}
              style={{
                fontSize: 11,
                padding: "3px 10px",
                borderRadius: 999,
                background: "#1f2937",
                color: "#9fc1f7",
              }}
            >
              {h.startsWith("#") ? h : `#${h}`}
            </span>
          ))}
        </div>
      )}

      {/* Description preview (collapsible) */}
      {kit.description && (
        <div style={{ marginTop: 12 }}>
          <button
            type="button"
            onClick={() => setDescOpen((o) => !o)}
            aria-expanded={descOpen}
            style={{
              background: "none",
              border: "none",
              color: "#8b93a3",
              fontSize: 12,
              cursor: "pointer",
              padding: 0,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span
              style={{
                display: "inline-block",
                transform: descOpen ? "rotate(90deg)" : "none",
                transition: "transform 120ms",
              }}
            >
              ▸
            </span>
            Description preview
          </button>
          {descOpen && (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                fontFamily: "inherit",
                fontSize: 12,
                color: "#b7c0cd",
                background: "#101420",
                border: "1px solid #1f2937",
                borderRadius: 6,
                padding: 12,
                margin: "8px 0 0",
                maxHeight: 260,
                overflowY: "auto",
              }}
            >
              {kit.description}
              {chapters.length > 0 && (
                <>
                  {"\n\nChapters:\n"}
                  {chapters
                    .map((c) => `${formatTime(c.time_s)} ${c.label}`)
                    .join("\n")}
                </>
              )}
            </pre>
          )}
        </div>
      )}

      {/* Thumbnail candidates */}
      {thumbnails.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <h3 style={{ fontSize: 12, color: "#8b93a3", margin: "0 0 6px" }}>
            Thumbnail candidates
          </h3>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={thStyle}>Clip</th>
                  <th style={thStyle}>Timeline</th>
                  <th style={thStyle}>Score</th>
                  <th style={thStyle}>Reason</th>
                </tr>
              </thead>
              <tbody>
                {thumbnails.map((t, i) => (
                  <tr key={`${t.clip_id}-${i}`}>
                    <td style={tdStyle}>{t.clip_id}</td>
                    <td style={tdStyle}>{t.timeline_s.toFixed(1)}s</td>
                    <td style={tdStyle}>{t.score.toFixed(2)}</td>
                    <td style={{ ...tdStyle, color: "#b7c0cd" }}>{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Export row */}
      <div
        style={{
          marginTop: 16,
          paddingTop: 14,
          borderTop: "1px solid #1f2937",
        }}
      >
        <h3 style={{ fontSize: 12, color: "#8b93a3", margin: "0 0 8px" }}>
          Export
        </h3>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            flexWrap: "wrap",
          }}
        >
          {EXPORT_PRESETS.map((p) => (
            <label
              key={p}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 5,
                fontSize: 12,
                color: "#b7c0cd",
                cursor: exporting ? "default" : "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={presets.has(p)}
                onChange={() => togglePreset(p)}
                disabled={exporting}
                style={{ accentColor: "#3b82f6" }}
              />
              {p}
            </label>
          ))}
          <button
            onClick={runExport}
            disabled={exporting || presets.size === 0}
            style={{
              padding: "7px 16px",
              background:
                exporting || presets.size === 0 ? "#1f2937" : "#3b82f6",
              color: exporting || presets.size === 0 ? "#5b6472" : "#e7ecf3",
              border: "none",
              borderRadius: 6,
              fontSize: 13,
              fontWeight: 600,
              cursor:
                exporting || presets.size === 0 ? "default" : "pointer",
            }}
          >
            {exporting ? "Exporting…" : "Export"}
          </button>
        </div>

        {exportError && (
          <p style={{ fontSize: 12, color: "#f87171", margin: "8px 0 0" }}>
            {exportError}
          </p>
        )}

        {exportResults && (
          <ul style={{ listStyle: "none", margin: "10px 0 0", padding: 0 }}>
            {Object.entries(exportResults).map(([preset, r]) => {
              const ok = !r.error && r.status !== "error";
              return (
                <li
                  key={preset}
                  style={{
                    display: "flex",
                    alignItems: "baseline",
                    gap: 10,
                    fontSize: 12,
                    padding: "4px 0",
                  }}
                >
                  <span
                    style={{
                      width: 8,
                      height: 8,
                      borderRadius: 999,
                      flexShrink: 0,
                      alignSelf: "center",
                      background: ok ? "#22c55e" : "#ef4444",
                    }}
                  />
                  <strong style={{ minWidth: 70 }}>{preset}</strong>
                  <span style={{ color: ok ? "#8fe0ae" : "#f87171" }}>
                    {r.error ?? r.status ?? (ok ? "done" : "failed")}
                  </span>
                  {typeof r.path === "string" && r.path && (
                    <code
                      style={{
                        color: "#8b93a3",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {r.path}
                    </code>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
