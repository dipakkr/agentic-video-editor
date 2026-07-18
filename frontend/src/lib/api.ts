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

export interface EdlBrief {
  platform: string;
  target_duration_s: number;
  tone: string;
}

export interface Edl {
  version: number;
  timeline: Segment[];
  music: EdlMusic;
  captions: EdlCaptions;
  brief: EdlBrief;
  output?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// API payload types
// ---------------------------------------------------------------------------

export interface CreateProjectBody {
  platform: string;
  target_duration_s: number;
  tone: string;
  music_track_id?: string | null;
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

export function eventsUrl(pid: string): string {
  return `${API_BASE}/projects/${pid}/events`;
}

export function latestRenderUrl(pid: string): string {
  return `${API_BASE}/projects/${pid}/renders/latest`;
}
