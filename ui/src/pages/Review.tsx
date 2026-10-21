import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActionItem, api, fmtDuration, fmtTs, Meeting, Notes, NotesData, PersonT, Progress, Segment, setup, Speaker, VoiceResult } from "../api";
import type { Ctx } from "../App";
import { StatusChip } from "./Home";
import { fmtDate, useI18n } from "../i18n";
import { serverText } from "../serverText";
import { useServerEvents } from "../events";
import { I } from "../components/icons";

const SPEAKER_COLORS = ["--sp1", "--sp2", "--sp3", "--sp4", "--sp5", "--sp6", "--sp7", "--sp8"];
export const UNNAMED_SPEAKER = /^(Person \d+|Speaker \d+|Others|Room|Not sure)$/;

export default function Review({ ctx, id }: { ctx: Ctx; id: string }) {
  const { t, tr, lang: ui } = useI18n();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [notes, setNotes] = useState<Notes | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [lit, setLit] = useState<{ seg: Set<string>; item: string | null }>({ seg: new Set(), item: null });
  const [drawer, setDrawer] = useState(false);
  const [menu, setMenu] = useState<"copy" | "regen" | null>(null);
  const [lang, setLang] = useState<string>("en");
  const trRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [pos, setPos] = useState(0);
  const [dur, setDur] = useState(0);
  const [query, setQuery] = useState("");

  const load = useCallback(() => {
    api.meeting(id).then((d) => {
      setMeeting(d.meeting);
      setSpeakers(d.speakers);
      setSegments(d.segments);
      setNotes(d.notes);
      const sl = d.meeting.summary_language || "en";
      setLang((cur) => (d.notes && d.notes.data.summary[cur] !== undefined ? cur : sl === "both" ? "both" : sl));
    }).catch(() => setNotFound(true));
  }, [id]);

  useEffect(load, [load]);
  useServerEvents(ctx, (e) => {
    if (e.type === "resync") return load();  // reconnected: we may have missed the end of processing
    if (e.meeting_id !== id) return;
    if (e.type === "progress") setMeeting((m) => (m ? { ...m, status: "processing", progress: e.progress as Progress } : m));
    if (e.type === "meetings_changed" && e.change === "deleted") ctx.go("#/");  // deleted in another window
    else if (["notes_ready", "failed", "status"].includes(e.type)) load();
  });

  const colorOf = useMemo(() => {
    const map: Record<string, string> = {};
    speakers.forEach((s, i) => (map[s.id] = s.is_self ? "var(--sp1)" : `var(${SPEAKER_COLORS[(i + 1) % SPEAKER_COLORS.length]})`));
    return (sid: string) => map[sid] ?? "var(--text-2)";
  }, [speakers]);

  if (notFound) return <div className="empty"><h2>{t("notFound")}</h2><p><a href="#/">{t("back")}</a></p></div>;
  if (!meeting) return <p className="muted">{t("loading")}</p>;

  const seek = (ms: number, play = false) => {
    const a = audioRef.current;
    if (!a) return;
    a.currentTime = ms / 1000;
    setPos(ms / 1000);
    if (play) a.play().catch(() => undefined);
  };
  const showEvidence = (ids: string[], itemId: string) => {
    setLit({ seg: new Set(ids), item: itemId });
    const first = segments.find((x) => x.id === ids[0]);
    if (first) seek(first.start_ms, true);
    const el = trRef.current?.querySelector(`[data-seg="${ids[0]}"]`) as HTMLElement | null;
    if (el && trRef.current) trRef.current.scrollTo({ top: el.offsetTop - trRef.current.offsetTop - 40, behavior: "smooth" });
  };
  const clickLine = (segId: string) => {
    setLit({ seg: new Set([segId]), item: null });
    const seg = segments.find((x) => x.id === segId);
    if (seg) seek(seg.start_ms, playing);
  };
  const q = query.trim().toLowerCase();
  // One pass per keystroke (a Set), not a search per line: stays fast on 2-hour meetings.
  const hits = new Set(q ? segments.filter((x) => x.text.toLowerCase().includes(q) || (x.speaker_name ?? "").toLowerCase().includes(q)).map((x) => x.id) : []);
  const citing = (ids: string[]) => ids.some((i) => lit.seg.has(i));

  const copyMd = async (opts: { actions_only?: boolean } = {}) => {
    const md = await api.markdown(id, { ...opts, language: lang === "both" ? undefined : lang });
    await navigator.clipboard.writeText(md);
    setMenu(null);
    ctx.toast(opts.actions_only ? t("actionsCopied") : t("notesCopied"));
  };
  const saveNotes = async (patch: Partial<NotesData>) => {
    try {
      setNotes(await api.editNotes(id, patch));
    } catch (e) {
      ctx.fail(e);
      load();
    }
  };
  const switchLang = (l: string) => {
    setLang(l);
    const needed = l === "both" ? ["en", "ar"] : [l];
    if (notes && needed.some((x) => !notes.data.summary[x])) {
      api.regenerate(id, { summary_language: l, scope: "summary" }).then(() => {
        ctx.toast(t("generating"));
        load();
      }, ctx.fail);
    }
  };

  const ev = (ids: string[], itemId: string) =>
    ids.length > 0 && segments.length > 0 ? (
      <button className="ev" onClick={() => showEvidence(ids, itemId)} title={t("transcript")}>
        <I.jump size={12} />{fmtTs(segments.find((s) => s.id === ids[0])?.start_ms ?? 0)}</button>
    ) : null;

  const processing = meeting.status === "processing" || meeting.status === "recording";
  const failed = meeting.status === "failed";
  const data = notes?.data;
  const unnamed = speakers.filter((s) => UNNAMED_SPEAKER.test(s.display_name)).length;
  const lowConf = segments.filter((s) => s.overlap).length;
  const warnings = [
    unnamed ? `${unnamed} ${t("unnamedN")}` : "",
    ...[...new Set(data?.warnings ?? [])].map((w) => serverText(w, ui)),  // a long meeting can repeat one
    lowConf ? `${lowConf} ${t("overlapN")}` : "",
  ].filter(Boolean);
  const point = (id_: string, text: string, original: string | undefined, ids: string[], low = false) => (
    <div key={id_} className={`item ${lit.item === id_ || citing(ids) ? "lit" : ""} ${low ? "low" : ""}`}>
      <span dir="auto">{text}</span>{ev(ids, id_)}
      {original && original !== text && <span className="orig" dir="auto">{original}</span>}
    </div>
  );

  return (
    <>
      <header className="mhead">
        <div className="mhead-row">
          <h1 dir="auto">{meeting.title}</h1>
          {notes && (
            <div className="toolbar">
              <div className="seg" role="group" aria-label={t("summary")}>
                {["en", "ar", "both"].map((l) => (
                  <button key={l} className={lang === l ? "on" : ""} onClick={() => switchLang(l)}>{l === "both" ? t("both") : l.toUpperCase()}</button>
                ))}
              </div>
              <div className="dropdown">
                <button className="btn" onClick={() => setMenu(menu === "regen" ? null : "regen")}><I.regenerate size={15} />{t("regenerate")}<I.chevron size={14} /></button>
                {menu === "regen" && (
                  <div className="menu-pop">
                    <button onClick={() => { setMenu(null); api.regenerate(id, { scope: "summary" }).then(() => { ctx.toast(t("generating")); load(); }, ctx.fail); }}>{t("regenSummary")}</button>
                    <button onClick={() => { setMenu(null); api.regenerate(id, {}).then(load, ctx.fail); }}>{t("regenAll")}</button>
                    <button onClick={() => { setMenu(null); api.retry(id).then(load, ctx.fail); }}>{t("regenTranscribe")}</button>
                  </div>
                )}
              </div>
              <div className="dropdown">
                <button className="btn primary" onClick={() => setMenu(menu === "copy" ? null : "copy")}><I.copy size={15} />{t("copy")}<I.chevron size={14} /></button>
                {menu === "copy" && (
                  <div className="menu-pop">
                    <button onClick={() => copyMd()}><span className="mi"><I.copy />{t("copyMd")}</span><span className="kbd">MD</span></button>
                    <button onClick={() => copyMd({ actions_only: true })}><span className="mi"><I.check />{t("copyActions")}</span></button>
                    <a href={`/api/meetings/${id}/export.md?download=true`} onClick={() => setMenu(null)}><span className="mi"><I.exportFile />{t("exportMd")}</span></a>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
        <div className="mmeta">
          <span>{fmtDate(meeting.started_at, ui)}</span><span dir="ltr">{fmtDuration(meeting.duration_ms || 0)}</span>
          {meeting.languages && meeting.languages.length > 0 && <span>{meeting.languages.map((l) => l.toUpperCase()).join(" + ")}</span>}
          <StatusChip m={meeting} />
          <span className="lock"><I.lock size={14} />{t("encrypted")}</span>
          {notes && <span className="faint">v{notes.version} · {notes.model}</span>}
        </div>
        {speakers.length > 0 && (
          <div className="people-row" aria-label={t("speakers")}>
            {speakers.map((s) => {
              const unsure = UNNAMED_SPEAKER.test(s.display_name);
              return (
                <button key={s.id} className={`person ${unsure ? "unsure" : ""}`} onClick={() => setDrawer(true)} title={t("speakers")}>
                  <span className="av" style={{ background: colorOf(s.id) }}>{(s.display_name.trim()[0] ?? "?").toUpperCase()}</span>
                  <span dir="auto">{s.is_self && s.display_name === "You" ? t("you") : s.display_name}</span>
                  {s.name_source === "voiceprint" ? <span className="voice"><I.voice size={13} />{t("recognised")}</span>
                    : unsure ? <span className="why"><I.unsure size={13} />{t("notSure")}</span> : null}
                </button>
              );
            })}
            <button className="btn ghost sm" onClick={() => setDrawer(true)}><I.speakers size={15} />{t("speakers")}</button>
          </div>
        )}
      </header>

      {(processing || failed) && <ProcessingCard meeting={meeting} onRetry={() => api.retry(id).then(load, ctx.fail)}
        onRegenerate={() => api.regenerate(id, {}).then(load, ctx.fail)} hasTranscript={segments.length > 0} />}

      {(segments.length > 0 || data) && (
        <div className="review">
          <article className="pane">
            <div className="pane-h"><h2>{t("notes")}{data?.parts && data.parts > 1 ? ` · ${data.parts}` : ""}</h2>
              <span className="faint" style={{ fontSize: 12 }}>{t("editHint")}</span></div>
            <div className="pane-b notes">
              {!data && <p className="faint">{failed ? t("notesFailed") : t("notesPending")}</p>}
              {data && (
                <>
                  <div className="sec-title">{t("summary")}</div>
                  {(lang === "both" ? ["en", "ar"] : [lang]).map((l) => (
                    data.summary[l] !== undefined ? (
                      <textarea key={`${l}-${notes?.version}`} className="summary-edit" dir={l === "ar" ? "rtl" : "ltr"}
                        defaultValue={data.summary[l]} aria-label={t("summary")}
                        onBlur={(e) => e.target.value !== data.summary[l] && saveNotes({ summary: { ...data.summary, [l]: e.target.value } })} />
                    ) : <p key={l} className="faint">{t("generating")}</p>
                  ))}

                  <div className="sec-title">{t("decisions")} <span className="n">{data.decisions.length}</span></div>
                  {data.decisions.length === 0 && <p className="faint">{t("none")}</p>}
                  {data.decisions.map((d) => point(d.id, d.text, d.original_text, d.evidence_segment_ids, d.confidence < 0.5))}

                  <div className="sec-title">{t("actionItems")} <span className="n">{data.action_items.length}</span></div>
                  {data.action_items.length === 0 ? <p className="faint">{t("none")}</p> : (
                    <div className="acts">
                      {data.action_items.map((a, idx) => (
                        <ActionRow key={`${a.id}-${notes?.version}`} a={a} lit={lit.item === a.id || citing(a.evidence_segment_ids)}
                          speakers={speakers} colorOf={colorOf} evButton={ev(a.evidence_segment_ids, a.id)}
                          onChange={(patch) => {
                            const items = data.action_items.map((x, i) => (i === idx ? { ...x, ...patch } : x));
                            saveNotes({ action_items: items });
                          }} />
                      ))}
                    </div>
                  )}

                  <div className="sec-title">{t("keyPoints")} <span className="n">{data.key_points.length}</span></div>
                  {data.key_points.length === 0 && <p className="faint">{t("none")}</p>}
                  {data.key_points.map((k) => point(k.id, k.text, k.original_text, k.evidence_segment_ids))}

                  <div className="sec-title">{t("openQuestions")} <span className="n">{data.open_questions.length}</span></div>
                  {data.open_questions.length === 0 && <p className="faint">{t("none")}</p>}
                  {data.open_questions.map((q) => point(q.id, q.question, q.question_original, q.evidence_segment_ids))}
                </>
              )}
            </div>
          </article>

          <aside className="pane tr-pane" aria-label={t("transcript")}>
            <div className="pane-h"><h2>{t("transcript")}</h2>
              <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span className="faint">{q ? `${hits.size} ${t("found")}` : `${segments.length} ${t("lines")}`}</span>
                <input className="input tr-search" type="search" placeholder={t("searchTranscript")} aria-label={t("searchTranscript")} value={query}
                  onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => {
                    if (e.key === "Enter" && hits.size) {
                      const ordered = segments.filter((s) => hits.has(s.id));
                      const next = ordered.find((h) => h.start_ms > pos * 1000 + 50) ?? ordered[0];
                      clickLine(next.id);
                      const el = trRef.current?.querySelector(`[data-seg="${next.id}"]`) as HTMLElement | null;
                      if (el && trRef.current) trRef.current.scrollTo({ top: el.offsetTop - trRef.current.offsetTop - 40, behavior: "smooth" });
                    }
                  }} /></span></div>
            <div className="player">
              <button className="play" aria-label={playing ? t("pause") : tr("Play", "تشغيل")} onClick={() => {
                const a = audioRef.current;
                if (!a) return;
                if (a.paused) a.play().catch(() => undefined);
                else a.pause();
              }}>{playing ? <I.pause size={15} fill="currentColor" /> : <I.play size={15} fill="currentColor" />}</button>
              <span className="ts">{fmtTs(pos * 1000)}</span>
              <input type="range" min={0} max={dur || 0} step={0.5} value={pos} aria-label={tr("Position", "الموضع")} onChange={(e) => seek(Number(e.target.value) * 1000)} />
              <span className="ts">{fmtTs((dur || meeting.duration_ms / 1000) * 1000)}</span>
              <audio ref={audioRef} preload="metadata" src={`/api/meetings/${id}/audio`}
                onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                onTimeUpdate={(e) => setPos(e.currentTarget.currentTime)}
                onLoadedMetadata={(e) => setDur(e.currentTarget.duration)} />
            </div>
            <div className="pane-b" ref={trRef}>
              {segments.map((s) => (
                <TranscriptLine key={s.id} s={s} color={colorOf(s.speaker_id)} speakers={speakers}
                  onSpeaker={(sid) => api.setLineSpeaker(id, s.id, sid).then(load, ctx.fail)}
                  lit={lit.seg.has(s.id) || (playing && pos * 1000 >= s.start_ms && pos * 1000 < s.end_ms)}
                  hit={hits.has(s.id)}
                  onClick={() => clickLine(s.id)}
                  onSave={(text) => api.editSegment(id, s.id, text).then(() => setSegments((all) => all.map((x) => (x.id === s.id ? { ...x, text, edited: 1 } : x))), ctx.fail)} />
              ))}
            </div>
          </aside>
        </div>
      )}

      {warnings.length > 0 && data && (
        <div className="warnbar"><span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><I.warn />{warnings.join(" · ")}</span>
          {unnamed > 0 && <button className="btn sm" onClick={() => setDrawer(true)}>{t("nameSpeakers")}</button>}</div>
      )}

      {drawer && <SpeakerDrawer id={id} speakers={speakers} segments={segments} onClose={() => setDrawer(false)}
        locked={processing} onError={(e) => { ctx.fail(e); load(); }}
        onChanged={(msg) => { load(); ctx.toast(msg ?? t("saved")); }} />}
      <div className="danger-zone">
        {confirmDelete ? (
          <div className="banner err" style={{ display: "inline-flex" }}>{t("deleteConfirm")}
            <button className="btn danger sm" onClick={() => api.remove(id).then(() => ctx.go("#/"), ctx.fail)}>{t("del")}</button>
            <button className="btn sm" onClick={() => setConfirmDelete(false)}>{t("cancel")}</button></div>
        ) : <button className="btn ghost sm" onClick={() => setConfirmDelete(true)}><I.trash size={14} />{t("deleteMeeting")}</button>}
      </div>
    </>
  );
}

function ProcessingCard({ meeting, onRetry, onRegenerate, hasTranscript }: { meeting: Meeting; onRetry: () => void;
  onRegenerate: () => void; hasTranscript: boolean }) {
  const { t, lang } = useI18n();
  const p = meeting.progress as Progress;
  const steps = p?.steps ?? { audio: "done", transcribe: "pending", speakers: "pending", notes: "pending" };
  const icon = (st: string, n: number) => (st === "done" ? <I.check size={13} /> : st === "failed" ? <I.close size={13} /> : st === "running" ? "" : String(n));
  const eta = p?.eta_seconds ? `${Math.max(1, Math.round(p.eta_seconds / 60))} ${t("minLeft")}` : "";
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="steps">
        <div className={`step ${steps.audio ?? "done"}`}><span className="ic"><I.check size={13} /></span><div>{t("audioSaved")}
          <div className="faint" style={{ fontSize: 12 }}><span dir="ltr">{fmtDuration(meeting.duration_ms || 0)}</span> · {t("encrypted")}</div></div><span /></div>
        <div className={`step ${steps.transcribe}`}><span className="ic">{icon(steps.transcribe, 1)}</span>
          <div>{t("transcribing")}<div className="progress"><i style={{ width: `${Math.round((p?.transcribe ?? 0) * 100)}%` }} /></div></div>
          <span className="faint mono">{Math.round((p?.transcribe ?? 0) * 100)}%</span></div>
        <div className={`step ${steps.speakers}`}><span className="ic">{icon(steps.speakers, 2)}</span><div>{t("identifying")}</div><span /></div>
        <div className={`step ${steps.notes}`}><span className="ic">{icon(steps.notes, 3)}</span>
          <div>{t("writingNotes")} <span className="faint">({meeting.llm_profile})</span>
            {p?.notes_parts ? <div className="faint" style={{ fontSize: 12 }}>{t("partOf")} {p.notes_part} / {p.notes_parts}</div> : null}</div><span /></div>
      </div>
      {meeting.status === "processing" && <p className="muted" style={{ marginTop: 14 }}>{eta} {t("closeOk")}</p>}
      {meeting.status === "failed" && (
        <div className="banner err" style={{ marginTop: 14 }}><I.fail />{serverText(meeting.error ?? "", lang)}
          {hasTranscript ? <>&nbsp;{t("transcriptSaved")}&nbsp;<button className="btn sm" onClick={onRegenerate}>{t("retryNotes")}</button></>
            : <>&nbsp;<button className="btn sm" onClick={onRetry}>{t("retry")}</button></>}
          <a className="btn sm" href="#/settings/ai">{t("aiSettings")}</a>
        </div>
      )}
    </div>
  );
}

function ActionRow({ a, lit, speakers, colorOf, evButton, onChange }: { a: ActionItem; lit: boolean; speakers: Speaker[];
  colorOf: (sid: string) => string; evButton: React.ReactNode; onChange: (patch: Partial<ActionItem>) => void }) {
  const { t } = useI18n();
  const sp = speakers.find((s) => s.display_name === a.owner);
  // The due date and the words said can be the same ("this week"): show the words only when they add something.
  const said = a.due_raw && a.due_raw !== a.due_date ? a.due_raw : null;
  return (
    <div className={`act ${lit ? "lit" : ""}`}>
      <input className="inline task-in" dir="auto" defaultValue={a.task} aria-label={t("task")}
        onBlur={(e) => e.target.value !== a.task && onChange({ task: e.target.value })} />
      {evButton ?? <span />}
      <div className="sub">
        <span className="owner">
          <span className="av" style={{ background: sp ? colorOf(sp.id) : "var(--text-3)" }}>{(a.owner ?? "?").trim()[0]?.toUpperCase() ?? "?"}</span>
          <input className={`inline ${a.owner ? "" : "warn"}`} list="mw-owners" dir="auto" defaultValue={a.owner ?? ""} placeholder={t("noOwner")}
            aria-label={t("owner")} onBlur={(e) => e.target.value !== (a.owner ?? "") && onChange({ owner: e.target.value || null })} />
          <datalist id="mw-owners">{speakers.map((s) => <option key={s.id} value={s.display_name} />)}</datalist>
        </span>
        <input className={`inline ${a.due_date ? "" : "empty"}`} type="date" defaultValue={a.due_date ?? ""} aria-label={t("due")}
          onChange={(e) => e.currentTarget.classList.toggle("empty", !e.currentTarget.value)}
          onBlur={(e) => e.target.value !== (a.due_date ?? "") && onChange({ due_date: e.target.value || null })} />
        {said && <span className="said" dir="auto">{t("said")}: {said}</span>}
      </div>
      {a.task_original && a.task_original !== a.task && <span className="orig" dir="auto">{a.task_original}</span>}
    </div>
  );
}

function TranscriptLine({ s, color, lit, hit, speakers, onClick, onSave, onSpeaker }: { s: Segment; color: string;
  lit: boolean; hit?: boolean; speakers: Speaker[]; onClick: () => void; onSave: (text: string) => void;
  onSpeaker: (speakerId: string) => void }) {
  const { t, tr } = useI18n();
  const langTag = (l: string) => ({ en: "EN", ar: "AR", mixed: tr("mixed", "مختلط") } as Record<string, string>)[l] ?? l;
  const [editing, setEditing] = useState(false);
  const [picking, setPicking] = useState(false);
  return (
    <div data-seg={s.id} className={`tr-line ${lit ? "lit" : ""} ${hit ? "hit" : ""} ${s.overlap ? "overlap" : ""}`} onClick={onClick}
      onDoubleClick={() => setEditing(true)}>
      <span className="ts">{fmtTs(s.start_ms)}</span>
      <span className="who">
        {picking ? (
          <select className="select sm" autoFocus value={s.speaker_id} onClick={(e) => e.stopPropagation()}
            onBlur={() => setPicking(false)}
            onChange={(e) => { setPicking(false); if (e.target.value !== s.speaker_id) onSpeaker(e.target.value); }}>
            {speakers.map((o) => <option key={o.id} value={o.id}>{o.display_name}</option>)}
            <option value="new">{t("newPerson")}</option>
          </select>
        ) : (
          <button className="sp sp-pick" style={{ color }} title={t("whoSaid")}
            onClick={(e) => { e.stopPropagation(); setPicking(true); }}>{s.speaker_is_self && s.speaker_name === "You" ? t("you") : s.speaker_name ?? "?"}</button>
        )}
        {s.uncertain ? <button className="badge warn" title={t("whoSaid")}
          onClick={(e) => { e.stopPropagation(); setPicking(true); }}><I.unsure size={12} />{t("notSure")}</button> : null}
        {s.overlap ? <span className="badge warn">{t("overlap")}</span> : null}
        {s.edited ? <I.edit size={12} className="faint" /> : null}
        <span className="faint" style={{ fontSize: 11 }}>{langTag(s.language)}</span></span>
      {editing ? (
        <textarea className="input txt" autoFocus defaultValue={s.text} dir="auto"
          onBlur={(e) => { setEditing(false); if (e.target.value !== s.text) onSave(e.target.value); }} />
      ) : <span className="txt" dir="auto">{s.text}</span>}
    </div>
  );
}

const pct = (x: number) => `${Math.round(x * 100)}%`;

function SpeakerDrawer({ id, speakers, segments, locked, onClose, onChanged, onError }: { id: string;
  speakers: Speaker[]; segments: Segment[]; locked: boolean; onClose: () => void; onChanged: (msg?: string) => void;
  onError: (e: unknown) => void }) {
  const { t, tr } = useI18n();
  const [known, setKnown] = useState<PersonT[]>([]);
  useEffect(() => {
    setup.people().then((r) => setKnown(r.people), () => undefined);
  }, []);
  const personOf = (s: Speaker) => known.find((p) => p.id === s.person_id);
  const [names, setNames] = useState<Record<string, string>>({});
  const [remember, setRemember] = useState<Record<string, boolean>>({});
  const [merges, setMerges] = useState<Record<string, string>>({});
  const [people, setPeople] = useState("");
  const [busy, setBusy] = useState(false);
  const shown = (s: Speaker) => (s.is_self && s.display_name === "You" ? t("you") : s.display_name);
  const voiceMsg = (name: string, v: VoiceResult) => !v ? null
    : v.saved ? tr(`${name}'s voice saved (${Math.round(v.seconds)} s)`, `تم حفظ صوت ${name} (${Math.round(v.seconds)} ث)`)
    : tr(`${name}'s voice not saved: ${v.reason}`, `لم يُحفظ صوت ${name}: ${v.reason}`);
  const run = async (fn: () => Promise<string | null | undefined>) => {
    setBusy(true);
    try {
      const msg = await fn();
      onChanged(msg ?? undefined);
      onClose();
    } catch (e) {
      onError(e);  // the drawer stays open, so nothing typed is lost
    } finally {
      setBusy(false);
    }
  };
  const stats = (sid: string) => {
    const segs = segments.filter((s) => s.speaker_id === sid);
    const secs = segs.reduce((n, s) => n + (s.end_ms - s.start_ms) / 1000, 0);
    const dur = secs >= 60 ? `${Math.round(secs / 60)} min` : `${Math.round(secs)} s`;
    return <>{segs.length === 1 ? tr("1 line", "سطر واحد") : `${segs.length} ${t("lines")}`} · <span dir="ltr">{dur}</span></>;
  };
  const apply = () => run(async () => {
    const msgs: string[] = [];
    for (const [sid, raw] of Object.entries(names)) {
      const name = raw.trim();
      if (!name) continue;
      const sp = speakers.find((x) => x.id === sid);
      const r = await api.linkPerson(id, sid, { name, remember_voice: !sp?.is_self && Boolean(remember[sid]) });
      const m = voiceMsg(name, r.voice);
      if (m) msgs.push(m);
    }
    // Naming can already merge speakers (same name twice): skip merges whose speakers are gone.
    let alive = new Set((await api.meeting(id)).speakers.map((x) => x.id));
    for (const [sid, into] of Object.entries(merges)) {
      if (!into || !alive.has(sid) || !alive.has(into)) continue;
      const r = await api.merge(id, sid, into);
      alive = new Set(r.speakers.map((x) => x.id));
    }
    return msgs.join(" · ") || null;
  });
  const confirmVoice = (s: Speaker) => run(async () => {
    const r = await api.confirmVoice(id, s.id);
    const name = s.suggestion?.name ?? s.display_name;
    return voiceMsg(name, r.voice) ?? tr(`Confirmed: ${name}`, `تم التأكيد: ${name}`);
  });
  const rememberVoice = (s: Speaker) => run(async () => {
    const r = await api.linkPerson(id, s.id, { name: s.display_name, remember_voice: true });
    return voiceMsg(s.display_name, r.voice);
  });
  const reject = (s: Speaker) => run(async () => {
    await api.rejectVoice(id, s.id);
    return tr("Got it. Modwen won't pair them again in this meeting", "حسناً. لن يربط مُدوَّن بينهما مرة أخرى في هذا الاجتماع");
  });
  const redetect = () => run(async () => {
    const n = parseInt(people, 10);
    await api.redetect(id, Number.isFinite(n) && n > 0 ? n : null);
    return null;
  });
  const colors = ["var(--sp2)", "var(--sp3)", "var(--sp4)", "var(--sp5)", "var(--sp6)", "var(--sp7)", "var(--sp8)"];
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy]);
  return (
    <>
    <div className="scrim" onClick={() => { if (!busy) onClose(); }} aria-hidden="true" />
    <div className="drawer" role="dialog" aria-modal="true" aria-label={t("speakers")}>
      <div className="drawer-head"><h2><I.speakers size={18} />{t("speakers")}</h2>
        <button className="btn ghost sm" aria-label={t("cancel")} onClick={onClose}><I.close size={16} /></button></div>
      {locked && <div className="banner warn"><I.warn />{tr("Modwen is still working on this meeting. Speakers can be changed when it has finished.",
        "ما زال مُدوَّن يعالج هذا الاجتماع. يمكن تعديل المتحدثين بعد الانتهاء.")}</div>}
      <fieldset disabled={locked || busy} className="drawer-body">
      {speakers.map((s, i) => {
        const unsure = UNNAMED_SPEAKER.test(s.display_name);
        const typed = (names[s.id] ?? "").trim();
        return (
          <section key={s.id} className="spk">
            <div className="top">
              <span className="spk-name">
                <span className="av" style={{ background: s.is_self ? "var(--sp1)" : colors[i % colors.length] }}>{(shown(s).trim()[0] ?? "?").toUpperCase()}</span>
                <span><b dir="auto">{shown(s)}</b><span className="faint spk-stats">{stats(s.id)}</span></span>
              </span>
              {s.name_source === "voiceprint" ? <span className="badge ok"><I.voice size={13} />{t("recognised")} · {pct(s.confidence)}</span>
                : s.is_self ? <span className="badge ok"><I.check size={13} />{tr("your voice", "صوتك")}</span>
                : s.name_source === "manual" ? <span className="badge ok"><I.check size={13} />{tr("named", "مُسمّى")}</span>
                : <span className="badge warn"><I.unsure size={13} />{unsure ? t("notSure") : tr("unnamed", "بلا اسم")}</span>}
            </div>

            {s.name_source === "voiceprint" && (
              <div className="spk-actions">
                <button className="btn sm" onClick={() => confirmVoice(s)} title={tr("Right person: keep the name and learn from this meeting", "الشخص الصحيح: احتفظ بالاسم وتعلّم من هذا الاجتماع")}>
                  <I.confirm size={14} />{tr("Confirm", "تأكيد")}</button>
                <button className="btn sm ghost" onClick={() => reject(s)}><I.close size={14} />{tr(`Not ${s.display_name}`, `ليس ${s.display_name}`)}</button>
              </div>
            )}
            {s.suggestion && s.name_source !== "voiceprint" && (
              <div className="banner info spk-suggest">
                <I.voice />{tr("Sounds like", "يبدو أنه")} <b dir="auto">{s.suggestion.name}</b> <span className="mono">({pct(s.suggestion.score)})</span>
                <button className="btn sm" onClick={() => confirmVoice(s)}>{tr(`Yes, it's ${s.suggestion.name}`, `نعم، ${s.suggestion.name}`)}</button>
                <button className="btn sm ghost" onClick={() => reject(s)}>{tr("No", "لا")}</button>
              </div>
            )}
            {!s.is_self && !unsure && s.name_source !== "voiceprint" && (personOf(s)?.voice
              ? <p className="spk-note ok"><I.voice size={14} />{tr(`${s.display_name}'s voice is saved: recognised in later meetings`, `صوت ${s.display_name} محفوظ: يُتعرّف عليه في الاجتماعات القادمة`)}</p>
              : <button className="btn sm" onClick={() => rememberVoice(s)}
                  title={tr("Only with their consent. Saves a voiceprint so Modwen recognises them in later meetings", "بموافقتهم فقط. يحفظ بصمة الصوت ليتعرّف مُدوَّن عليهم لاحقاً")}>
                  <I.voice size={14} />{tr(`Remember ${s.display_name}'s voice`, `تذكّر صوت ${s.display_name}`)}</button>)}
            {s.is_self ? (
              <p className="spk-note">{tr("This is you.", "هذا أنت.")} <a href="#/people">{tr("Set your name on the People page", "ضع اسمك في صفحة الأشخاص")}</a>
                {tr("; your voice is saved from Settings → Privacy.", "؛ صوتك يُحفظ من الإعدادات ← الخصوصية.")}</p>
            ) : null}

            <label className="spk-field">
              <span>{s.is_self ? tr("Not you? Type who it was", "ليس أنت؟ اكتب من كان") : tr("Who is this?", "من هذا؟")}</span>
              <datalist id="mw-people">{known.filter((p) => !p.is_self).map((p) => <option key={p.id} value={p.display_name} />)}</datalist>
              <input className="input" list="mw-people" dir="auto" placeholder={tr("Name, e.g. Sara Haddad", "الاسم، مثلاً سارة حداد")}
                value={names[s.id] ?? ""} onChange={(e) => setNames({ ...names, [s.id]: e.target.value })} />
            </label>
            {!s.is_self && typed && (
              <label className="check spk-remember">
                <input type="checkbox" checked={Boolean(remember[s.id])} onChange={(e) => setRemember({ ...remember, [s.id]: e.target.checked })} />
                {tr(`Remember this voice, so Modwen recognises ${typed} next time (they agreed)`, `تذكّر هذا الصوت ليتعرّف مُدوَّن على ${typed} لاحقاً (بموافقته)`)}
              </label>
            )}
            {speakers.length > 1 && (
              <label className="spk-field">
                <span>{tr("Same person as…", "نفس الشخص مثل…")}</span>
                <select className="select" value={merges[s.id] ?? ""} onChange={(e) => setMerges({ ...merges, [s.id]: e.target.value })}>
                  <option value="">{tr("No, a different person", "لا، شخص مختلف")}</option>
                  {speakers.filter((o) => o.id !== s.id).map((o) => <option key={o.id} value={o.id}>{shown(o)}</option>)}
                </select>
              </label>
            )}
          </section>
        );
      })}
      <button className="btn primary lg drawer-apply" onClick={apply}>{busy ? <I.spin className="spin-anim" /> : <I.check />}{busy ? tr("Saving…", "جارٍ الحفظ…") : t("save")}</button>
      <p className="faint drawer-hint">{tr("Names are saved on the People page: rename or delete someone there and every meeting follows. To fix a single line, click the name next to it in the transcript.",
        "تُحفظ الأسماء في صفحة الأشخاص: غيّر الاسم أو احذفه هناك فتتغير كل الاجتماعات. لتصحيح سطر واحد، اضغط على الاسم بجانبه في النص.")}</p>
      <section className="spk redetect">
        <b>{tr("Wrong speakers?", "المتحدثون غير صحيحين؟")}</b>
        <p className="faint">{tr("Detect the voices again from the recording. Your text edits and the names you gave are kept, saved voices are matched again, and the notes are rewritten.",
          "اكتشف الأصوات من جديد من التسجيل. تعديلاتك والأسماء تبقى، وتُطابَق الأصوات المحفوظة مجدداً، وتُعاد كتابة الملاحظات.")}</p>
        <div className="redetect-row">
          <input className="input" type="number" min={1} max={20} placeholder={tr("People (optional)", "عدد الأشخاص (اختياري)")} value={people}
            onChange={(e) => setPeople(e.target.value)} title={tr("How many people were in the meeting, you included", "كم شخصاً كان في الاجتماع، بما فيهم أنت")} />
          <button className="btn" onClick={redetect}><I.regenerate size={15} />{tr("Re-detect speakers", "أعد اكتشاف المتحدثين")}</button>
        </div>
      </section>
      </fieldset>
    </div>
    </>
  );
}
