// Typed client for the AVE FastAPI backend (backend/ave/api/app.py).
// All fetch helpers throw an Error carrying the response text on non-2xx.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// ---------------------------------------------------------------------------
// EDL types (mirrors ave.edl.schema as serialized by the API)
// ---------------------------------------------------------------------------

export interface Segment {
  id: string;
  source_clip: string;
  in: number;
  out: number;
  speed: number;
  transition_in: string;
  cut_snapped_to_beat: boolean;
  snapped_beat_s: number | null;
  reason: string;
}

export interface SyncPoint {
  beat_s: number;
  timeline_s: number;
  is_downbeat: boolean;
}

export interface EdlMusic {
  track_id: string | null;
  offset_s: number;
  sync_map: SyncPoint[];
}

export interface EdlCaptions {
  style: string;
}

/** Per-agent customization knobs (Brief.agent_config, EDL schema v1.1). */
export interface AgentConfig {
  transition_style?: string | null;
  music_genre_pin?: string | null;
  duck_db?: number | null;
  caption_style?: string | null;
  target_lufs?: number | null;
  enable_broll?: boolean;
  enable_graphics?: boolean;
}

export interface EdlBrief {
  platform: string;
  target_duration_s: number;
  tone: string;
  agent_config?: AgentConfig;
}

/** B-roll cutaway laid over the primary timeline (EDL v1.1). */
export interface Overlay {
  id: string;
  source_clip: string;
  in: number;
  out: number;
  timeline_start_s: number;
  mute: boolean;
  reason: string;
}

export interface TitleCard {
  text: string;
  start_s: number;
  duration_s: number;
  style: string;
}

export interface LowerThird {
  text: string;
  start_s: number;
  duration_s: number;
  position: string;
}

export interface GraphicsSpec {
  title_card?: TitleCard | null;
  lower_thirds?: LowerThird[];
}

export interface Edl {
  version: number;
  timeline: Segment[];
  overlays?: Overlay[];
  graphics?: GraphicsSpec;
  music: EdlMusic;
  captions: EdlCaptions;
  brief: EdlBrief;
  output?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// QC / release kit / export types (M5 endpoints — may not exist yet)
// ---------------------------------------------------------------------------

export interface QcCheckResult {
  check: string;
  passed: boolean;
  details: string;
  responsible_agent: string;
}

export interface QcReport {
  results: QcCheckResult[];
  passed: boolean;
  failures_by_agent: Record<string, string[]>;
}

export interface ReleaseChapter {
  time_s: number;
  label: string;
}

export interface ThumbnailCandidate {
  clip_id: string;
  source_time_s: number;
  timeline_s: number;
  score: number;
  reason: string;
}

export interface ReleaseKit {
  titles: string[];
  description: string;
  hashtags: string[];
  chapters: ReleaseChapter[];
  thumbnails: ThumbnailCandidate[];
}

export interface ExportPresetResult {
  status?: string;
  path?: string;
  error?: string;
  [key: string]: unknown;
}

export interface ExportResponse {
  results: Record<string, ExportPresetResult>;
}

// ---------------------------------------------------------------------------
// API payload types
// ---------------------------------------------------------------------------

export interface CreateProjectBody {
  platform: string;
  target_duration_s: number;
  tone: string;
  music_track_id?: string | null;
  agent_config?: AgentConfig;
}

export interface CreateProjectResponse {
  project_id: string;
  brief: Record<string, unknown>;
}

export interface UploadClipResponse {
  stored: string;
  bytes: number;
}

export interface RunResponse {
  project_id: string;
  edl_version: number | null;
  render: Record<string, unknown> | null;
}

export interface FeedbackResponse {
  project_id: string;
  edl_version: number | null;
}

export interface ProgressEvent {
  stage: string;
  status: string;
  data: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

async function ensureOk(res: Response): Promise<Response> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await ensureOk(await fetch(`${API_BASE}${path}`));
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await ensureOk(
    await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// API surface
// ---------------------------------------------------------------------------

export function createProject(
  body: CreateProjectBody
): Promise<CreateProjectResponse> {
  return postJson<CreateProjectResponse>("/projects", body);
}

export async function uploadClip(
  pid: string,
  file: File
): Promise<UploadClipResponse> {
  const fd = new FormData();
  fd.append("file", file);
  const res = await ensureOk(
    await fetch(`${API_BASE}/projects/${pid}/clips`, {
      method: "POST",
      body: fd,
    })
  );
  return (await res.json()) as UploadClipResponse;
}

export async function runPipeline(pid: string): Promise<RunResponse> {
  const res = await ensureOk(
    await fetch(`${API_BASE}/projects/${pid}/run`, { method: "POST" })
  );
  return (await res.json()) as RunResponse;
}

export function getEdl(pid: string): Promise<Edl> {
  return getJson<Edl>(`/projects/${pid}/edl`);
}

/**
 * Versions endpoints are being added in parallel — callers must treat failures
 * (404 or network) as "feature unavailable". Without `v`, returns the list of
 * available version numbers; with `v`, returns that EDL version.
 */
export function getEdlVersions(pid: string): Promise<{ versions: number[] }>;
export function getEdlVersions(pid: string, v: number): Promise<Edl>;
export function getEdlVersions(
  pid: string,
  v?: number
): Promise<{ versions: number[] } | Edl> {
  if (v === undefined) {
    return getJson<{ versions: number[] }>(`/projects/${pid}/edl/versions`);
  }
  return getJson<Edl>(`/projects/${pid}/edl/versions/${v}`);
}

export function sendFeedback(
  pid: string,
  note: string
): Promise<FeedbackResponse> {
  return postJson<FeedbackResponse>(`/projects/${pid}/feedback`, { note });
}

/**
 * QC / release-kit / export endpoints are being added in parallel — callers
 * must treat failures (404 or network) as "feature unavailable" and hide the
 * corresponding UI.
 */
export function getQcReport(pid: string): Promise<QcReport> {
  return getJson<QcReport>(`/projects/${pid}/qc`);
}

export function getReleaseKit(pid: string): Promise<ReleaseKit> {
  return getJson<ReleaseKit>(`/projects/${pid}/release`);
}

export function exportProject(
  pid: string,
  presets: string[]
): Promise<ExportResponse> {
  return postJson<ExportResponse>(`/projects/${pid}/export`, { presets });
}

export function eventsUrl(pid: string): string {
  return `${API_BASE}/projects/${pid}/events`;
}

export function latestRenderUrl(pid: string): string {
  return `${API_BASE}/projects/${pid}/renders/latest`;
}
