import { useRef, useState } from "react";
import { api, fmtDuration, Meeting } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import { MeetingStatus, useMeetings } from "../components/Sidebar";
import { fmtDate, useI18n } from "../i18n";

/** Kept for pages that show a meeting's state next to its title. */
export function StatusChip({ m }: { m: Meeting }) {
  const { t } = useI18n();
  if (m.status === "ready") return <span className={`badge ${m.opened_at ? "" : "ok"}`}>{m.opened_at ? t("stReady") : t("stNew")}</span>;
  return <MeetingStatus m={m} />;
}

export default function Home({ ctx }: { ctx: Ctx }) {
  const { t, lang } = useI18n();
  const { meetings, reload } = useMeetings(ctx);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const upload = async (f: File) => {
    setUploading(true);
    try {
      const m = await api.importFile(f, f.name.replace(/\.[^.]+$/, ""), "auto");
      ctx.toast(t("imported"));
      ctx.go(`#/meetings/${m.id}`);
    } catch (e) {
      ctx.fail(e);
    } finally {
      setUploading(false);
    }
  };

  const list = meetings ?? [];
  const attention = list.filter((m) => ["processing", "failed", "recording"].includes(m.status) || (m.status === "ready" && !m.opened_at));
  const recent = list.filter((m) => !attention.includes(m)).slice(0, 8);

  const row = (m: Meeting) => (
    <a key={m.id} className="mrow" href={m.status === "recording" ? "#/live" : `#/meetings/${m.id}`}>
      <div style={{ minWidth: 0 }}>
        <div className="t" dir="auto">{m.title}</div>
        <div className="meta">
          <span>{fmtDate(m.started_at, lang)}</span><span dir="ltr">{fmtDuration(m.duration_ms || 0)}</span>
          {(m.languages ?? []).length > 0 && <span className="chip">{(m.languages ?? []).map((l) => l.toUpperCase()).join(" + ")}</span>}
          {m.speaker_count ? <span>{m.speaker_count} {m.speaker_count === 1 ? t("speaker1") : t("speakersN")}</span> : null}
          {m.action_item_count ? <span>{m.action_item_count} {t("actionsN")}</span> : null}
        </div>
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <StatusChip m={m} />
        {m.status === "failed" && <button className="btn sm" onClick={(e) => {
          e.preventDefault();
          // only the notes failed: keep the transcript (re-transcribing a long meeting takes as long again)
          (m.error?.startsWith("notes") ? api.regenerate(m.id, {}) : api.retry(m.id)).then(reload, ctx.fail);
        }}>{t("retry")}</button>}
      </div>
    </a>
  );

  return (
    <>
      <div className="hero">
        <div><h1 className="page-title">{t("homeTitle")}</h1><p className="lead">{t("homeLead")}</p></div>
      </div>
      <div className="start-cards">
        <button className="start-card rec" disabled={ctx.recorder.recording} onClick={() => ctx.go(ctx.recorder.recording ? "#/live" : "#/start")}>
          <b><I.record size={18} />{ctx.recorder.recording ? t("recordingNow") : t("record")}</b><span>{t("onlineHint")} · {t("inPerson")}</span>
        </button>
        <button className="start-card" disabled={uploading} onClick={() => fileRef.current?.click()}>
          <b>{uploading ? <I.spin size={18} className="spin-anim" /> : <I.importFile size={18} />}{uploading ? t("importing") : t("importRec")}</b><span>{t("importHint")}</span>
        </button>
        <input ref={fileRef} type="file" accept="audio/*,.wav,.flac,.mp3,.ogg,.aiff" hidden
          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) upload(f); }} />
      </div>

      {meetings === null && <p className="muted">{t("loading")}</p>}
      {meetings !== null && meetings.length === 0 && (
        <div className="empty" style={{ marginTop: 18 }}>
          <h2>{t("emptyTitle")}</h2><p>{t("emptyLead")}</p>
        </div>
      )}
      {attention.length > 0 && <><div className="sec-title">{t("needsAttention")} <span className="n">{attention.length}</span></div>{attention.map(row)}</>}
      {recent.length > 0 && <><div className="sec-title">{t("recent")}</div>{recent.map(row)}</>}
    </>
  );
}
