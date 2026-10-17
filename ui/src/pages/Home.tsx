import { useEffect, useRef, useState } from "react";
import { api, fmtDuration, Meeting } from "../api";
import type { Ctx } from "../App";

const STATUS: Record<string, [string, string]> = {
  recording: ["rec", "Recording"],
  paused: ["rec", "Paused"],
  processing: ["warn", "Processing"],
  ready: ["ok", "Notes ready"],
  failed: ["rec", "Failed: retry"],
  discarded: ["", "Discarded"],
};

export function StatusChip({ m }: { m: Meeting }) {
  const [cls, label] = STATUS[m.status] ?? ["", m.status];
  const pct = m.status === "processing" && m.progress && "overall" in m.progress && m.progress.overall
    ? ` ${Math.round(m.progress.overall * 100)}%` : "";
  const opened = m.status === "ready" && m.opened_at ? "Opened" : label;
  return <span className={`badge ${m.status === "ready" && m.opened_at ? "" : cls}`}>
    {m.status === "processing" && <span className="dot pulse" />}{opened}{pct}</span>;
}

export default function Home({ ctx }: { ctx: Ctx }) {
  const [meetings, setMeetings] = useState<Meeting[] | null>(null);
  const [q, setQ] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = () => api.meetings().then((r) => setMeetings(r.meetings));
  useEffect(() => {
    load();
  }, []);
  useEffect(() => {
    const t = ctx.lastEvent?.type;
    if (t && ["notes_ready", "failed", "recording_stopped", "recording_started", "status"].includes(t)) load();
    if (t === "progress") load();
  }, [ctx.lastEvent]);

  const upload = async (f: File) => {
    setUploading(true);
    try {
      const m = await api.importFile(f, f.name.replace(/\.[^.]+$/, ""), "auto");
      ctx.toast("Recording imported; processing locally");
      ctx.go(`#/meetings/${m.id}`);
    } catch (e) {
      alert(`Import failed: ${(e as Error).message}`);
    } finally {
      setUploading(false);
    }
  };

  const list = (meetings ?? []).filter((m) => m.title.toLowerCase().includes(q.toLowerCase()));
  const attention = list.filter((m) => m.status === "processing" || m.status === "failed"
    || (m.status === "ready" && !m.opened_at) || m.status === "recording");
  const recent = list.filter((m) => !attention.includes(m));

  const row = (m: Meeting) => (
    <div key={m.id} className={`mrow ${m.status === "ready" && !m.opened_at ? "hl" : ""}`}
      onClick={() => ctx.go(m.status === "recording" ? "#/live" : `#/meetings/${m.id}`)}>
      <div>
        <div className="t">{m.title}</div>
        <div className="meta">
          {m.started_at ? new Date(m.started_at).toLocaleString() : ""} · {fmtDuration(m.duration_ms || 0)}
          {(m.languages ?? []).length > 0 && <span className="badge acc">{(m.languages ?? []).map((l) => l.toUpperCase()).join(" + ")}</span>}
          {m.speaker_count ? <span>{m.speaker_count} speakers</span> : null}
          {m.action_item_count ? <span>{m.action_item_count} action items</span> : null}
          {m.platform === "import" && <span className="faint">imported</span>}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <StatusChip m={m} />
        {m.status === "failed" && <button className="btn sm" onClick={(e) => { e.stopPropagation(); api.retry(m.id).then(load); }}>Retry</button>}
      </div>
    </div>
  );

  return (
    <>
      <div className="page-head">
        <h1>Meetings</h1>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <input className="input" style={{ width: 240 }} placeholder="⌕ Search meetings…" value={q}
            onChange={(e) => setQ(e.target.value)} />
          <input ref={fileRef} type="file" accept="audio/*,.wav,.flac,.mp3,.ogg,.aiff" hidden
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          <button className="btn" disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? "Importing…" : "⇪ Import recording"}</button>
        </div>
      </div>
      {meetings === null && <p className="muted">Loading…</p>}
      {meetings !== null && meetings.length === 0 && (
        <div className="card">
          <b>No meetings yet.</b>
          <p className="muted">Press <b>● Start meeting</b> when your Google Meet or Teams call begins, or
            <b> Import recording</b> to try Modwen on an audio file (WAV, FLAC, MP3, OGG).</p>
        </div>
      )}
      {attention.length > 0 && <><div className="section-title">Needs attention ({attention.length})</div>{attention.map(row)}</>}
      {recent.length > 0 && <><div className="section-title">Recent</div>{recent.map(row)}</>}
    </>
  );
}
