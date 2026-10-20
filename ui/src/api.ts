// Thin client for the local Modwen API (same origin, session cookie).

export type Meeting = {
  id: string;
  title: string;
  status: "recording" | "paused" | "processing" | "failed" | "ready" | "discarded";
  mode: string;
  platform: string;
  meeting_language: string;
  summary_language: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number;
  channels: string[];
  stt_info: { engine?: string; diarizer?: string } | null;
  llm_profile: string | null;
  hardware_tier: string | null;
  bookmarks: number[];
  progress: Progress | Record<string, never>;
  error: string | null;
  opened_at: string | null;
  action_item_count?: number;
  languages?: string[];
  speaker_count?: number;
};

export type Progress = {
  step: string;
  steps: Record<string, "pending" | "running" | "done" | "failed">;
  transcribe: number;
  notes_part: number;
  notes_parts: number;
  overall?: number;
  eta_seconds: number | null;
};

export type Speaker = { id: string; label: string; display_name: string; channel: string; is_self: number;
  name_source: string | null; confidence: number; person_id: string | null;
  suggestion: { person_id: string; name: string; score: number } | null };
export type VoiceResult = { saved: boolean; seconds: number; reason?: string } | null;

export type Segment = { id: string; seq: number; speaker_id: string; channel: string; start_ms: number;
  end_ms: number; text: string; language: string; confidence: number; overlap: number; edited: number;
  speaker_name: string | null; uncertain?: number };

export type Point = { id: string; text: string; original_text: string; evidence_segment_ids: string[];
  confidence: number };
export type ActionItem = { id: string; task: string; task_original: string; owner: string | null;
  due_date: string | null; due_raw: string | null; evidence_segment_ids: string[]; confidence: number };
export type Question = { id: string; question: string; question_original: string; evidence_segment_ids: string[];
  confidence: number };
export type NotesData = { summary: Record<string, string>; key_points: Point[]; decisions: Point[];
  action_items: ActionItem[]; open_questions: Question[]; warnings: string[]; parts?: number;
  meeting_date?: string };
export type Notes = { id: string; version: number; llm_profile: string; model: string; prompt_version: string;
  data: NotesData; generated_at: string };

export type RecorderStatus = { recording: boolean; paused?: boolean; meeting_id?: string; elapsed_ms?: number;
  levels?: Record<string, number>; warnings?: Record<string, string>; bookmarks?: number[];
  sources?: Record<string, string>; live?: { enabled: boolean; paused: boolean; error: string | null } };

export type LiveLine = { channel: string; speaker: string; start_ms: number; text: string; language: string };

const headers: Record<string, string> = { "Content-Type": "application/json" };
const envToken = (import.meta as unknown as { env: Record<string, string> }).env?.VITE_MODWEN_TOKEN;
if (envToken) headers["x-modwen-token"] = envToken;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = j.detail ?? msg;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg);
  }
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  status: () => req<{ recorder: RecorderStatus; tier: string; llm_active: string; summary_language: string;
    version: string; jobs_pending: number; setup_complete: boolean }>("GET", "/api/status"),
  hardware: () => req<{ hardware: Record<string, unknown>; plan: Record<string, unknown> & { tier: string;
    label: string; llama_server_command: string; est_2h_minutes: [number, number]; warnings: string[] };
    chosen: { backend: string; live_model: string; final_model: string } }>("GET", "/api/hardware"),
  devices: () => req<{ inputs: { index: number; name: string; default: boolean }[] }>("GET", "/api/devices"),
  meetings: () => req<{ meetings: Meeting[] }>("GET", "/api/meetings"),
  meeting: (id: string) => req<{ meeting: Meeting; speakers: Speaker[]; segments: Segment[];
    notes: Notes | null }>("GET", `/api/meetings/${id}`),
  start: (b: { title: string; mode: string; meeting_language: string; sources: string[];
    consent_confirmed: boolean }) => req<Meeting>("POST", "/api/meetings/start", b),
  stop: () => req<{ meeting_id: string }>("POST", "/api/recorder/stop"),
  pause: () => req<RecorderStatus>("POST", "/api/recorder/pause"),
  resume: () => req<RecorderStatus>("POST", "/api/recorder/resume"),
  mark: () => req<{ at_ms: number }>("POST", "/api/recorder/mark"),
  live: (id: string) => req<{ lines: LiveLine[] }>("GET", `/api/meetings/${id}/live`),
  retry: (id: string) => req("POST", `/api/meetings/${id}/retry`),
  regenerate: (id: string, b: { summary_language?: string; llm_profile?: string; scope?: "all" | "summary" }) =>
    req("POST", `/api/meetings/${id}/regenerate`, b),
  rename: (id: string, sid: string, name: string) =>
    req<{ old: string; new: string }>("POST", `/api/meetings/${id}/speakers/${sid}/rename`, { name }),
  merge: (id: string, sid: string, into: string) =>
    req<{ into: string; speakers: Speaker[] }>("POST", `/api/meetings/${id}/speakers/${sid}/merge`, { into }),
  redetect: (id: string, num_speakers: number | null) =>
    req("POST", `/api/meetings/${id}/speakers/redetect`, { num_speakers }),
  linkPerson: (id: string, sid: string, b: { person_id?: string; name?: string; remember_voice: boolean }) =>
    req<{ speaker_id: string; voice: VoiceResult; speakers: Speaker[] }>("POST", `/api/meetings/${id}/speakers/${sid}/person`, b),
  confirmVoice: (id: string, sid: string) =>
    req<{ voice: VoiceResult; speakers: Speaker[] }>("POST", `/api/meetings/${id}/speakers/${sid}/confirm`),
  rejectVoice: (id: string, sid: string) => req<{ speakers: Speaker[] }>("POST", `/api/meetings/${id}/speakers/${sid}/reject`),
  setLineSpeaker: (id: string, segId: string, speaker_id: string) =>
    req<{ speaker_id: string; speakers: Speaker[] }>("POST", `/api/meetings/${id}/segments/${segId}/speaker`, { speaker_id }),
  editSegment: (id: string, segId: string, text: string) => req("PUT", `/api/meetings/${id}/segments/${segId}`, { text }),
  editNotes: (id: string, data: Partial<NotesData>) => req<Notes>("PUT", `/api/meetings/${id}/notes`, { data }),
  remove: (id: string) => req("DELETE", `/api/meetings/${id}`),
  markdown: (id: string, opts: { language?: string; actions_only?: boolean } = {}) => {
    const q = new URLSearchParams();
    if (opts.language) q.set("language", opts.language);
    if (opts.actions_only) q.set("actions_only", "true");
    return req<string>("GET", `/api/meetings/${id}/export.md?${q}`);
  },
  profiles: () => req<{ active: string; fallback: string[]; profiles: Record<string, { base_url: string;
    model: string; context_window: number; structured_output: string; temperature: number }> }>(
    "GET", "/api/llm/profiles"),
  testLlm: (profile?: string) => req<{ ok: boolean; reply?: string; error?: string; seconds?: number;
    models?: string[]; base_url: string }>("POST", "/api/llm/test", { profile }),
  setActive: (profile: string) => req("POST", "/api/llm/active", { profile }),
  importFile: async (file: File, title: string, language: string) => {
    // The file is the raw body: the server streams it into an encrypted temp file (form uploads would be
    // spooled to disk unencrypted).
    const q = new URLSearchParams({ title, meeting_language: language });
    const res = await fetch(`/api/meetings/import?${q}`, { method: "POST", body: file, credentials: "same-origin",
      headers: { "content-type": file.type || "application/octet-stream", ...(envToken ? { "x-modwen-token": envToken } : {}) } });
    if (!res.ok) throw new ApiError(res.status, (await res.json()).detail ?? res.statusText);
    return (await res.json()) as Meeting;
  },
};

export type Job = { id: string; kind: string; title: string; status: "running" | "done" | "failed"; done_bytes: number;
  total_bytes: number; fraction: number; step: string; error: string | null };

export type SetupStatus = {
  setup_complete: boolean;
  hardware: Record<string, unknown> & { ram_gb: number; free_disk_gb: number; cpu: string; os: string };
  plan: { tier: string; label: string; llm_model: string; llm_context: number; est_2h_minutes: [number, number];
    warnings: string[]; llama_server_command: string; final_model: string; live_model: string; whisper_backend: string };
  whisper: { backend: string; models: { name: string; role: string; present: boolean }[] };
  llm: { active: string; online: boolean; health: { ok: boolean; models?: string[]; error?: string };
    managed: { enabled: boolean; model_choice: string; binary_installed: boolean; model_installed: boolean;
      running: boolean; port: number; port_busy_external: boolean; log: string };
    recommended: string; choices: { id: string; file: string; size_gb: number; context: number }[];
    default_variant: string };
  config_path: string | null;
  jobs: Job[];
};

export type PersonT = { id: string; display_name: string; display_name_ar: string | null; email: string | null;
  is_self: number; consent_at: string | null;
  voice: { samples: number; seconds: number; kinds: Record<string, number> } | null };

export const setup = {
  status: () => req<SetupStatus>("GET", "/api/setup/status"),
  jobs: () => req<{ jobs: Job[] }>("GET", "/api/jobs"),
  downloadWhisper: () => req<{ job: Job | null; message?: string }>("POST", "/api/setup/whisper/download"),
  installManaged: (model_choice: string, variant?: string) =>
    req<{ job: Job }>("POST", "/api/setup/llm/managed/install", { model_choice, variant }),
  startManaged: () => req("POST", "/api/setup/llm/managed/start"),
  stopManaged: () => req("POST", "/api/setup/llm/managed/stop"),
  useExternal: (b: { base_url: string; model?: string; api_key?: string; context_window?: number; name?: string }) =>
    req<{ profile: string; model: string; models: string[]; base_url: string }>("POST", "/api/setup/llm/external", b),
  audioTest: () => req<{ levels: Record<string, number>; errors: Record<string, string>;
    sources: Record<string, string> }>("POST", "/api/setup/audio-test?seconds=3"),
  notifyTest: () => req("POST", "/api/notify/test"),
  complete: () => req("POST", "/api/setup/complete"),
  settings: () => req<{ settings: Record<string, unknown>; config_path: string | null }>("GET", "/api/settings"),
  saveSettings: (patch: Record<string, unknown>) => req<{ settings: Record<string, unknown> }>("PUT", "/api/settings", patch),
  saveProfile: (name: string, b: { base_url: string; model: string; api_key?: string; context_window: number;
    temperature: number; structured_output: string; thinking: boolean }) => req("PUT", `/api/llm/profiles/${name}`, b),
  deleteProfile: (name: string) => req("DELETE", `/api/llm/profiles/${name}`),
  network: () => req<{ lan_access_setting: boolean; lan_active: boolean; urls: string[]; restart_needed: boolean }>(
    "GET", "/api/network"),
  people: () => req<{ people: PersonT[] }>("GET", "/api/people"),
  addPerson: (b: { display_name: string; display_name_ar?: string | null; email?: string | null }) => req<PersonT>("POST", "/api/people", b),
  editPerson: (id: string, b: { display_name: string; display_name_ar?: string | null; email?: string | null }) =>
    req<PersonT & { meetings: number }>("PUT", `/api/people/${id}`, b),
  deletePerson: (id: string) => req<{ meetings: number }>("DELETE", `/api/people/${id}`),
  thisIsMe: (id: string) => req<PersonT>("POST", `/api/people/${id}/self`),
  deleteVoiceprint: (id: string) => req("DELETE", `/api/people/${id}/voiceprint`),
};

export const fmtGB = (bytes: number) => `${(bytes / 1e9).toFixed(bytes > 1e9 ? 1 : 2)} GB`;

export type BBEvent = { type: string; [k: string]: unknown };

export function connectEvents(onEvent: (e: BBEvent) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 500;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const tokenQ = envToken ? `?token=${encodeURIComponent(envToken)}` : "";
    ws = new WebSocket(`${proto}://${location.host}/ws/events${tokenQ}`);
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch {
        /* ignore */
      }
    };
    ws.onopen = () => (retry = 500);
    ws.onclose = () => {
      if (!closed) setTimeout(open, (retry = Math.min(retry * 2, 8000)));
    };
  };
  open();
  return () => {
    closed = true;
    ws?.close();
  };
}

export const fmtTs = (ms: number) => {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return [h, m, sec].map((n) => String(n).padStart(2, "0")).join(":");
};

export const fmtDuration = (ms: number) => {
  if (ms < 60000) return `${Math.round(ms / 1000)} s`;
  const m = Math.round(ms / 60000);
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m} min`;
};
