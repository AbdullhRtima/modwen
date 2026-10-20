import { useEffect, useRef, useState } from "react";
import { useServerEvents } from "../events";
import { api, fmtTs, LiveLine } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import { useI18n } from "../i18n";

export default function Live({ ctx }: { ctx: Ctx }) {
  const { t } = useI18n();
  const r = ctx.recorder;
  const [lines, setLines] = useState<LiveLine[]>([]);
  const [follow, setFollow] = useState(true);
  const [livePaused, setLivePaused] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (r.meeting_id) api.live(r.meeting_id).then((d) => setLines(d.lines), () => undefined);
  }, [r.meeting_id]);
  useServerEvents(ctx, (e) => {
    if (e.type === "live_line" && e.meeting_id === r.meeting_id) setLines((prev) => [...prev.slice(-499), e.line as LiveLine]);
    if (e.type === "live_status") setLivePaused(Boolean(e.paused));
    if (e.type === "resync" && r.meeting_id) api.live(r.meeting_id).then((d) => setLines(d.lines), () => undefined);
  });
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  if (!r.recording) {
    return <div className="empty"><h2>{t("notRecording")}</h2><p><a href="#/start">{t("record")}</a> · <a href="#/">{t("back")}</a></p></div>;
  }
  const who = (l: LiveLine) => (l.channel === "mic" ? t("you") : l.channel === "room" ? t("room") : t("call"));

  return (
    <>
      <div className="page-head"><h1>{t("liveTitle")}</h1>
        <span className="faint" style={{ fontSize: 12.5 }}>{Object.values(r.sources ?? {}).join(" · ")}</span></div>
      {r.live?.error && <div className="banner warn"><I.warn />{r.live.error}</div>}
      <div className={`banner ${livePaused ? "warn" : "info"}`}>{livePaused ? <I.warn /> : <I.info />}{livePaused ? t("livePaused") : t("liveDraft")}</div>
      <section className="card live-stage" aria-live="polite">
        <div className="live-box" ref={box} onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        }}>
          {lines.length === 0 && <p className="faint">{t("listening")}</p>}
          {lines.map((l, i) => (
            <div key={i} className="cap">
              <span className="w" style={{ color: l.channel === "mic" ? "var(--sp1)" : "var(--sp2)" }}>{who(l)}<span className="ts">{fmtTs(l.start_ms)}</span></span>
              <span className="x" dir="auto">{l.text}</span>
            </div>
          ))}
        </div>
        {!follow && <button className="btn sm" style={{ marginTop: 12 }} onClick={() => setFollow(true)}><I.down size={14} />{t("jumpLatest")}</button>}
      </section>
    </>
  );
}
