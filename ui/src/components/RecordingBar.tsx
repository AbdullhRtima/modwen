import { useEffect, useState } from "react";
import { api, fmtTs } from "../api";
import type { Ctx } from "../App";
import { openPill } from "../pages/Pill";

export default function RecordingBar({ ctx, compact = false }: { ctx: Ctx; compact?: boolean }) {
  const r = ctx.recorder;
  const [elapsed, setElapsed] = useState(r.elapsed_ms ?? 0);
  useEffect(() => {
    setElapsed(r.elapsed_ms ?? 0);
    if (r.paused) return;
    const t = window.setInterval(() => setElapsed((e) => e + 1000), 1000);
    return () => window.clearInterval(t);
  }, [r.elapsed_ms, r.paused]);

  const warnings = Object.values(r.warnings ?? {});
  const stop = async () => {
    const { meeting_id } = await api.stop();
    ctx.refreshStatus();
    ctx.go(`#/meetings/${meeting_id}`);
  };
  const levels = r.levels ?? {};
  return (
    <div className={`recbar ${warnings.length ? "warn" : ""}`}>
      <span className="badge rec"><span className={`dot ${r.paused ? "" : "pulse"}`} />{r.paused ? "PAUSED" : "REC"}</span>
      <span className="timer">{fmtTs(elapsed)}</span>
      {"mic" in levels && <span className="meter">You <span className="bar"><i style={{ width: `${levels.mic * 100}%` }} /></span></span>}
      {"system" in levels && <span className="meter others">Others <span className="bar"><i style={{ width: `${levels.system * 100}%` }} /></span></span>}
      {"room" in levels && <span className="meter">Room <span className="bar"><i style={{ width: `${levels.room * 100}%` }} /></span></span>}
      <span className="lock">🔒 Local only</span>
      <span style={{ flex: 1 }} />
      {warnings.map((w) => <span key={w} className="badge warn">⚠ {w}</span>)}
      <button className="btn sm" title="Mark moment (bookmark)" onClick={() => api.mark().then(() => ctx.toast("Moment marked"))}>★ Mark</button>
      {r.paused
        ? <button className="btn sm" onClick={() => api.resume().then(ctx.refreshStatus)}>▶ Resume</button>
        : <button className="btn sm" onClick={() => api.pause().then(ctx.refreshStatus)}>❚❚ Pause</button>}
      <button className="btn danger sm" onClick={stop}>■ Stop</button>
      <button className="btn sm" title="Open the small floating recording pill" onClick={() => openPill()}>⧉ Pill</button>
      {compact && <a className="btn sm" href="#/live">⤢ Live</a>}
    </div>
  );
}
