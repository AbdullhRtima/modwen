import { useEffect, useState } from "react";
import { api, fmtTs } from "../api";
import type { Ctx } from "../App";

// Compact floating recording pill, opened in its own small browser window (#/pill).
// In v1 (Tauri) the same page becomes an always-on-top native window.
export default function Pill({ ctx }: { ctx: Ctx }) {
  const r = ctx.recorder;
  const [elapsed, setElapsed] = useState(r.elapsed_ms ?? 0);

  useEffect(() => {
    document.title = r.recording ? "● REC · Modwen" : "Modwen";
  }, [r.recording]);
  useEffect(() => {
    setElapsed(r.elapsed_ms ?? 0);
    if (!r.recording || r.paused) return;
    const t = window.setInterval(() => setElapsed((e) => e + 1000), 1000);
    return () => window.clearInterval(t);
  }, [r.elapsed_ms, r.paused, r.recording]);

  const openMain = (hash: string) => {
    if (window.opener && !window.opener.closed) {
      window.opener.location.hash = hash;
      window.opener.focus();
    } else {
      window.open(`${location.origin}/${hash}`, "modwen-main");
    }
  };
  const stop = async () => {
    const { meeting_id } = await api.stop();
    openMain(`#/meetings/${meeting_id}`);
    window.close();
  };

  if (!r.recording) {
    return (
      <div className="pill-page">
        <div className="pill">
          <span className="muted">Not recording.</span>
          <span style={{ flex: 1 }} />
          <button className="btn sm" onClick={() => openMain("#/start")}>● Start…</button>
          <button className="btn sm ghost" onClick={() => window.close()}>✕</button>
        </div>
      </div>
    );
  }

  const levels = r.levels ?? {};
  const warnings = Object.values(r.warnings ?? {});
  const meter = (label: string, key: string, cls = "") =>
    key in levels ? (
      <span className={`meter ${cls}`}>{label}<span className="bar"><i style={{ width: `${levels[key] * 100}%` }} /></span></span>
    ) : null;

  return (
    <div className="pill-page">
      <div className={`pill ${warnings.length ? "warn" : ""}`}>
        <div className="pill-row">
          <span className="badge rec"><span className={`dot ${r.paused ? "" : "pulse"}`} />{r.paused ? "PAUSED" : "REC"}</span>
          <span className="timer">{fmtTs(elapsed)}</span>
          {meter("You", "mic")}
          {meter("Others", "system", "others")}
          {meter("Room", "room")}
        </div>
        <div className="pill-row">
          {warnings.length ? <span className="badge warn" title={warnings.join(" · ")}>⚠ {warnings[0]}</span>
            : <span className="lock">🔒 Local only</span>}
          <span style={{ flex: 1 }} />
          <button className="btn sm" title="Mark moment" onClick={() => api.mark()}>★</button>
          {r.paused
            ? <button className="btn sm" title="Resume" onClick={() => api.resume().then(ctx.refreshStatus)}>▶</button>
            : <button className="btn sm" title="Pause" onClick={() => api.pause().then(ctx.refreshStatus)}>❚❚</button>}
          <button className="btn danger sm" onClick={stop}>■ Stop</button>
          <button className="btn sm" title="Open the live transcript" onClick={() => openMain("#/live")}>⤢</button>
        </div>
      </div>
    </div>
  );
}

export function openPill(): Window | null {
  return window.open(`${location.origin}/#/pill`, "modwen-pill",
    "popup=yes,width=470,height=150,left=40,top=40,resizable=yes");
}
