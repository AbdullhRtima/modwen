import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActionItem, api, fmtDuration, fmtTs, Meeting, Notes, NotesData, Progress, Segment, setup, Speaker } from "../api";
import type { Ctx } from "../App";
import { StatusChip } from "./Home";

const SPEAKER_COLORS = ["--sp1", "--sp2", "--sp3", "--sp4", "--sp5", "--sp6", "--sp7", "--sp8"];
export const UNNAMED_SPEAKER = /^(Person \d+|Speaker \d+|Others|Room)$/;

export default function Review({ ctx, id }: { ctx: Ctx; id: string }) {
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
  useEffect(() => {
    const e = ctx.lastEvent;
    if (!e || e.meeting_id !== id) return;
    if (e.type === "progress") setMeeting((m) => (m ? { ...m, status: "processing", progress: e.progress as Progress } : m));
    if (["notes_ready", "failed", "status"].includes(e.type)) load();
  }, [ctx.lastEvent, id, load]);

  const colorOf = useMemo(() => {
    const map: Record<string, string> = {};
    speakers.forEach((s, i) => (map[s.id] = s.is_self ? "var(--sp1)" : `var(${SPEAKER_COLORS[(i + 1) % SPEAKER_COLORS.length]})`));
    return (sid: string) => map[sid] ?? "var(--text-2)";
  }, [speakers]);

  if (notFound) return <div className="card">Meeting not found. <a href="#/">Back to meetings</a></div>;
  if (!meeting) return <p className="muted">Loading…</p>;

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
  const hits = q ? segments.filter((x) => x.text.toLowerCase().includes(q) || (x.speaker_name ?? "").toLowerCase().includes(q)) : [];
  const citing = (ids: string[]) => ids.some((i) => lit.seg.has(i));

  const copyMd = async (opts: { actions_only?: boolean } = {}) => {
    const md = await api.markdown(id, { ...opts, language: lang === "both" ? undefined : lang });
    await navigator.clipboard.writeText(md);
    setMenu(null);
    ctx.toast(opts.actions_only ? "Action items copied" : "Notes copied as Markdown");
  };
  const saveNotes = async (patch: Partial<NotesData>) => {
    const n = await api.editNotes(id, patch);
    setNotes(n);
  };
  const switchLang = (l: string) => {
    setLang(l);
    const needed = l === "both" ? ["en", "ar"] : [l];
    if (notes && needed.some((x) => !notes.data.summary[x])) {
      api.regenerate(id, { summary_language: l, scope: "summary" }).then(() => {
        ctx.toast("Generating that summary locally…");
        load();
      });
    }
  };

  const ev = (ids: string[], itemId: string) =>
    ids.length > 0 && segments.length > 0 ? (
      <button className="ev" onClick={() => showEvidence(ids, itemId)}>
        ↗ {fmtTs(segments.find((s) => s.id === ids[0])?.start_ms ?? 0)}</button>
    ) : null;

  const processing = meeting.status === "processing" || meeting.status === "recording";
  const failed = meeting.status === "failed";
  const data = notes?.data;
  const unnamed = speakers.filter((s) => UNNAMED_SPEAKER.test(s.display_name)).length;
  const lowConf = segments.filter((s) => s.overlap).length;
  const warnings = [
    unnamed ? `${unnamed} unnamed speaker${unnamed > 1 ? "s" : ""}` : "",
    ...(data?.warnings ?? []),
    lowConf ? `${lowConf} overlapping line${lowConf > 1 ? "s" : ""}` : "",
  ].filter(Boolean);

  return (
    <>
      <div className="review-head">
        <div>
          <h1>{meeting.title}</h1>
          <div className="muted" style={{ fontSize: 12.5, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {meeting.started_at ? new Date(meeting.started_at).toLocaleString() : ""} · {fmtDuration(meeting.duration_ms || 0)}
            <StatusChip m={meeting} /> <span className="lock">🔒 Local only</span>
            {notes && <span className="faint">notes v{notes.version} · {notes.model}</span>}
          </div>
        </div>
        {notes && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 12.5 }}>Summary</span>
            <div className="seg">
              {["en", "ar", "both"].map((l) => (
                <button key={l} className={lang === l ? "on" : ""} onClick={() => switchLang(l)}>{l === "both" ? "Both" : l.toUpperCase()}</button>
              ))}
            </div>
            <button className="btn" onClick={() => setDrawer(true)}>☺ Speakers</button>
            <div className="dropdown">
              <button className="btn" onClick={() => setMenu(menu === "regen" ? null : "regen")}>↻ Regenerate ▾</button>
              {menu === "regen" && (
                <div className="menu-pop">
                  <button onClick={() => { setMenu(null); api.regenerate(id, { scope: "summary" }).then(() => { ctx.toast("Rewriting the summary from your edited notes…"); load(); }); }}>Summary only (keeps your edits)</button>
                  <button onClick={() => { setMenu(null); api.regenerate(id, {}).then(load); }}>Whole notes (re-reads the transcript; replaces edits)</button>
                  <button onClick={() => { setMenu(null); api.retry(id).then(load); }}>Re-transcribe + notes</button>
                </div>
              )}
            </div>
            <div className="dropdown">
              <button className="btn primary" onClick={() => setMenu(menu === "copy" ? null : "copy")}>Copy ▾</button>
              {menu === "copy" && (
                <div className="menu-pop">
                  <button onClick={() => copyMd()}>Copy as Markdown <span className="kbd">MD</span></button>
                  <button onClick={() => copyMd({ actions_only: true })}>Copy action items only</button>
                  <a className="btn ghost" style={{ width: "100%" }} href={`/api/meetings/${id}/export.md?download=true`}
                    onClick={() => setMenu(null)}>Export .md file</a>
                  <button disabled className="faint">Send email: Phase 3</button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {(processing || failed) && <ProcessingCard meeting={meeting} onRetry={() => api.retry(id).then(load)}
        onRegenerate={() => api.regenerate(id, {}).then(load)} hasTranscript={segments.length > 0} />}

      {(segments.length > 0 || data) && (
        <div className="review">
          <div className="pane">
            <div className="pane-h"><span>Notes{data?.parts && data.parts > 1 ? ` · built from ${data.parts} parts` : ""}</span>
              <span className="faint">Edit inline; changes save automatically</span></div>
            <div className="pane-b notes">
              {!data && <p className="faint">{failed ? "Notes are not available yet. Start your local AI model, then press Retry above." : "Notes appear here when the AI model finishes."}</p>}
              {data && (
                <>
                  <h4>Summary</h4>
                  {(lang === "both" ? ["en", "ar"] : [lang]).map((l) => (
                    data.summary[l] !== undefined ? (
                      <textarea key={`${l}-${notes?.version}`} className="summary-edit" dir={l === "ar" ? "rtl" : "ltr"}
                        defaultValue={data.summary[l]}
                        onBlur={(e) => e.target.value !== data.summary[l] && saveNotes({ summary: { ...data.summary, [l]: e.target.value } })} />
                    ) : <p key={l} className="faint">Generating the {l === "ar" ? "Arabic" : "English"} summary…</p>
                  ))}

                  <h4>Decisions</h4>
                  {data.decisions.length === 0 && <p className="faint">None recorded.</p>}
                  {data.decisions.map((d) => (
                    <div key={d.id} className={`item ${lit.item === d.id || citing(d.evidence_segment_ids) ? "lit" : ""} ${d.confidence < 0.5 ? "low" : ""}`}>
                      {d.text}{ev(d.evidence_segment_ids, d.id)}
                      {d.original_text && d.original_text !== d.text && <span className="orig" dir="auto">“{d.original_text}”</span>}
                    </div>
                  ))}

                  <h4>Action items</h4>
                  {data.action_items.length === 0 ? <p className="faint">None recorded.</p> : (
                    <table className="atable">
                      <thead><tr><th style={{ width: "24%" }}>Owner</th><th>Task</th><th style={{ width: "20%" }}>Due</th><th /></tr></thead>
                      <tbody>
                        {data.action_items.map((a, idx) => (
                          <ActionRow key={`${a.id}-${notes?.version}`} a={a} lit={lit.item === a.id || citing(a.evidence_segment_ids)}
                            speakers={speakers} evButton={ev(a.evidence_segment_ids, a.id)}
                            onChange={(patch) => {
                              const items = data.action_items.map((x, i) => (i === idx ? { ...x, ...patch } : x));
                              saveNotes({ action_items: items });
                            }} />
                        ))}
                      </tbody>
                    </table>
                  )}

                  <h4>Key points</h4>
                  {data.key_points.map((k) => (
                    <div key={k.id} className={`item ${lit.item === k.id || citing(k.evidence_segment_ids) ? "lit" : ""}`}>
                      {k.text}{ev(k.evidence_segment_ids, k.id)}
                      {k.original_text && k.original_text !== k.text && <span className="orig" dir="auto">“{k.original_text}”</span>}
                    </div>
                  ))}

                  <h4>Open questions</h4>
                  {data.open_questions.length === 0 && <p className="faint">None.</p>}
                  {data.open_questions.map((q) => (
                    <div key={q.id} className={`item ${lit.item === q.id || citing(q.evidence_segment_ids) ? "lit" : ""}`}>
                      {q.question}{ev(q.evidence_segment_ids, q.id)}
                      {q.question_original && q.question_original !== q.question && <span className="orig" dir="auto">“{q.question_original}”</span>}
                    </div>
                  ))}
                </>
              )}
            </div>
          </div>

          <div className="pane">
            <div className="pane-h"><span>Transcript · {segments.length} lines · {meeting.stt_info?.engine ?? ""}</span>
              <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                {q && <span className="faint">{hits.length} found</span>}
                <input className="input" style={{ width: 160, padding: "3px 8px" }} placeholder="⌕ Search" value={query}
                  onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => {
                    if (e.key === "Enter" && hits.length) {
                      const next = hits.find((h) => h.start_ms > pos * 1000 + 50) ?? hits[0];
                      clickLine(next.id);
                      const el = trRef.current?.querySelector(`[data-seg="${next.id}"]`) as HTMLElement | null;
                      if (el && trRef.current) trRef.current.scrollTo({ top: el.offsetTop - trRef.current.offsetTop - 40, behavior: "smooth" });
                    }
                  }} /></span></div>
            <div className="player">
              <button className="btn sm" title={playing ? "Pause" : "Play"} onClick={() => {
                const a = audioRef.current;
                if (!a) return;
                if (a.paused) a.play().catch(() => undefined);
                else a.pause();
              }}>{playing ? "❚❚" : "▶"}</button>
              <span className="ts">{fmtTs(pos * 1000)}</span>
              <input type="range" min={0} max={dur || 0} step={0.5} value={pos} onChange={(e) => seek(Number(e.target.value) * 1000)} />
              <span className="ts">{fmtTs((dur || meeting.duration_ms / 1000) * 1000)}</span>
              <audio ref={audioRef} preload="metadata" src={`/api/meetings/${id}/audio`}
                onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
                onTimeUpdate={(e) => setPos(e.currentTarget.currentTime)}
                onLoadedMetadata={(e) => setDur(e.currentTarget.duration)} />
            </div>
            <div className="pane-b" ref={trRef}>
              {segments.map((s) => (
                <TranscriptLine key={s.id} s={s} color={colorOf(s.speaker_id)} speakers={speakers}
                  onSpeaker={(sid) => api.setLineSpeaker(id, s.id, sid).then(load)}
                  lit={lit.seg.has(s.id) || (playing && pos * 1000 >= s.start_ms && pos * 1000 < s.end_ms)}
                  hit={Boolean(q) && hits.includes(s)}
                  onClick={() => clickLine(s.id)}
                  onSave={(text) => api.editSegment(id, s.id, text).then(() => setSegments((all) => all.map((x) => (x.id === s.id ? { ...x, text, edited: 1 } : x))))} />
              ))}
            </div>
          </div>
        </div>
      )}

      {warnings.length > 0 && data && (
        <div className="warnbar"><span>⚠ {warnings.join(" · ")}</span>
          {unnamed > 0 && <button className="btn sm" onClick={() => setDrawer(true)}>Name speakers</button>}</div>
      )}

      {drawer && <SpeakerDrawer id={id} speakers={speakers} segments={segments} onClose={() => setDrawer(false)}
        onChanged={() => { load(); ctx.toast("Speakers updated"); }} />}
      <div style={{ marginTop: 24 }}>
        <button className="btn ghost sm" onClick={() => {
          if (confirm("Delete this meeting, its audio and notes? This cannot be undone.")) api.remove(id).then(() => ctx.go("#/"));
        }}>🗑 Delete meeting</button>
      </div>
    </>
  );
}

function ProcessingCard({ meeting, onRetry, onRegenerate, hasTranscript }: { meeting: Meeting; onRetry: () => void;
  onRegenerate: () => void; hasTranscript: boolean }) {
  const p = meeting.progress as Progress;
  const steps = p?.steps ?? { audio: "done", transcribe: "pending", speakers: "pending", notes: "pending" };
  const icon = (st: string, n: number) => (st === "done" ? "✓" : st === "failed" ? "✕" : st === "running" ? "" : String(n));
  const eta = p?.eta_seconds ? `about ${Math.max(1, Math.round(p.eta_seconds / 60))} min left` : "";
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="steps">
        <div className={`step ${steps.audio ?? "done"}`}><span className="ic">✓</span><div>Audio saved
          <div className="faint" style={{ fontSize: 12 }}>{fmtDuration(meeting.duration_ms || 0)} · encrypted</div></div><span /></div>
        <div className={`step ${steps.transcribe}`}><span className="ic">{icon(steps.transcribe, 1)}</span>
          <div>Transcribing<div className="progress"><i style={{ width: `${Math.round((p?.transcribe ?? 0) * 100)}%` }} /></div></div>
          <span className="faint">{Math.round((p?.transcribe ?? 0) * 100)}%</span></div>
        <div className={`step ${steps.speakers}`}><span className="ic">{icon(steps.speakers, 2)}</span><div>Identifying speakers</div><span /></div>
        <div className={`step ${steps.notes}`}><span className="ic">{icon(steps.notes, 3)}</span>
          <div>Writing notes ({meeting.llm_profile})
            {p?.notes_parts ? <div className="faint" style={{ fontSize: 12 }}>part {p.notes_part} of {p.notes_parts}</div> : null}</div><span /></div>
      </div>
      {meeting.status === "processing" && <p className="muted" style={{ marginTop: 14 }}>{eta} You can close this window; you'll get a notification.</p>}
      {meeting.status === "failed" && (
        <div className="banner err" style={{ marginTop: 14 }}>✕ {meeting.error}
          {hasTranscript ? <>&nbsp;The transcript is saved.&nbsp;<button className="btn sm" onClick={onRegenerate}>Retry notes</button></>
            : <>&nbsp;<button className="btn sm" onClick={onRetry}>Retry</button></>}
          <a className="btn sm" href="#/settings">AI model settings</a>
        </div>
      )}
    </div>
  );
}

function ActionRow({ a, lit, speakers, evButton, onChange }: { a: ActionItem; lit: boolean; speakers: Speaker[];
  evButton: React.ReactNode; onChange: (patch: Partial<ActionItem>) => void }) {
  return (
    <tr className={`item ${lit ? "lit" : ""}`}>
      <td>
        <input list="bb-speakers" defaultValue={a.owner ?? ""} placeholder="? owner"
          style={!a.owner ? { borderColor: "var(--warn)" } : undefined}
          onBlur={(e) => e.target.value !== (a.owner ?? "") && onChange({ owner: e.target.value || null })} />
        <datalist id="bb-speakers">{speakers.map((s) => <option key={s.id} value={s.display_name} />)}</datalist>
      </td>
      <td>
        <input defaultValue={a.task} onBlur={(e) => e.target.value !== a.task && onChange({ task: e.target.value })} />
        {a.task_original && a.task_original !== a.task && <span className="orig" dir="auto">“{a.task_original}”</span>}
      </td>
      <td>
        <input type="date" defaultValue={a.due_date ?? ""} onBlur={(e) => e.target.value !== (a.due_date ?? "") && onChange({ due_date: e.target.value || null })} />
        {a.due_raw && <span className="said" dir="auto">said: “{a.due_raw}”</span>}
      </td>
      <td>{evButton}</td>
    </tr>
  );
}

function TranscriptLine({ s, color, lit, hit, speakers, onClick, onSave, onSpeaker }: { s: Segment; color: string;
  lit: boolean; hit?: boolean; speakers: Speaker[]; onClick: () => void; onSave: (text: string) => void;
  onSpeaker: (speakerId: string) => void }) {
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
            <option value="new">+ New person</option>
          </select>
        ) : (
          <button className="sp sp-pick" style={{ color }} title="Who said this? Click to change"
            onClick={(e) => { e.stopPropagation(); setPicking(true); }}>{s.speaker_name ?? "?"}</button>
        )}
        {s.overlap ? <span className="badge warn">overlap</span> : null}
        {s.edited ? <span className="faint">✎</span> : null}
        <span className="faint" style={{ fontSize: 11 }}>{s.language}</span></span>
      {editing ? (
        <textarea className="input txt" autoFocus defaultValue={s.text} dir="auto"
          onBlur={(e) => { setEditing(false); if (e.target.value !== s.text) onSave(e.target.value); }} />
      ) : <span className="txt" dir="auto">{s.text}</span>}
    </div>
  );
}

function SpeakerDrawer({ id, speakers, segments, onClose, onChanged }: { id: string; speakers: Speaker[];
  segments: Segment[]; onClose: () => void; onChanged: () => void }) {
  const [known, setKnown] = useState<string[]>([]);
  useEffect(() => {
    setup.people().then((r) => setKnown(r.people.map((p) => p.display_name)));
  }, []);
  const [names, setNames] = useState<Record<string, string>>({});
  const [merges, setMerges] = useState<Record<string, string>>({});
  const [people, setPeople] = useState("");
  const stats = (sid: string) => {
    const segs = segments.filter((s) => s.speaker_id === sid);
    const secs = segs.reduce((t, s) => t + (s.end_ms - s.start_ms) / 1000, 0);
    return `${segs.length} lines · ${secs >= 60 ? `${Math.round(secs / 60)} min` : `${Math.round(secs)} s`}`;
  };
  const apply = async () => {
    for (const [sid, name] of Object.entries(names)) if (name.trim()) await api.rename(id, sid, name.trim());
    for (const [sid, into] of Object.entries(merges)) if (into) await api.merge(id, sid, into);
    onChanged();
    onClose();
  };
  const redetect = async () => {
    const n = parseInt(people, 10);
    await api.redetect(id, Number.isFinite(n) && n > 0 ? n : null);
    onChanged();
    onClose();
  };
  return (
    <div className="drawer">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <b>Speakers</b><button className="btn ghost sm" onClick={onClose}>✕</button></div>
      {speakers.map((s) => (
        <div key={s.id} className="spk">
          <div className="top"><b>{s.display_name}</b>
            {s.is_self ? <span className="badge ok">✓ your voice</span>
              : s.name_source === "manual" ? <span className="badge ok">✓ named</span> : <span className="badge warn">unnamed</span>}</div>
          <div className="faint" style={{ fontSize: 12, margin: "4px 0 8px" }}>{stats(s.id)}</div>
          <datalist id="mw-people">{known.map((n) => <option key={n} value={n} />)}</datalist>
          <input className="input" list="mw-people" placeholder={s.is_self ? "Not you? Type who it is" : "Name, e.g. Sara Haddad"}
            value={names[s.id] ?? ""} onChange={(e) => setNames({ ...names, [s.id]: e.target.value })} style={{ marginBottom: 6 }} />
          {speakers.length > 1 && (
            <select className="select" value={merges[s.id] ?? ""} onChange={(e) => setMerges({ ...merges, [s.id]: e.target.value })}>
              <option value="">Merge into… (same person split in two)</option>
              {speakers.filter((o) => o.id !== s.id).map((o) => <option key={o.id} value={o.id}>{o.display_name}</option>)}
            </select>
          )}
        </div>
      ))}
      <button className="btn primary" style={{ width: "100%", justifyContent: "center" }} onClick={apply}>Apply</button>
      <p className="faint" style={{ fontSize: 12 }}>Renaming updates the transcript and the names in the notes without calling the AI model again.
        Giving two speakers the same name merges them. To fix a single line, click the name next to it in the transcript.</p>
      <div className="spk" style={{ marginTop: 16 }}>
        <b>Wrong speakers?</b>
        <p className="faint" style={{ fontSize: 12, margin: "4px 0 8px" }}>Detect the voices again from the recording. Your text edits are kept;
          names you gave are reset and the notes are rewritten.</p>
        <div style={{ display: "flex", gap: 8 }}>
          <input className="input" type="number" min={1} max={20} placeholder="People (optional)" value={people}
            onChange={(e) => setPeople(e.target.value)} style={{ flex: 1 }} title="How many people were in the meeting, you included" />
          <button className="btn" onClick={redetect}>↻ Re-detect speakers</button>
        </div>
      </div>
    </div>
  );
}
