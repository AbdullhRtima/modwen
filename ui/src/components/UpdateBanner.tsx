import { useEffect, useState } from "react";
import type { Ctx } from "../App";
import { desktop, UpdateInfo } from "../desktop";
import { useI18n } from "../i18n";
import { I } from "./icons";

/** Desktop app only: "Modwen X is available" with Install / Later (design: docs/planning/04-desktop-updates.md). */
export default function UpdateBanner({ ctx }: { ctx: Ctx }) {
  const { tr } = useI18n();
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [hidden, setHidden] = useState<string | null>(() => {
    try { return localStorage.getItem("modwen.update.later"); } catch { return null; }
  });
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!desktop) return;
    desktop.core.invoke<UpdateInfo | null>("update_info").then(setInfo, () => undefined);
    let stop: (() => void) | undefined;
    desktop.event.listen<UpdateInfo>("update-available", (e) => setInfo(e.payload)).then((u) => (stop = u), () => undefined);
    return () => stop?.();
  }, []);
  if (!desktop || !info || hidden === info.version) return null;
  const busyNow = ctx.recorder.recording;
  const install = async () => {
    setBusy(true);
    try {
      await desktop!.core.invoke("install_update");  // restarts the app when it's done
    } catch (e) {
      ctx.fail(new Error(String(e)));
      setBusy(false);
    }
  };
  const later = () => {
    try { localStorage.setItem("modwen.update.later", info.version); } catch { /* storage unavailable */ }
    setHidden(info.version);
  };
  return (
    <div className="update-banner" role="status">
      <I.download size={16} />
      <span className="grow"><b>{tr(`Modwen ${info.version} is available.`, `يتوفر مُدوَّن ${info.version}.`)}</b>{" "}
        {busyNow ? tr("It installs when the recording has finished.", "يُثبَّت بعد انتهاء التسجيل.")
          : tr("Your meetings and settings are kept.", "تبقى اجتماعاتك وإعداداتك.")}</span>
      {info.notes && <button className="btn sm ghost" onClick={() => setOpen(!open)}>{tr("What's new", "ما الجديد")}</button>}
      <button className="btn sm primary" disabled={busy || busyNow} onClick={install}>
        {busy ? <I.spin size={14} className="spin-anim" /> : null}{busy ? tr("Installing…", "جارٍ التثبيت…") : tr("Install and restart", "ثبّت وأعد التشغيل")}</button>
      <button className="btn sm ghost" onClick={later}>{tr("Later", "لاحقاً")}</button>
      {open && <pre className="update-notes" dir="auto">{info.notes}</pre>}
    </div>
  );
}
