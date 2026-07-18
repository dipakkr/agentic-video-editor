"use client";

// QC report panel (M5). The /qc endpoint is being added in parallel, so this
// component fetches defensively: on any failure (404 / network / bad shape)
// it hides itself entirely.

import { useEffect, useState } from "react";
import { getQcReport, type QcReport } from "@/lib/api";

export function QcPanel({
  pid,
  refreshKey,
}: {
  pid: string;
  /** Bump to refetch (e.g. after a feedback revision). */
  refreshKey?: number;
}) {
  const [report, setReport] = useState<QcReport | null>(null);

  useEffect(() => {
    let cancelled = false;
    getQcReport(pid)
      .then((res) => {
        if (cancelled) return;
        setReport(res && Array.isArray(res.results) ? res : null);
      })
      .catch(() => {
        if (!cancelled) setReport(null);
      });
    return () => {
      cancelled = true;
    };
  }, [pid, refreshKey]);

  if (!report || report.results.length === 0) return null;

  const failed = report.results.filter((r) => !r.passed);
  const agents = Array.from(
    new Set(failed.map((r) => r.responsible_agent).filter(Boolean))
  );

  return (
    <section
      style={{
        marginTop: 24,
        border: "1px solid #1f2937",
        borderRadius: 8,
        padding: 16,
      }}
    >
      <h2
        style={{
          fontSize: 15,
          margin: "0 0 8px",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        Quality control
        <span
          style={{
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 999,
            background: report.passed
              ? "rgba(34,197,94,0.25)"
              : "rgba(239,68,68,0.25)",
            color: report.passed ? "#8fe0ae" : "#f87171",
          }}
        >
          {report.passed ? "passed" : "failed"}
        </span>
      </h2>

      {!report.passed && (
        <p style={{ fontSize: 12, color: "#f87171", margin: "0 0 10px" }}>
          {failed.length} check{failed.length === 1 ? "" : "s"} failed — routed
          to: {agents.length > 0 ? agents.join(", ") : "unknown"}
        </p>
      )}

      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {report.results.map((r, i) => (
          <li
            key={`${r.check}-${i}`}
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 10,
              padding: "6px 4px",
              borderBottom: "1px solid #161c29",
              fontSize: 13,
            }}
          >
            <span
              aria-label={r.passed ? "passed" : "failed"}
              style={{
                width: 8,
                height: 8,
                borderRadius: 999,
                flexShrink: 0,
                alignSelf: "center",
                background: r.passed ? "#22c55e" : "#ef4444",
              }}
            />
            <strong style={{ minWidth: 140 }}>{r.check}</strong>
            <span style={{ color: "#b7c0cd", flex: 1 }}>{r.details}</span>
            {!r.passed && r.responsible_agent && (
              <span
                style={{
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 999,
                  background: "#1f2937",
                  color: "#8b93a3",
                  whiteSpace: "nowrap",
                }}
              >
                → {r.responsible_agent}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
