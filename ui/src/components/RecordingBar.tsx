import { useEffect, useState } from "react";
import { api, fmtTs } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import { useI18n } from "../i18n";
import { openPill } from "../pages/Pill";

/** The floating recording pill: shown on every page while a meeting is being recorded. */
export default function RecordingBar({ ctx, onLive = false }: { ctx: Ctx; onLive?: boolean }) {
  const { t } = useI18n();
  const r = ctx.recorder;
  const [elapsed, setElapsed] = useState(r.elapsed_ms ?? 0);
  useEffect(() => {
    setElapsed(r.elapsed_ms ?? 0);
    if (r.paused) return;
    const timer = window.setInterval(() => setElapsed((e) => e + 1000), 1000);
    return () => window.clearInterval(timer);
  }, [r.elapsed_ms, r.paused]);

  const warnings = Object.values(r.warnings ?? {});
  const levels = r.levels ?? {};
  const act = (fn: () => Promise<unknown>, ok?: string) => fn().then(() => { ctx.refreshStatus(); if (ok) ctx.toast(ok); }, ctx.fail);
  const stop = async () => {
    try {
      const { meeting_id } = await api.stop();
      ctx.refreshStatus();
      ctx.go(`#/meetings/${meeting_id}`);
    } catch (e) {
      ctx.fail(e);
    }
  };
  const meter = (label: string, key: string) => key in levels
    ? <span className="meter">{label}<span className="bar"><i style={{ width: `${Math.round(levels[key] * 100)}%` }} /></span></span> : null;

  return (
    <div className="pillbar" role="region" aria-label={t("stRecording")}>
      <span className={`rd ${r.paused ? "" : "live"}`} aria-hidden="true" />
      <span>{r.paused ? t("paused") : t("rec")}</span>
      <span className="timer">{fmtTs(elapsed)}</span>
      <span className="meters">{meter(t("you"), "mic")}{meter(t("call"), "system")}{meter(t("room"), "room")}</span>
      {warnings.length > 0 && <span className="warnings" title={warnings.join(" · ")}><I.warn size={14} /> {warnings[0]}</span>}
      <button className="pb" onClick={() => act(() => api.mark(), t("marked"))}><I.mark size={15} />{t("mark")}</button>
      {r.paused
        ? <button className="pb" onClick={() => act(() => api.resume())}><I.play size={15} />{t("resume")}</button>
        : <button className="pb" onClick={() => act(() => api.pause())}><I.pause size={15} />{t("pause")}</button>}
      {!onLive && <a className="pb" href="#/live"><I.live size={15} />{t("openLive")}</a>}
      <button className="pb" title={t("popOut")} aria-label={t("popOut")} onClick={() => openPill()}><I.popOut size={15} /></button>
      <button className="pb stop" onClick={stop}><I.stop size={13} fill="currentColor" />{t("stop")}</button>
    </div>
  );
}
