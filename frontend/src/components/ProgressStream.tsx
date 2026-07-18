"use client";

// Live pipeline progress via SSE. Renders one row per known stage with a status
// chip derived from the event stream; closes the EventSource on {stage:"done"}
// or unmount.

import { useEffect, useState } from "react";
import { eventsUrl, type ProgressEvent } from "@/lib/api";

const STAGES = [
  "ingest",
  "editorial",
  "music_beat",
  "captions",
  "render",
  "done",
] as const;

type StageStatus = "pending" | "running" | "done" | "degraded" | "skipped";

const CHIP_COLORS: Record<StageStatus, { bg: string; fg: string }> = {
  pending: { bg: "#1f2937", fg: "#8b93a3" },
  running: { bg: "rgba(59,130,246,0.25)", fg: "#9fc1f7" },
  done: { bg: "rgba(34,197,94,0.25)", fg: "#8fe0ae" },
  degraded: { bg: "rgba(245,158,11,0.25)", fg: "#f5c97b" },
  skipped: { bg: "rgba(139,147,163,0.2)", fg: "#8b93a3" },
};

function normalizeStatus(raw: string): StageStatus {
  switch (raw) {
    case "running":
    case "started":
    case "start":
      return "running";
    case "done":
    case "ok":
    case "complete":
    case "completed":
    case "finished":
      return "done";
    case "degraded":
    case "warning":
    case "error":
    case "failed":
      return "degraded";
    case "skipped":
      return "skipped";
    default:
      return "running";
  }
}

export function ProgressStream({
  pid,
  onDone,
}: {
  pid: string;
  onDone?: () => void;
}) {
  const [statuses, setStatuses] = useState<Record<string, StageStatus>>({});
  const [lastNote, setLastNote] = useState<string | null>(null);
  const [connError, setConnError] = useState(false);

  useEffect(() => {
    setStatuses({});
    setLastNote(null);
    setConnError(false);

    const source = new EventSource(eventsUrl(pid));
    let closed = false;

    source.onmessage = (ev: MessageEvent<string>) => {
      let event: ProgressEvent;
      try {
        event = JSON.parse(ev.data) as ProgressEvent;
      } catch {
        return;
      }
      const status = normalizeStatus(event.status);
      setStatuses((prev) => ({ ...prev, [event.stage]: status }));
      const note = event.data && (event.data.message ?? event.data.note);
      if (typeof note === "string") setLastNote(note);

      if (event.stage === "done") {
        closed = true;
        source.close();
        onDone?.();
      }
    };

    source.onerror = () => {
      if (!closed) setConnError(true);
    };

    return () => {
      closed = true;
      source.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid]);

  // Any stage the backend reported that isn't in our known list still gets a row.
  const extraStages = Object.keys(statuses).filter(
    (s) => !(STAGES as readonly string[]).includes(s)
  );

  return (
    <section
      style={{
        marginTop: 20,
        border: "1px solid #1f2937",
        borderRadius: 8,
        padding: 16,
      }}
    >
      <h2 style={{ fontSize: 15, margin: "0 0 10px" }}>Pipeline progress</h2>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {[...STAGES, ...extraStages].map((stage) => {
          const status: StageStatus = statuses[stage] ?? "pending";
          const chip = CHIP_COLORS[status];
          return (
            <div
              key={stage}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                fontSize: 13,
              }}
            >
              <span
                style={{
                  color: status === "pending" ? "#5b6472" : "#e7ecf3",
                }}
              >
                {stage}
              </span>
              <span
                style={{
                  fontSize: 11,
                  padding: "2px 10px",
                  borderRadius: 999,
                  background: chip.bg,
                  color: chip.fg,
                }}
              >
                {status}
              </span>
            </div>
          );
        })}
      </div>
      {lastNote && (
        <p style={{ fontSize: 12, color: "#8b93a3", margin: "10px 0 0" }}>
          {lastNote}
        </p>
      )}
      {connError && (
        <p style={{ fontSize: 12, color: "#f5c97b", margin: "10px 0 0" }}>
          Event stream interrupted — progress may be stale.
        </p>
      )}
    </section>
  );
}
