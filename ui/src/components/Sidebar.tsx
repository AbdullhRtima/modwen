import { useEffect, useState } from "react";
import { api, fmtDuration, Meeting } from "../api";
import type { Ctx } from "../App";
import { Key, useI18n } from "../i18n";
import { useServerEvents } from "../events";
import { I } from "./icons";

/** Meetings grouped the way people remember them: today, yesterday, this week, older. */
function groupOf(m: Meeting): Key {
  const d = new Date(m.started_at ?? 0);
  const now = new Date();
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86_400_000);
  if (diff <= 0) return "today";
  if (diff === 1) return "yesterday";
  if (diff < 7) return "thisWeek";
  return "older";
}

export function useMeetings(ctx: Ctx) {
  const [meetings, setMeetings] = useState<Meeting[] | null>(null);
  const load = () => api.meetings().then((r) => setMeetings(r.meetings)).catch(() => undefined);
  useEffect(() => {
    load();
  }, []);
  useServerEvents(ctx, (e) => {
    if (e.type === "progress") {  // update the % chip in place; no request per tick
      setMeetings((ms) => ms && ms.map((m) => (m.id === e.meeting_id ? { ...m, status: "processing", progress: e.progress as Meeting["progress"] } : m)));
      return;
    }
    if (["notes_ready", "failed", "recording_stopped", "recording_started", "status", "meetings_changed", "resync"].includes(e.type)) load();
  });
  return { meetings, reload: load };
}

export function MeetingStatus({ m }: { m: Meeting }) {
  const { t } = useI18n();
  if (m.status === "recording") return <span className="chip rec"><span className="dot pulse" /> {t("stRecording")}</span>;
  if (m.status === "processing") {
    const pct = m.progress && "overall" in m.progress && m.progress.overall ? ` ${Math.round(m.progress.overall * 100)}%` : "";
    return <span className="chip warn">{t("stProcessing")}{pct}</span>;
  }
  if (m.status === "failed") return <span className="chip rec">{t("stFailed")}</span>;
  return null;
}

export default function Sidebar({ ctx, path, onNavigate }: { ctx: Ctx; path: string; onNavigate: () => void }) {
  const { t, lang } = useI18n();
  const { meetings } = useMeetings(ctx);
  const [q, setQ] = useState("");
  const current = path.startsWith("/meetings/") ? path.split("/")[2] : null;
  const query = q.trim().toLowerCase();
  const list = (meetings ?? []).filter((m) => !query || m.title.toLowerCase().includes(query));
  const groups: Key[] = ["today", "yesterday", "thisWeek", "older"];
  const time = (m: Meeting) => {
    if (!m.started_at) return "";
    const g = groupOf(m);
    const d = new Date(m.started_at);
    const loc = lang === "ar" ? "ar" : undefined;
    return g === "today" || g === "yesterday" ? d.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit" })
      : g === "thisWeek" ? d.toLocaleDateString(loc, { weekday: "short" }) : d.toLocaleDateString(loc, { day: "numeric", month: "short" });
  };

  return (
    <aside className="side" aria-label={t("meetings")}>
      <a className="brand" href="#/" onClick={onNavigate}>
        <span className="mark" aria-hidden="true"><i style={{ height: 9 }} /><i style={{ height: 15 }} /><i style={{ height: 20 }} /><i style={{ height: 12 }} /></span>
        <span><b>Modwen</b><small>مُدوَّن</small></span>
      </a>
      <button className="rec-btn" disabled={ctx.recorder.recording}
        onClick={() => { ctx.go("#/start"); onNavigate(); }}>
        {ctx.recorder.recording ? <span className="dot pulse" /> : <I.record />}{ctx.recorder.recording ? t("recordingNow") : t("record")}
      </button>
      <label className="side-search">
        <I.search size={15} />
        <input id="meeting-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("search")} aria-label={t("search")} />
      </label>
      <div className="mlist">
        {meetings === null && <div className="group">{t("loading")}</div>}
        {meetings !== null && query && list.length === 0 && <div className="group">{t("noMatch")}</div>}
        {groups.map((g) => {
          const items = list.filter((m) => groupOf(m) === g);
          if (!items.length) return null;
          return (
            <div key={g}>
              <div className="group">{t(g)}</div>
              {items.map((m) => (
                <a key={m.id} href={m.status === "recording" ? "#/live" : `#/meetings/${m.id}`} onClick={onNavigate}
                  className={`mitem ${current === m.id ? "on" : ""} ${m.status === "ready" && !m.opened_at ? "unread" : ""}`}>
                  <span className="t" dir="auto">{m.title}</span>
                  <span className="m">
                    <span className="mono">{time(m)}</span>
                    {m.status === "ready" && <span dir="ltr">{fmtDuration(m.duration_ms || 0)}</span>}
                    <MeetingStatus m={m} />
                    {(m.languages ?? []).map((l) => <span key={l} className="chip">{l.toUpperCase()}</span>)}
                  </span>
                </a>
              ))}
            </div>
          );
        })}
      </div>
      <nav className="side-nav" aria-label={t("settings")}>
        <a href="#/people" onClick={onNavigate} className={path.startsWith("/people") ? "on" : ""}>
          <I.people />
          {t("people")}</a>
        <a href="#/settings" onClick={onNavigate} className={path.startsWith("/settings") ? "on" : ""}>
          <I.settings />
          {t("settings")}</a>
      </nav>
      <div className="side-foot">
        <span className="lock">
          <I.lock size={13} />
          {t("localOnly")}</span>
        <span className="faint">Tier {ctx.tier} · {ctx.llmActive}</span>
      </div>
    </aside>
  );
}
