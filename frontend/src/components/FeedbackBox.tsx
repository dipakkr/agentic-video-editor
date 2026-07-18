"use client";

// Natural-language feedback loop: submit a note, backend revises the EDL,
// we refetch it and hand the new version to the parent.

import { useState } from "react";
import { getEdl, sendFeedback, type Edl } from "@/lib/api";

export function FeedbackBox({
  pid,
  onRevised,
}: {
  pid: string;
  onRevised: (edl: Edl) => void;
}) {
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const trimmed = note.trim();
    if (!trimmed || pending) return;
    setPending(true);
    setError(null);
    try {
      await sendFeedback(pid, trimmed);
      const edl = await getEdl(pid);
      onRevised(edl);
      setNote("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
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
      <h2 style={{ fontSize: 15, margin: "0 0 8px" }}>Revise with feedback</h2>
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        disabled={pending}
        rows={3}
        placeholder='e.g. "make the intro punchier and cut the second clip shorter"'
        style={{
          width: "100%",
          boxSizing: "border-box",
          background: "#101420",
          color: "#e7ecf3",
          border: "1px solid #1f2937",
          borderRadius: 6,
          padding: 10,
          fontSize: 13,
          fontFamily: "inherit",
          resize: "vertical",
        }}
      />
      <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 12 }}>
        <button
          onClick={submit}
          disabled={pending || !note.trim()}
          style={{
            padding: "8px 16px",
            background: pending || !note.trim() ? "#1f2937" : "#3b82f6",
            color: pending || !note.trim() ? "#5b6472" : "#e7ecf3",
            border: "none",
            borderRadius: 6,
            fontSize: 13,
            fontWeight: 600,
            cursor: pending || !note.trim() ? "default" : "pointer",
          }}
        >
          {pending ? "Revising…" : "Send feedback"}
        </button>
        {error && (
          <span style={{ fontSize: 12, color: "#f87171" }}>{error}</span>
        )}
      </div>
    </section>
  );
}
