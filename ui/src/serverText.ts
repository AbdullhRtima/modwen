// Messages the server writes in English (notes warnings, recording warnings), shown in Arabic when the interface
// is Arabic. Each pattern mirrors one message in modwen/notes/engine.py or modwen/pipeline/recorder.py; a message
// with no match is shown as it is, so a new server message is never lost, only untranslated.
import { ApiError } from "./api";
import type { Key } from "./i18n";

type Rule = [RegExp, (m: RegExpMatchArray) => string];

const AR: Rule[] = [
  // notes (modwen/notes/engine.py)
  [/^Dropped a deadline not found in the transcript \((.+)\)$/, (m) => `حُذف موعد لم يُذكر في النص (${m[1]})`],
  [/^(\d+) item\(s\) without transcript evidence \(low confidence\)$/, (m) => `${m[1]} عنصر بلا دليل من النص (ثقة منخفضة)`],
  [/^(\d+) action item\(s\) without an owner$/, (m) => `${m[1]} مهمة بلا مسؤول`],
  [/^(\d+) action item\(s\) assigned to an unnamed speaker: rename speakers$/,
    (m) => `${m[1]} مهمة مسندة إلى متحدث بلا اسم: سمِّ المتحدثين`],
  [/^Summary \((\w+)\) failed: (.*)$/, (m) => `تعذّر إنشاء الملخص (${m[1]}): ${m[2]}`],
  [/^Modwen closed before any audio was saved \(audio is saved every (\d+) s\)\. Nothing to process\.$/,
    (m) => `أُغلق مُدوَّن قبل حفظ أي صوت (يُحفظ الصوت كل ${m[1]} ث). لا يوجد ما يُعالج.`],
  // recording (modwen/pipeline/recorder.py)
  [/^Bluetooth mic lowers accuracy\. Use built-in or wired\.$/, () => "ميكروفون البلوتوث يقلل الدقة. استخدم الميكروفون المدمج أو السلكي."],
  [/^The (microphone|system audio) device changed; recording continues on (.*)\.$/,
    (m) => `تغيّر جهاز ${m[1] === "microphone" ? "الميكروفون" : "صوت النظام"}؛ يستمر التسجيل على ${m[2]}.`],
  [/^Lost the (\w+) device and could not reopen it: (.*)$/, (m) => `انقطع جهاز ${dev(m[1])} وتعذّرت إعادة فتحه: ${m[2]}`],
  [/^No (.+) for (\d+) s\. Is the meeting muted\?$/, (m) => `لا يوجد ${dev(m[1])} منذ ${m[2]} ث. هل الاجتماع مكتوم؟`],
  [/^Recording for ([\d.]+) h; safety stop at ([\d.]+) h\.$/, (m) => `التسجيل مستمر منذ ${m[1]} ساعة؛ يتوقف تلقائياً عند ${m[2]} ساعة.`],
  [/^Low disk space \(([\d.]+) GB\); stopping to protect the recording\.$/, (m) => `المساحة منخفضة (${m[1]} GB)؛ أُوقف التسجيل لحمايته.`],
  [/^This computer is busy; live captions are paused\. The recording is safe\.$/, () => "الجهاز مشغول؛ توقف النص المباشر مؤقتاً. التسجيل بأمان."],
  [/^The audio device dropped sound (\d+) time\(s\); timing was kept\. Close heavy apps if this keeps happening\.$/,
    (m) => `فقد جهاز الصوت جزءاً من الصوت ${m[1]} مرة؛ حُفظ التوقيت. أغلق التطبيقات الثقيلة إذا تكرر ذلك.`],
];

function dev(name: string): string {
  return ({ mic: "الميكروفون", microphone: "الميكروفون", system: "صوت النظام", "system audio": "صوت النظام", room: "صوت الغرفة" } as Record<string, string>)[name] ?? name;
}

export function serverText(msg: string, lang: string): string {
  if (lang !== "ar") return msg;
  for (const [re, fn] of AR) {
    const m = msg.match(re);
    if (m) return fn(m);
  }
  return msg;
}

// Import refusals (modwen/audio/importer.py). Unlike the messages above, a refusal with no match is NOT shown: an
// import shows one of these or the general failure message, never raw text. tests/test_import_ui_text.py checks
// them against the engine's strings.
const REFUSALS: [RegExp, (m: RegExpMatchArray, t: (k: Key) => string) => string][] = [
  [/^This file has no audio, so there's nothing to transcribe\. Choose a file with sound\.$/,
    () => "هذا الملف لا يحتوي على صوت. اختر ملفاً يحتوي على تسجيل صوتي."],
  [/^This isn't a supported audio or video file\. It may be copy-protected or use a format Modwen can't read\. Use WAV, FLAC, MP3, OGG, AIFF, MP4, MOV or M4A\.$/,
    () => "لا يمكن استيراد هذا الملف. قد يكون محمياً من النسخ أو بصيغة لا يدعمها مُدوَّن. استخدم ملف WAV أو FLAC أو MP3 أو OGG أو AIFF أو MP4 أو MOV أو M4A."],
  [/^This file is damaged or incomplete, so nothing was imported\. If you can, get a fresh copy and import that\.$/,
    () => "هذا الملف تالف أو غير مكتمل، فلم يُستورد شيء منه. جرّب نسخة سليمة منه إن توفّرت."],
  [/^This recording is ([\d.]+(?:e\+\d+)?) hours long\. Imports are limited to ([\d.]+) hours\. Trim or split it, then import it again\.$/,
    (m) => {
      const h = Number(m[1]);
      const unit = Number.isInteger(h) && h >= 7 && h <= 10 ? "ساعات" : "ساعة";
      return `مدة هذا التسجيل ${m[1]} ${unit}، والحد الأقصى للاستيراد ${m[2]} ساعات. اختصره أو قسّمه، ثم استورده من جديد.`;
    }],
  [/^This file is larger than 8 GB, the limit for imports\. Use a smaller copy and import that\.$/, (_m, t) => t("importTooBig")],
  [/^This file is empty\. Choose the original recording and import it again\.$/,
    () => "هذا الملف فارغ. اختر التسجيل الأصلي واستورده من جديد."],
];

/** What to tell the user about a failed import: a known refusal in their language, otherwise the general message. */
export function importMessage(err: unknown, t: (k: Key) => string, lang: string): string {
  if (err instanceof ApiError && (err.status === 400 || err.status === 413)) {
    for (const [re, fn] of REFUSALS) {
      const m = err.message.match(re);
      if (m) return lang === "ar" ? fn(m, t) : err.message;
    }
  }
  return t("importFailed");
}
