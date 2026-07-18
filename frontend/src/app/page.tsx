"use client";

import { useState } from "react";
import { Timeline } from "@/components/Timeline";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// M1 UI: a thin driver over the API to prove the pipeline end-to-end. The full timeline
// editor, preview player, and streamed per-agent progress land in M3.
export default function Home() {
  const [projectId, setProjectId] = useState<string | null>(null);
  const [edl, setEdl] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  async function createAndRun(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    try {
      const res = await fetch(`${API}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform: "youtube", target_duration_s: 30, tone: "energetic" }),
      });
      const { project_id } = await res.json();
      setProjectId(project_id);

      for (const f of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", f);
        await fetch(`${API}/projects/${project_id}/clips`, { method: "POST", body: fd });
      }
      await fetch(`${API}/projects/${project_id}/run`, { method: "POST" });
      const edlRes = await fetch(`${API}/projects/${project_id}/edl`);
      setEdl(await edlRes.json());
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 960, margin: "0 auto", padding: 32 }}>
      <h1 style={{ fontSize: 28 }}>🎬 Agentic Video Editor</h1>
      <p style={{ opacity: 0.7 }}>Upload 2–20 clips. The agents analyze, plan, and render a rough cut.</p>

      <label style={{ display: "inline-block", padding: "10px 16px", background: "#3b82f6",
        borderRadius: 8, cursor: "pointer", marginTop: 12 }}>
        {busy ? "Working…" : "Upload clips & generate"}
        <input type="file" accept="video/*" multiple hidden disabled={busy}
          onChange={(e) => createAndRun(e.target.files)} />
      </label>

      {projectId && <p style={{ opacity: 0.6, marginTop: 16 }}>Project: <code>{projectId}</code></p>}
      {edl && <Timeline edl={edl} />}
    </main>
  );
}
