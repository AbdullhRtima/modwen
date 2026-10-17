import { useEffect, useState } from "react";
import { api } from "../api";
import type { Ctx } from "../App";
import { openPill } from "./Pill";

const NOTICE_EN = "Heads-up: I'm using Modwen to record and transcribe this meeting for notes. The audio stays on my machine. Tell me if you'd prefer I stop.";
const NOTICE_AR = "تنبيه: أستخدم أداة Modwen لتسجيل هذا الاجتماع وتفريغه لكتابة المحضر. التسجيل يبقى على جهازي فقط. أخبروني إذا كنتم تفضلون إيقافه.";

export default function Start({ ctx }: { ctx: Ctx }) {
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
  const copy = (text: string, label: string) =>
    navigator.clipboard.writeText(text).then(() => ctx.toast(`${label} copied`));

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
    <div className="card" style={{ maxWidth: 600 }}>
      <h1 style={{ marginTop: 0, fontSize: 18 }}>Start meeting</h1>
      <div className="field"><label>Title</label>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus /></div>
      <div className="field"><label>Type</label>
        <div className="seg">
          <button className={mode === "online" ? "on" : ""} onClick={() => setMode("online")}>Online (Meet, Teams)</button>
          <button className={mode === "in_person" ? "on" : ""} onClick={() => setMode("in_person")}>In-person</button>
        </div></div>
      <div className="field"><label>Audio</label>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          <label className="check"><input type="checkbox" checked={mic || mode === "in_person"} disabled={mode === "in_person"}
            onChange={(e) => setMic(e.target.checked)} />My mic: {micName}</label>
          {mode === "online" && <label className="check"><input type="checkbox" checked={system}
            onChange={(e) => setSystem(e.target.checked)} />System audio (the meeting)</label>}
        </div></div>
      <div className="field"><label>Language</label>
        <div className="seg">
          <button className={lang === "auto" ? "on" : ""} onClick={() => setLang("auto")}>Auto (Arabic + English)</button>
          <button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>English only</button>
          <button className={lang === "ar" ? "on" : ""} onClick={() => setLang("ar")}>Arabic only</button>
        </div></div>
      <div className="field"><label>Processing</label><span className="lock">🔒 Local only: nothing leaves this computer</span></div>
      {bluetooth && <div className="banner warn">⚠ Bluetooth mic lowers accuracy. Use the built-in or a wired mic.</div>}
      <div className="card" style={{ background: "var(--surface-2)", marginTop: 6 }}>
        <p style={{ margin: "0 0 10px" }}><b>Let participants know they're being recorded.</b></p>
        <div className="notice">{NOTICE_EN}</div>
        <div className="notice" dir="rtl" style={{ textAlign: "right" }}>{NOTICE_AR}</div>
        <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
          <button className="btn sm" onClick={() => copy(NOTICE_EN, "Notice (EN)")}>Copy notice EN</button>
          <button className="btn sm" onClick={() => copy(NOTICE_AR, "Notice (AR)")}>Copy notice AR</button>
        </div>
        <label className="check"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          I have informed participants</label>
      </div>
      <label className="check" style={{ marginTop: 12 }}><input type="checkbox" checked={showPill}
        onChange={(e) => setShowPill(e.target.checked)} />Show the floating recording pill</label>
      {error && <div className="banner err" style={{ marginTop: 12 }}>{error}</div>}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button className="btn" onClick={() => ctx.go("#/")}>Cancel</button>
        <button className="btn danger" disabled={!consent || busy || (mode === "online" && !mic && !system)} onClick={start}>
          {busy ? "Starting…" : "● Start"}</button>
      </div>
    </div>
  );
}
