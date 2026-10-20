// Messages the server writes in English (notes warnings, recording warnings), shown in Arabic when the interface
// is Arabic. Each pattern mirrors one message in modwen/notes/engine.py or modwen/pipeline/recorder.py; a message
// with no match is shown as it is, so a new server message is never lost, only untranslated.

type Rule = [RegExp, (m: RegExpMatchArray) => string];

const AR: Rule[] = [
  // notes (modwen/notes/engine.py)
  [/^Dropped a deadline not found in the transcript \((.+)\)$/, (m) => `حُذف موعد لم يُذكر في النص (${m[1]})`],
  [/^(\d+) item\(s\) without transcript evidence \(low confidence\)$/, (m) => `${m[1]} عنصر بلا دليل من النص (ثقة منخفضة)`],
  [/^(\d+) action item\(s\) without an owner$/, (m) => `${m[1]} مهمة بلا مسؤول`],
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
