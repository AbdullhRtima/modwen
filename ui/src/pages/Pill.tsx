import { useEffect, useState } from "react";
import { api, fmtTs } from "../api";
import type { Ctx } from "../App";
import { useI18n } from "../i18n";
import { I } from "../components/icons";

// Compact floating recording pill, opened in its own small browser window (#/pill).
// In v1 (Tauri) the same page becomes an always-on-top native window.
export default function Pill({ ctx }: { ctx: Ctx }) {
  const { t } = useI18n();
  const r = ctx.recorder;
  const [elapsed, setElapsed] = useState(r.elapsed_ms ?? 0);

  useEffect(() => {
    document.title = r.recording ? "● REC · Modwen" : "Modwen";
  }, [r.recording]);
  useEffect(() => {
    setElapsed(r.elapsed_ms ?? 0);
    if (!r.recording || r.paused) return;
    const t = window.setInterval(() => setElapsed((e) => e + 1000), 1000);
    return () => window.clearInterval(t);
  }, [r.elapsed_ms, r.paused, r.recording]);

  const openMain = (hash: string) => {
    if (window.opener && !window.opener.closed) {
      window.opener.location.hash = hash;
      window.opener.focus();
    } else {
      window.open(`${location.origin}/${hash}`, "modwen-main");
    }
  };
  const stop = async () => {
    const { meeting_id } = await api.stop();
    openMain(`#/meetings/${meeting_id}`);
    window.close();
  };

  if (!r.recording) {
    return (
      <div className="pill-page">
        <div className="pill"><div className="pill-row">
          <span>{t("notRecording")}</span>
          <span style={{ flex: 1 }} />
          <button className="pb" onClick={() => openMain("#/start")}><I.record size={14} />{t("record")}</button>
          <button className="pb" aria-label="Close" onClick={() => window.close()}><I.close size={14} /></button>
        </div></div>
      </div>
    );
  }

  const levels = r.levels ?? {};
  const warnings = Object.values(r.warnings ?? {});
  const meter = (label: string, key: string) =>
    key in levels ? <span className="meter">{label}<span className="bar"><i style={{ width: `${levels[key] * 100}%` }} /></span></span> : null;

  return (
    <div className="pill-page">
      <div className="pill">
        <div className="pill-row">
          <span className={`dot ${r.paused ? "" : "pulse"}`} style={{ color: "var(--rec)" }} />
          <span>{r.paused ? t("paused") : t("rec")}</span>
          <span className="timer">{fmtTs(elapsed)}</span>
          <span className="meters">{meter(t("you"), "mic")}{meter(t("call"), "system")}{meter(t("room"), "room")}</span>
        </div>
        <div className="pill-row">
          {warnings.length ? <span className="badge warn" title={warnings.join(" · ")}><I.warn size={13} />{warnings[0]}</span>
            : <span style={{ fontSize: 12, opacity: .8, display: "inline-flex", gap: 5, alignItems: "center" }}><I.lock size={12} />{t("localOnly")}</span>}
          <span style={{ flex: 1 }} />
          <button className="pb" title={t("mark")} aria-label={t("mark")} onClick={() => api.mark()}><I.mark size={14} /></button>
          {r.paused
            ? <button className="pb" title={t("resume")} aria-label={t("resume")} onClick={() => api.resume().then(ctx.refreshStatus)}><I.play size={14} /></button>
            : <button className="pb" title={t("pause")} aria-label={t("pause")} onClick={() => api.pause().then(ctx.refreshStatus)}><I.pause size={14} /></button>}
          <button className="pb stop" onClick={stop}><I.stop size={12} fill="currentColor" />{t("stop")}</button>
          <button className="pb" title={t("openLive")} aria-label={t("openLive")} onClick={() => openMain("#/live")}><I.live size={14} /></button>
        </div>
      </div>
    </div>
  );
}

export function openPill(): Window | null {
  return window.open(`${location.origin}/#/pill`, "modwen-pill",
    "popup=yes,width=470,height=150,left=40,top=40,resizable=yes");
}
