import { api, fmtDuration, isFailedImport, Meeting } from "../api";
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

  const list = meetings ?? [];
  const attention = list.filter((m) => ["processing", "failed", "recording"].includes(m.status) || (m.status === "ready" && !m.opened_at));
  const recent = list.filter((m) => !attention.includes(m)).slice(0, 8);

  // A div, not a link: the button can't sit inside one. The title's link is stretched over the row instead.
  const record = (m: Meeting) => (
    <div key={m.id} className="mrow import-failed">
      <div style={{ minWidth: 0 }}>
        <div className="t" dir="auto"><a href={`#/meetings/${m.id}`} aria-describedby={`rec-chip-${m.id} rec-why-${m.id}`}>{m.title}</a></div>
        <div className="meta"><span>{fmtDate(m.started_at, lang)}</span></div>
        <p className="why" id={`rec-why-${m.id}`}>{t("importStoppedShort")}</p>
      </div>
      <div className="acts-end">
        <span className="chip rec strong" id={`rec-chip-${m.id}`}>{t("stImportFailed")}</span>
        <button className="btn" disabled={ctx.importing} aria-label={`${t("importAgain")}: ${m.title}`} onClick={() => ctx.pickImport(m.id)}>{t("importAgain")}</button>
      </div>
    </div>
  );

  const row = (m: Meeting) => isFailedImport(m) ? record(m) : (
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
        <button className="start-card" disabled={ctx.importing} onClick={() => ctx.pickImport()}>
          <b>{ctx.importing ? <I.spin size={18} className="spin-anim" /> : <I.importFile size={18} />}{ctx.importing ? t("importing") : t("importRec")}</b>{" "}<span>{t("importHint")}</span>{" "}<span className="trust">{t("importTrust")}</span>
        </button>
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
