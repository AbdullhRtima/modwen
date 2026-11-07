import { useEffect, useRef, useState } from "react";
import { useServerEvents } from "../events";
import { api, fmtTs, LiveLine, LiveSpeakerState } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import { useI18n } from "../i18n";
import { liveSpeakerTooltip } from "../liveSpeakerTooltip";

const NAMES_DEBOUNCE_MS = 5000;

/** True once `active` has held for 5 s, and stays true until it has been false for 5 s (no banner flicker). */
function useDebounced(active: boolean, ms = NAMES_DEBOUNCE_MS): boolean {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (active === shown) return;
    const h = setTimeout(() => setShown(active), ms);
    return () => clearTimeout(h);
  }, [active, shown, ms]);
  return shown;
}

const sameId = (a: LiveLine, id: unknown) => a.id !== undefined && a.id !== null && String(a.id) === String(id);

/** Late name for one line. Only upgrades an unnamed or named line (last event wins); never removes a name.
 *  Unknown ids (trimmed, or not seen yet) are ignored. */
export function applyLiveSpeaker(lines: LiveLine[], id: unknown, name: unknown, state: unknown): LiveLine[] {
  if (id === undefined || id === null || typeof name !== "string" || !name || (state !== "named" && state !== "maybe")) return lines;
  const i = lines.findIndex((l) => sameId(l, id));
  if (i < 0) return lines;
  const next = lines.slice();
  next[i] = { ...lines[i], speaker_name: name, speaker_state: state as LiveSpeakerState };
  return next;
}

/** A line list fetched from the server (reload or resync) replaces ours, but never drops a name we already hold. */
export function mergeFetched(prev: LiveLine[], fetched: LiveLine[]): LiveLine[] {
  return fetched.map((l) => {
    if (l.speaker_state) return l;
    const old = prev.find((p) => sameId(p, l.id));
    return old?.speaker_state ? { ...l, speaker_name: old.speaker_name, speaker_state: old.speaker_state } : l;
  });
}

/** The one speaker label: the channel label, or a provisional name (dotted underline, "?" when unsure). */
function SpeakerLabel({ line }: { line: LiveLine }) {
  const { t } = useI18n();
  const base = line.channel === "mic" ? t("you") : line.channel === "room" ? t("room") : t("call");
  const state = line.speaker_state;
  const raw = line.speaker_name;
  if ((state !== "named" && state !== "maybe") || !raw) return <span className="nm">{base}</span>;
  // The self person is saved as the literal "You"; show the translated label, as Review does.
  const name = raw === "You" ? t("you") : raw;
  const maybe = state === "maybe";
  const tip = liveSpeakerTooltip(maybe, name, t("liveNameMaybe"), t("liveNameGuess"));
  return (
    <span className="nm named" title={tip}>
      <span className="guess"><bdi>{name}</bdi></span>
      {maybe && <span>{t("liveQMark")}</span>}
      <span className="sr">, {maybe ? t("liveNameSrMaybe") : t("liveNameSrGuess")}</span>
    </span>
  );
}

export default function Live({ ctx }: { ctx: Ctx }) {
  const { t } = useI18n();
  const r = ctx.recorder;
  const [lines, setLines] = useState<LiveLine[]>([]);
  const [follow, setFollow] = useState(true);
  const [livePaused, setLivePaused] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  const names = r.live?.names;
  const namesPausedShown = useDebounced(names === "paused");
  useEffect(() => {
    if (!r.meeting_id) return;
    let stale = false;
    api.live(r.meeting_id).then((d) => { if (!stale) setLines((prev) => mergeFetched(prev, d.lines)); }, () => undefined);
    return () => { stale = true; };
  }, [r.meeting_id]);
  useServerEvents(ctx, (e) => {
    // Events carry meeting_id; anything for another meeting is ignored.
    if (e.type === "live_line" && e.meeting_id === r.meeting_id) setLines((prev) => [...prev.slice(-499), e.line as LiveLine]);
    if (e.type === "live_speaker" && e.meeting_id === r.meeting_id)
      setLines((prev) => applyLiveSpeaker(prev, e.id, e.speaker_name, e.speaker_state));
    if (e.type === "live_status") setLivePaused(Boolean(e.paused));
    if (e.type === "resync" && r.meeting_id) {
      api.live(r.meeting_id).then((d) => setLines((prev) => mergeFetched(prev, d.lines)), () => undefined);
    }
  });
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  if (!r.recording) {
    return <div className="empty"><h2>{t("notRecording")}</h2><p><a href="#/start">{t("record")}</a> · <a href="#/">{t("back")}</a></p></div>;
  }
  // Names banner when the server reports `names`; an older server sends none, so the old banner stays.
  const namesBanner = !livePaused && (names === "on" || names === "paused");
  const bannerWarn = livePaused || (namesBanner && namesPausedShown);
  const bannerText = livePaused ? t("livePaused") : namesBanner ? (namesPausedShown ? t("liveNamesPaused") : t("liveDraftNames")) : t("liveDraft");

  return (
    <>
      <div className="page-head"><h1>{t("liveTitle")}</h1>
        <span className="faint" style={{ fontSize: 12.5 }}>{Object.values(r.sources ?? {}).join(" · ")}</span></div>
      {r.live?.error && <div className="banner warn"><I.warn />{r.live.error}</div>}
      <div className={`banner ${bannerWarn ? "warn" : "info"}`}>{bannerWarn ? <I.warn /> : <I.info />}{bannerText}</div>
      <section className="card live-stage" aria-live="polite" aria-relevant="additions">
        <div className="live-box" ref={box} onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        }}>
          {lines.length === 0 && <p className="faint">{t("listening")}</p>}
          {lines.map((l, i) => (
            <div key={l.id !== undefined && l.id !== null ? `${r.meeting_id}:${l.id}` : i} className="cap">
              <span className="w" style={{ color: l.channel === "mic" ? "var(--sp1)" : "var(--sp2)" }}><SpeakerLabel line={l} /><span className="ts"><bdi dir="ltr">{fmtTs(l.start_ms)}</bdi></span></span>
              <span className="x" dir="auto">{l.text}</span>
            </div>
          ))}
        </div>
        {!follow && <button className="btn sm" style={{ marginTop: 12 }} onClick={() => setFollow(true)}><I.down size={14} />{t("jumpLatest")}</button>}
      </section>
    </>
  );
}
