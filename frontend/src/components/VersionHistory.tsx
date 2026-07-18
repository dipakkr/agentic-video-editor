"use client";

// EDL version history. The versions endpoints are being added in parallel, so
// this component is defensive: if the list endpoint fails (404 / network), the
// component hides itself entirely.

import { useEffect, useState } from "react";
import { getEdlVersions, type Edl } from "@/lib/api";

export function VersionHistory({
  pid,
  currentVersion,
  refreshKey,
  onSelect,
}: {
  pid: string;
  currentVersion: number | null;
  /** Bump to refetch the version list (e.g. after feedback creates a new version). */
  refreshKey?: number;
  onSelect: (edl: Edl) => void;
}) {
  const [versions, setVersions] = useState<number[] | null>(null);
  const [loading, setLoading] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEdlVersions(pid)
      .then((res) => {
        if (!cancelled) setVersions(Array.isArray(res.versions) ? res.versions : null);
      })
      .catch(() => {
        // Endpoint not available yet — hide the component.
        if (!cancelled) setVersions(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pid, refreshKey]);

  if (!versions || versions.length === 0) return null;

  async function select(v: number) {
    setLoading(v);
    setError(null);
    try {
      const edl = await getEdlVersions(pid, v);
      onSelect(edl);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(null);
    }
  }

  return (
    <section
      style={{
        marginTop: 24,
        border: "1px solid #1f2937",
        borderRadius: 8,
        padding: 16,
      }}
    >
      <h2 style={{ fontSize: 15, margin: "0 0 8px" }}>Version history</h2>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {versions.map((v) => {
          const active = v === currentVersion;
          return (
            <button
              key={v}
              onClick={() => select(v)}
              disabled={loading !== null}
              style={{
                padding: "6px 12px",
                borderRadius: 6,
                border: active ? "1px solid #3b82f6" : "1px solid #1f2937",
                background: active ? "rgba(59,130,246,0.2)" : "#101420",
                color: active ? "#9fc1f7" : "#e7ecf3",
                fontSize: 12,
                cursor: loading !== null ? "default" : "pointer",
              }}
            >
              {loading === v ? "…" : `v${v}`}
            </button>
          );
        })}
      </div>
      {error && (
        <p style={{ fontSize: 12, color: "#f87171", margin: "8px 0 0" }}>
          {error}
        </p>
      )}
    </section>
  );
}
