import { useEffect, useState } from "react";
import { setup } from "../api";
import { liveNamesCurrentlyOn, liveNamesNoticeEligible, hasSeenLiveNamesNotice, markLiveNamesNoticeSeen } from "../liveNamesOnboarding";
import type { Ctx } from "../App";
import { useI18n } from "../i18n";
import { I } from "./icons";

/** One-time notice (task 14, reports/13-designer-onboarding.md §7 "Returning users") for anyone
 * who already had `setup_complete: true` before the Welcome.tsx "Live speaker names" step
 * existed, or who skipped that step. Shows once, never reappears once acknowledged. Does NOT
 * restart onboarding, read, or write any settings — acknowledging it is not consent or
 * enrollment permission for anything; it only flips the `modwen.liveNamesNoticeSeen` localStorage
 * flag (see liveNamesOnboarding.ts).
 *
 * Task 15: scoped to the dashboard only — rendered from Home.tsx, not globally from App.tsx (see
 * App.tsx) — and deferred while a meeting is active (`ctx.recorder.recording`, the same "is a
 * meeting active" convention UpdateBanner.tsx/Home.tsx/Sidebar.tsx/Start.tsx already use).
 * Deferring never marks the notice seen, so it shows the next time the user is on the dashboard
 * and not recording. */
export default function LiveNamesNotice({ ctx }: { ctx: Ctx }) {
  const { tr } = useI18n();
  const { setupComplete, recorder } = ctx;
  const [seen, setSeen] = useState(() => hasSeenLiveNamesNotice());
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    if (setupComplete && !seen) setup.settings().then((r) => setSettings(r.settings)).catch(() => undefined);
  }, [setupComplete, seen]);
  if (!liveNamesNoticeEligible({ setupComplete, seen, recording: recorder.recording })) return null;
  const on = liveNamesCurrentlyOn(settings);
  const dismiss = () => {
    markLiveNamesNoticeSeen();
    setSeen(true);
  };
  return (
    <div className="banner info live-names-notice" role="status" style={{ margin: "0 16px 12px", flexWrap: "wrap", alignItems: "flex-start" }}>
      <I.voice size={16} style={{ marginTop: 2 }} />
      <span className="grow">
        <b>{tr("Live speaker names (on by default)", "التعرّف المباشر على المتحدثين (مفعّل تلقائياً)")}</b>{" "}
        {tr(
          "While you record, Modwen compares each speaker's voice against the voices you've already saved in People, with their consent, and shows a matched name on the live caption; everyone else stays Call or Room. Modwen never enrolls a new voice or groups unknown speakers on its own.",
          // Arabic: the CEO (a native Arabic speaker) reviewed and explicitly approved this reconciled
          // wording on 2026-11-07 — see eleven/decisions/0001-arabic-disclosure-wording.md (piece 3,
          // the returning-user banner's main sentence; its two decision-record paragraphs are merged
          // here into this single English paragraph's string).
          "أثناء التسجيل، يقارن مُدوَّن صوت كل متحدث ببصمات الأصوات المحفوظة مسبقاً في صفحة الأشخاص بموافقة أصحابها. إذا تطابق الصوت مع بصمة محفوظة، يظهر اسم صاحبه على النص المباشر. أما بقية المتحدثين، فيستمر ظهورهم باسم «المكالمة» أو «الغرفة». لا يحفظ مُدوَّن بصمات صوت جديدة أثناء الاجتماع، ولا يجمع المتحدثين غير المعروفين تلقائياً.")}{" "}
        {on
          ? tr("You can turn this off any time in Settings → Transcription.",
            // Arabic: CEO-approved 2026-11-07, decision record 0001 (banner closing line, feature on).
            "يمكنك إيقاف هذه الميزة في أي وقت من الإعدادات ← تحويل الكلام إلى نص.")
          : tr("This is currently turned off for you.",
            // Arabic: CEO-approved 2026-11-07, decision record 0001 (banner closing line, feature off).
            "هذا الخيار متوقف حالياً بالنسبة إليك.")}{" "}
        <a href="#/settings/privacy">{tr("How voices are handled", "كيف يتعامل مُدوَّن مع الأصوات")}</a>
      </span>
      <button className="btn sm ghost" onClick={dismiss}>{tr("Got it", "فهمت")}</button>
    </div>
  );
}
