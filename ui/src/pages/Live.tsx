import { useEffect, useRef, useState } from "react";
import { api, fmtTs, LiveLine } from "../api";
import type { Ctx } from "../App";
import RecordingBar from "../components/RecordingBar";

export default function Live({ ctx }: { ctx: Ctx }) {
  const r = ctx.recorder;
  const [lines, setLines] = useState<LiveLine[]>([]);
  const [follow, setFollow] = useState(true);
  const [livePaused, setLivePaused] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (r.meeting_id) api.live(r.meeting_id).then((d) => setLines(d.lines));
  }, [r.meeting_id]);

  useEffect(() => {
    const e = ctx.lastEvent;
    if (!e) return;
    if (e.type === "live_line" && e.meeting_id === r.meeting_id) {
      setLines((prev) => [...prev.slice(-499), e.line as LiveLine]);
    }
    if (e.type === "live_status") setLivePaused(Boolean(e.paused));
  }, [ctx.lastEvent]);

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  if (!r.recording) {
    return (
      <div className="card">
        <b>Not recording.</b> <a href="#/start">Start a meeting</a> or go back to <a href="#/">Meetings</a>.
      </div>
    );
  }

  return (
    <>
      <RecordingBar ctx={ctx} />
      {r.live?.error && <div className="banner warn">⚠ {r.live.error}. Recording continues; notes are made after you stop.</div>}
      {livePaused
        ? <div className="banner warn">⚠ Live transcript paused because this computer is busy. Recording continues normally.</div>
        : <div className="banner info">ℹ Live text is a quick draft with "You / Others" labels. The accurate transcript with names is made after you stop.</div>}
      <div className="card">
        <div className="live-box" ref={box} onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        }}>
          {lines.length === 0 && <p className="faint">Listening… lines appear a few seconds after people speak.</p>}
          {lines.map((l, i) => (
            <div key={i} className="tline">
              <span className="ts">{fmtTs(l.start_ms)}</span>
              <span className="sp" style={{ color: l.channel === "mic" ? "var(--sp1)" : "var(--sp3)" }}>{l.speaker}</span>
              <span className="txt" dir="auto">{l.text}</span>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 10, gap: 8, flexWrap: "wrap" }}>
          <button className="btn" onClick={() => api.mark().then(() => ctx.toast("Moment marked"))}>★ Mark moment</button>
          {!follow && <button className="btn sm" onClick={() => setFollow(true)}>Jump to latest ↓</button>}
          <span className="faint" style={{ fontSize: 12.5 }}>
            Sources: {Object.entries(r.sources ?? {}).map(([k, v]) => `${k}: ${v}`).join(" · ")}</span>
        </div>
      </div>
    </>
  );
}
