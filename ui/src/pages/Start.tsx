import { useEffect, useState } from "react";
import { api } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import { openPill } from "./Pill";
import { useI18n } from "../i18n";

const NOTICE_EN = "Heads-up: I'm using Modwen to record and transcribe this meeting for notes. The audio stays on my machine. Tell me if you'd prefer I stop.";
const NOTICE_AR = "تنبيه: أستخدم أداة Modwen لتسجيل هذا الاجتماع وتفريغه لكتابة المحضر. التسجيل يبقى على جهازي فقط. أخبروني إذا كنتم تفضلون إيقافه.";

export default function Start({ ctx }: { ctx: Ctx }) {
  const { t } = useI18n();
  const now = new Date();
  const [title, setTitle] = useState(`Meeting ${now.toLocaleDateString()} ${now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
  const [mode, setMode] = useState<"online" | "in_person">(location.hash.includes("in_person") ? "in_person" : "online");
  const [mic, setMic] = useState(true);
  const [system, setSystem] = useState(true);
  const [lang, setLang] = useState<"auto" | "en" | "ar">("auto");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [micName, setMicName] = useState("default microphone");
  const [showPill, setShowPill] = useState(true);

  useEffect(() => {
    api.devices().then((d) => {
      const def = d.inputs.find((i) => i.default);
      if (def) setMicName(def.name);
    }).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (ctx.recorder.recording) ctx.go("#/live");
  }, [ctx.recorder.recording]);

  const bluetooth = /airpods|bluetooth|hands-free|headset|buds/i.test(micName);
  const copy = (text: string) => navigator.clipboard.writeText(text).then(() => ctx.toast(t("copied")), ctx.fail);

  const start = async () => {
    setBusy(true);
    setError(null);
    const pill = showPill ? openPill() : null;
    try {
      const sources = mode === "in_person" ? ["mic"] : [mic && "mic", system && "system"].filter(Boolean) as string[];
      await api.start({ title, mode, meeting_language: lang, sources, consent_confirmed: consent });
      ctx.refreshStatus();
      ctx.go("#/live");
    } catch (e) {
      pill?.close();
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head"><h1>{t("newMeeting")}</h1><span className="lock"><I.lock size={14} />{t("localOnly")}</span></div>
      <div className="card" style={{ display: "grid", gap: 18 }}>
        <div className="field" style={{ marginBottom: 0 }}><label htmlFor="mtitle">{t("title")}</label>
          <input id="mtitle" className="input" value={title} dir="auto" onChange={(e) => setTitle(e.target.value)} autoFocus /></div>
        <div className="field" style={{ marginBottom: 0, alignItems: "start" }}><label>{t("type")}</label>
          <div className="choice">
            <button className={mode === "online" ? "on" : ""} onClick={() => setMode("online")}><b>{t("online")}</b><span>{t("onlineHint")}</span></button>
            <button className={mode === "in_person" ? "on" : ""} onClick={() => setMode("in_person")}><b>{t("inPerson")}</b><span>{t("inPersonHint")}</span></button>
          </div></div>
        <div className="field" style={{ marginBottom: 0 }}><label>{t("audio")}</label>
          <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
            <label className="check"><input type="checkbox" checked={mic || mode === "in_person"} disabled={mode === "in_person"}
              onChange={(e) => setMic(e.target.checked)} />{t("myMic")} <span className="faint">· {micName}</span></label>
            {mode === "online" && <label className="check"><input type="checkbox" checked={system}
              onChange={(e) => setSystem(e.target.checked)} />{t("callAudio")}</label>}
          </div></div>
        <div className="field" style={{ marginBottom: 0 }}><label>{t("language")}</label>
          <div className="seg">
            <button className={lang === "auto" ? "on" : ""} onClick={() => setLang("auto")}>{t("langAuto")}</button>
            <button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>{t("langEn")}</button>
            <button className={lang === "ar" ? "on" : ""} onClick={() => setLang("ar")}>{t("langAr")}</button>
          </div></div>
        {bluetooth && <div className="banner warn" style={{ margin: 0 }}><I.warn />{t("btWarn")}</div>}
        <div style={{ background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: 10, padding: "14px 16px" }}>
          <p style={{ margin: "0 0 10px", fontWeight: 600 }}>{t("consentTitle")}</p>
          <div className="notice" dir="ltr">{NOTICE_EN}</div>
          <div className="notice" dir="rtl">{NOTICE_AR}</div>
          <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
            <button className="btn sm" onClick={() => copy(NOTICE_EN)}><I.copy size={14} />{t("copyEn")}</button>
            <button className="btn sm" onClick={() => copy(NOTICE_AR)}><I.copy size={14} />{t("copyAr")}</button>
          </div>
          <label className="check"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />{t("informed")}</label>
        </div>
        <label className="check"><input type="checkbox" checked={showPill} onChange={(e) => setShowPill(e.target.checked)} />{t("showPill")}</label>
        {error && <div className="banner err" style={{ margin: 0 }}>{error}</div>}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="btn" onClick={() => ctx.go("#/")}>{t("cancel")}</button>
          <button className="btn danger lg" disabled={!consent || busy || (mode === "online" && !mic && !system)} onClick={start}>
            {busy ? <I.spin className="spin-anim" /> : <I.record />}{busy ? t("starting") : t("start")}</button>
        </div>
      </div>
    </>
  );
}
