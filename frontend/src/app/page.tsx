"use client";

// M3 web UI: brief form → clip uploads → pipeline run with live SSE progress →
// timeline + preview + natural-language feedback + version history.

import { useCallback, useRef, useState } from "react";
import {
  API_BASE,
  createProject,
  getEdl,
  latestRenderUrl,
  runPipeline,
  uploadClip,
  type AgentConfig,
  type Edl,
} from "@/lib/api";
import { Timeline } from "@/components/Timeline";
import { ProgressStream } from "@/components/ProgressStream";
import { FeedbackBox } from "@/components/FeedbackBox";
import { VersionHistory } from "@/components/VersionHistory";
import { SettingsPanel } from "@/components/SettingsPanel";
import { QcPanel } from "@/components/QcPanel";
import { ReleasePanel } from "@/components/ReleasePanel";

const PLATFORMS = ["youtube", "reels", "shorts", "tiktok"] as const;
// Must match the backend Tone enum (ave/edl/schema.py).
const TONES = ["energetic", "cinematic", "tutorial", "vlog"] as const;

type UploadStatus = "queued" | "uploading" | "done" | "error";

interface UploadItem {
  file: File;
  status: UploadStatus;
  error?: string;
}

type Phase = "idle" | "running" | "ready";

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

export default function Home() {
  // Brief form
  const [platform, setPlatform] = useState<string>("youtube");
  const [targetDuration, setTargetDuration] = useState<number>(30);
  const [tone, setTone] = useState<string>("energetic");
  const [agentConfig, setAgentConfig] = useState<AgentConfig | null>(null);

  // Uploads
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Pipeline
  const [phase, setPhase] = useState<Phase>("idle");
  const [projectId, setProjectId] = useState<string | null>(null);
  const [edl, setEdl] = useState<Edl | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [historyKey, setHistoryKey] = useState(0);
  const [videoFailed, setVideoFailed] = useState(false);

  function onFilesPicked(list: FileList | null) {
    if (!list?.length) return;
    const items: UploadItem[] = Array.from(list).map((file) => ({
      file,
      status: "queued",
    }));
    setUploads((prev) => [...prev, ...items]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function removeUpload(i: number) {
    setUploads((prev) => prev.filter((_, idx) => idx !== i));
  }

  const setUploadStatus = useCallback(
    (i: number, status: UploadStatus, err?: string) => {
      setUploads((prev) =>
        prev.map((u, idx) => (idx === i ? { ...u, status, error: err } : u))
      );
    },
    []
  );

  async function generate() {
    if (!uploads.length || phase === "running") return;
    setError(null);
    setEdl(null);
    setVideoFailed(false);
    setPhase("running");
    try {
      const { project_id } = await createProject({
        platform,
        target_duration_s: targetDuration,
        tone,
        ...(agentConfig ? { agent_config: agentConfig } : {}),
      });
      setProjectId(project_id);

      for (let i = 0; i < uploads.length; i++) {
        setUploadStatus(i, "uploading");
        try {
          await uploadClip(project_id, uploads[i].file);
          setUploadStatus(i, "done");
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          setUploadStatus(i, "error", msg);
          throw new Error(`upload failed for ${uploads[i].file.name}: ${msg}`);
        }
      }

      await runPipeline(project_id);
      const result = await getEdl(project_id);
      setEdl(result);
      setHistoryKey((k) => k + 1);
      setPhase("ready");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase(edl ? "ready" : "idle");
    }
  }

  const onRevised = useCallback((revised: Edl) => {
    setEdl(revised);
    setVideoFailed(false);
    setHistoryKey((k) => k + 1);
  }, []);

  const onSelectVersion = useCallback((selected: Edl) => {
    setEdl(selected);
  }, []);

  const canGenerate = uploads.length > 0 && phase !== "running";

  return (
    <main style={{ maxWidth: 960, margin: "0 auto", padding: 32 }}>
      <h1 style={{ fontSize: 28, marginBottom: 4 }}>🎬 Agentic Video Editor</h1>
      <p style={{ color: "#8b93a3", marginTop: 0 }}>
        Upload 2–20 clips. The agents analyze, plan, and render a rough cut —
        then revise it with plain-language feedback.
      </p>

      {/* Brief form */}
      <section
        style={{
          border: "1px solid #1f2937",
          borderRadius: 8,
          padding: 16,
          marginTop: 16,
        }}
      >
        <h2 style={{ fontSize: 15, margin: "0 0 12px" }}>Brief</h2>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          <label style={labelStyle}>
            Platform
            <select
              value={platform}
              onChange={(e) => setPlatform(e.target.value)}
              disabled={phase === "running"}
              style={inputStyle}
            >
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label style={labelStyle}>
            Target duration (s)
            <input
              type="number"
              min={5}
              max={600}
              value={targetDuration}
              onChange={(e) => setTargetDuration(Number(e.target.value) || 30)}
              disabled={phase === "running"}
              style={{ ...inputStyle, width: 120 }}
            />
          </label>
          <label style={labelStyle}>
            Tone
            <select
              value={tone}
              onChange={(e) => setTone(e.target.value)}
              disabled={phase === "running"}
              style={inputStyle}
            >
              {TONES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        </div>

        <SettingsPanel
          disabled={phase === "running"}
          onChange={setAgentConfig}
        />

        {/* Clip picker */}
        <div style={{ marginTop: 16 }}>
          <label
            style={{
              display: "inline-block",
              padding: "8px 14px",
              background: "#1f2937",
              borderRadius: 6,
              cursor: phase === "running" ? "default" : "pointer",
              fontSize: 13,
            }}
          >
            + Add clips
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              multiple
              hidden
              disabled={phase === "running"}
              onChange={(e) => onFilesPicked(e.target.files)}
            />
          </label>

          {uploads.length > 0 && (
            <ul style={{ listStyle: "none", margin: "12px 0 0", padding: 0 }}>
              {uploads.map((u, i) => (
                <li
                  key={`${u.file.name}-${i}`}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    fontSize: 13,
                    padding: "4px 0",
                  }}
                >
                  <span
                    style={{
                      fontSize: 11,
                      padding: "2px 8px",
                      borderRadius: 999,
                      background:
                        u.status === "done"
                          ? "rgba(34,197,94,0.25)"
                          : u.status === "uploading"
                            ? "rgba(59,130,246,0.25)"
                            : u.status === "error"
                              ? "rgba(239,68,68,0.25)"
                              : "#1f2937",
                      color:
                        u.status === "done"
                          ? "#8fe0ae"
                          : u.status === "uploading"
                            ? "#9fc1f7"
                            : u.status === "error"
                              ? "#f87171"
                              : "#8b93a3",
                      minWidth: 64,
                      textAlign: "center",
                    }}
                  >
                    {u.status}
                  </span>
                  <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {u.file.name}
                    <span style={{ color: "#5b6472" }}>
                      {" "}
                      · {(u.file.size / 1e6).toFixed(1)} MB
                    </span>
                  </span>
                  {u.error && (
                    <span style={{ color: "#f87171", fontSize: 11 }}>
                      {u.error}
                    </span>
                  )}
                  {phase !== "running" && u.status === "queued" && (
                    <button
                      onClick={() => removeUpload(i)}
                      style={{
                        background: "none",
                        border: "none",
                        color: "#8b93a3",
                        cursor: "pointer",
                        fontSize: 13,
                      }}
                      aria-label={`remove ${u.file.name}`}
                    >
                      ✕
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <button
          onClick={generate}
          disabled={!canGenerate}
          style={{
            marginTop: 16,
            padding: "10px 20px",
            background: canGenerate ? "#3b82f6" : "#1f2937",
            color: canGenerate ? "#e7ecf3" : "#5b6472",
            border: "none",
            borderRadius: 8,
            fontSize: 14,
            fontWeight: 600,
            cursor: canGenerate ? "pointer" : "default",
          }}
        >
          {phase === "running" ? "Generating…" : "Generate"}
        </button>
        {error && (
          <p style={{ fontSize: 12, color: "#f87171", margin: "10px 0 0" }}>
            {error}
          </p>
        )}
      </section>

      {projectId && (
        <p style={{ color: "#5b6472", fontSize: 12, marginTop: 12 }}>
          Project <code>{projectId}</code> · API {API_BASE}
        </p>
      )}

      {projectId && phase === "running" && <ProgressStream pid={projectId} />}

      {edl && projectId && (
        <>
          <Timeline edl={edl} />

          {/* Preview */}
          <section style={{ marginTop: 24 }}>
            <h2 style={{ fontSize: 15, margin: "0 0 8px" }}>Preview</h2>
            {videoFailed ? (
              <div
                style={{
                  border: "1px dashed #1f2937",
                  borderRadius: 8,
                  padding: 32,
                  textAlign: "center",
                  color: "#5b6472",
                  fontSize: 13,
                }}
              >
                Render not available yet — the preview will appear here once the
                render endpoint serves this project.
              </div>
            ) : (
              <video
                key={`${projectId}-v${edl.version}`}
                controls
                src={latestRenderUrl(projectId)}
                onError={() => setVideoFailed(true)}
                style={{
                  width: "100%",
                  borderRadius: 8,
                  border: "1px solid #1f2937",
                  background: "#000",
                }}
              />
            )}
          </section>

          <QcPanel pid={projectId} refreshKey={historyKey} />
          <ReleasePanel pid={projectId} refreshKey={historyKey} />

          <FeedbackBox pid={projectId} onRevised={onRevised} />
          <VersionHistory
            pid={projectId}
            currentVersion={edl.version}
            refreshKey={historyKey}
            onSelect={onSelectVersion}
          />
        </>
      )}
    </main>
  );
}
