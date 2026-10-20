// Interface language (English / Arabic, with a mirrored RTL layout) and theme (system / light / dark).
// Both are per-browser preferences, remembered in localStorage.
import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type Lang = "en" | "ar";
export type Theme = "system" | "light" | "dark";

const STRINGS = {
  // shell
  record: ["Record a meeting", "سجّل اجتماعاً"],
  recordingNow: ["Recording…", "يسجّل الآن…"],
  search: ["Search meetings…", "ابحث في الاجتماعات…"],
  people: ["People", "الأشخاص"],
  settings: ["Settings", "الإعدادات"],
  meetings: ["Meetings", "الاجتماعات"],
  localOnly: ["Local only · nothing leaves this computer", "محلي فقط · لا شيء يغادر هذا الجهاز"],
  today: ["Today", "اليوم"],
  yesterday: ["Yesterday", "أمس"],
  thisWeek: ["Earlier this week", "في وقت سابق هذا الأسبوع"],
  older: ["Older", "أقدم"],
  noMatch: ["No meetings match", "لا توجد اجتماعات مطابقة"],
  loading: ["Loading…", "جارٍ التحميل…"],
  // statuses
  stRecording: ["Recording", "يسجّل"],
  stPaused: ["Paused", "متوقف مؤقتاً"],
  stProcessing: ["Processing", "قيد المعالجة"],
  stReady: ["Notes ready", "الملاحظات جاهزة"],
  stNew: ["New", "جديد"],
  stFailed: ["Failed: retry", "فشل: أعد المحاولة"],
  retry: ["Retry", "أعد المحاولة"],
  // home
  homeTitle: ["Your meetings", "اجتماعاتك"],
  homeLead: ["Record a call or a meeting in the room. Modwen writes the transcript and the notes on this computer.",
    "سجّل مكالمة أو اجتماعاً في الغرفة. يكتب مُدوَّن النص الكامل والملاحظات على هذا الجهاز."],
  importRec: ["Import a recording", "استورد تسجيلاً"],
  importing: ["Importing…", "جارٍ الاستيراد…"],
  importHint: ["WAV, FLAC, MP3, OGG or AIFF", "WAV أو FLAC أو MP3 أو OGG أو AIFF"],
  imported: ["Recording imported; processing on this computer", "تم الاستيراد، وتجري المعالجة على هذا الجهاز"],
  needsAttention: ["Needs your attention", "تحتاج انتباهك"],
  recent: ["Recent", "الأحدث"],
  emptyTitle: ["No meetings yet", "لا توجد اجتماعات بعد"],
  emptyLead: ["Press Record when your Meet, Zoom or Teams call starts, or import an audio file to try Modwen.",
    "اضغط «سجّل» عند بدء مكالمة Meet أو Zoom أو Teams، أو استورد ملفاً صوتياً لتجربة مُدوَّن."],
  speakersN: ["speakers", "متحدثين"],
  speaker1: ["speaker", "متحدث"],
  actionsN: ["action items", "مهام"],
  // start
  newMeeting: ["New meeting", "اجتماع جديد"],
  title: ["Title", "العنوان"],
  type: ["Type", "النوع"],
  online: ["Online call", "مكالمة عبر الإنترنت"],
  onlineHint: ["Meet, Zoom, Teams…", "Meet أو Zoom أو Teams…"],
  inPerson: ["In the room", "في الغرفة"],
  inPersonHint: ["One microphone, everyone around it", "ميكروفون واحد للجميع"],
  audio: ["Audio", "الصوت"],
  myMic: ["My microphone", "الميكروفون"],
  callAudio: ["The call's audio", "صوت المكالمة"],
  language: ["Language", "اللغة"],
  langAuto: ["Arabic + English", "عربي + إنجليزي"],
  langEn: ["English only", "إنجليزي فقط"],
  langAr: ["Arabic only", "عربي فقط"],
  consentTitle: ["Let everyone know they're being recorded", "أخبر الجميع أن الاجتماع يُسجَّل"],
  copyEn: ["Copy notice (EN)", "انسخ التنبيه (EN)"],
  copyAr: ["Copy notice (AR)", "انسخ التنبيه (AR)"],
  informed: ["I've told the participants", "أخبرتُ المشاركين"],
  showPill: ["Show the floating recording pill", "أظهر شريط التسجيل العائم"],
  btWarn: ["Bluetooth mics lower accuracy. Use the built-in or a wired mic.",
    "ميكروفونات البلوتوث تقلّل الدقة. استخدم الميكروفون المدمج أو السلكي."],
  cancel: ["Cancel", "إلغاء"],
  start: ["Start recording", "ابدأ التسجيل"],
  starting: ["Starting…", "جارٍ البدء…"],
  copied: ["Copied", "تم النسخ"],
  // recording
  rec: ["REC", "تسجيل"],
  paused: ["PAUSED", "متوقف"],
  mark: ["Mark", "علامة"],
  marked: ["Moment marked", "تم وضع علامة"],
  pause: ["Pause", "إيقاف مؤقت"],
  resume: ["Resume", "استئناف"],
  stop: ["Stop", "إنهاء"],
  you: ["You", "أنت"],
  call: ["Call", "المكالمة"],
  room: ["Room", "الغرفة"],
  openLive: ["Live captions", "الترجمة المباشرة"],
  popOut: ["Pop out", "نافذة عائمة"],
  notRecording: ["Not recording.", "لا يوجد تسجيل."],
  liveTitle: ["Live captions", "الترجمة المباشرة"],
  liveDraft: ["A quick draft while you talk. The accurate transcript, with names, is ready a minute after you stop.",
    "مسودة سريعة أثناء الحديث. النص الدقيق مع الأسماء يجهز بعد دقيقة من الإنهاء."],
  livePaused: ["Live captions paused because this computer is busy. The recording continues normally.",
    "الترجمة المباشرة متوقفة لأن الجهاز مشغول. التسجيل مستمر بشكل طبيعي."],
  listening: ["Listening… lines appear a few seconds after people speak.", "يستمع… تظهر الأسطر بعد ثوانٍ من الحديث."],
  jumpLatest: ["Jump to latest", "انتقل إلى الأحدث"],
  // review
  notes: ["Notes", "الملاحظات"],
  summary: ["Summary", "الملخّص"],
  decisions: ["Decisions", "القرارات"],
  actionItems: ["Action items", "المهام"],
  keyPoints: ["Key points", "النقاط الرئيسية"],
  openQuestions: ["Open questions", "أسئلة مفتوحة"],
  none: ["None recorded.", "لا يوجد."],
  transcript: ["Transcript", "النص الكامل"],
  lines: ["lines", "سطراً"],
  found: ["found", "نتيجة"],
  searchTranscript: ["Search the transcript", "ابحث في النص"],
  speakers: ["Speakers", "المتحدثون"],
  regenerate: ["Regenerate", "إعادة الكتابة"],
  regenSummary: ["Summary only (keeps your edits)", "الملخّص فقط (يحتفظ بتعديلاتك)"],
  regenAll: ["Whole notes (replaces your edits)", "كل الملاحظات (يستبدل تعديلاتك)"],
  regenTranscribe: ["Transcribe again, then notes", "أعد التفريغ ثم الملاحظات"],
  copy: ["Copy", "نسخ"],
  copyMd: ["Copy as Markdown", "انسخ بصيغة Markdown"],
  copyActions: ["Copy action items", "انسخ المهام"],
  exportMd: ["Export .md file", "صدّر ملف ‎.md"],
  notesCopied: ["Notes copied", "تم نسخ الملاحظات"],
  actionsCopied: ["Action items copied", "تم نسخ المهام"],
  editHint: ["Edit anything; changes save on their own", "عدّل أي شيء؛ التغييرات تُحفظ تلقائياً"],
  notesPending: ["Notes appear here when the AI model finishes.", "تظهر الملاحظات هنا عندما ينتهي نموذج الذكاء الاصطناعي."],
  notesFailed: ["Notes aren't available yet. Start your local AI model, then retry.",
    "الملاحظات غير متاحة بعد. شغّل نموذج الذكاء الاصطناعي المحلي ثم أعد المحاولة."],
  owner: ["Owner", "المسؤول"],
  task: ["Task", "المهمة"],
  due: ["Due", "الموعد"],
  said: ["said", "قيل"],
  noOwner: ["No owner", "بلا مسؤول"],
  recognised: ["recognised by voice", "تم التعرّف من الصوت"],
  notSure: ["not sure", "غير متأكد"],
  notSureQ: ["? not sure", "؟ غير متأكد"],
  whoSaid: ["Who said this? Click to change", "من قال هذا؟ اضغط للتغيير"],
  newPerson: ["+ New person", "+ شخص جديد"],
  overlap: ["overlap", "تداخل"],
  unnamedN: ["unnamed speaker(s)", "متحدث بلا اسم"],
  overlapN: ["overlapping line(s)", "سطر متداخل"],
  nameSpeakers: ["Name speakers", "سمِّ المتحدثين"],
  deleteMeeting: ["Delete meeting", "احذف الاجتماع"],
  deleteConfirm: ["Delete this meeting, its audio and notes? This can't be undone.",
    "حذف هذا الاجتماع وصوته وملاحظاته؟ لا يمكن التراجع."],
  notFound: ["Meeting not found.", "الاجتماع غير موجود."],
  back: ["Back to meetings", "العودة إلى الاجتماعات"],
  encrypted: ["Encrypted on this computer", "مشفّر على هذا الجهاز"],
  generating: ["Writing the summary on this computer…", "يُكتب الملخّص على هذا الجهاز…"],
  both: ["Both", "الاثنتان"],
  // processing
  audioSaved: ["Audio saved", "تم حفظ الصوت"],
  transcribing: ["Transcribing", "تفريغ الصوت"],
  identifying: ["Identifying speakers", "التعرّف على المتحدثين"],
  writingNotes: ["Writing notes", "كتابة الملاحظات"],
  partOf: ["part", "جزء"],
  minLeft: ["min left", "دقيقة متبقية"],
  closeOk: ["You can close this window; you'll get a notification.", "يمكنك إغلاق النافذة؛ سيصلك إشعار."],
  transcriptSaved: ["The transcript is saved.", "النص الكامل محفوظ."],
  retryNotes: ["Retry notes", "أعد كتابة الملاحظات"],
  aiSettings: ["AI model settings", "إعدادات نموذج الذكاء الاصطناعي"],
  // people
  peopleLead: ["Names you give in meetings live here. Rename someone and every meeting follows. Voices are encrypted and never leave this computer.",
    "الأسماء التي تعطيها في الاجتماعات تُحفظ هنا. غيّر الاسم مرة فيتغير في كل الاجتماعات. الأصوات مشفّرة ولا تغادر هذا الجهاز."],
  addPerson: ["Add person", "أضف شخصاً"],
  name: ["Name", "الاسم"],
  arabicName: ["Arabic name", "الاسم بالعربية"],
  email: ["Email", "البريد الإلكتروني"],
  optional: ["optional", "اختياري"],
  save: ["Save", "حفظ"],
  saved: ["Saved", "تم الحفظ"],
  edit: ["Edit", "تعديل"],
  del: ["Delete", "حذف"],
  thisIsMe: ["This is me", "هذا أنا"],
  isYou: ["you", "أنت"],
  setYourName: ["Edit to set your name", "عدّل لتضع اسمك"],
  voiceSaved: ["Voice saved", "الصوت محفوظ"],
  noVoice: ["No voice saved", "لا يوجد صوت محفوظ"],
  deleteVoice: ["Delete voice", "احذف الصوت"],
  noPeople: ["No people yet. Name a speaker in a meeting, or add the colleagues you meet most.",
    "لا يوجد أشخاص بعد. سمِّ متحدثاً في اجتماع، أو أضف الزملاء الذين تقابلهم كثيراً."],
  samples: ["sample(s)", "عينة"],
  // settings / appearance
  appearance: ["Appearance", "المظهر"],
  interfaceLang: ["Interface language", "لغة الواجهة"],
  theme: ["Theme", "السمة"],
  system: ["System", "النظام"],
  light: ["Light", "فاتح"],
  dark: ["Dark", "داكن"],
  // errors / connection
  offline: ["Can't reach Modwen. Is it still running? Retrying…", "تعذّر الوصول إلى مُدوَّن. هل ما زال يعمل؟ نعيد المحاولة…"],
  needsLink: ["This page needs the session link. Open Modwen from the tray icon, or use the link printed by modwen serve (it ends with ?token=…).",
    "هذه الصفحة تحتاج رابط الجلسة. افتح مُدوَّن من أيقونة شريط المهام، أو استخدم الرابط الذي يطبعه modwen serve (ينتهي بـ ‎?token=…)."],
  wrong: ["Something went wrong", "حدث خطأ ما"],
} as const;

export type Key = keyof typeof STRINGS;

type I18n = { lang: Lang; dir: "ltr" | "rtl"; t: (k: Key) => string; tr: (en: string, ar: string) => string;
  setLang: (l: Lang) => void; theme: Theme; setTheme: (t: Theme) => void };

const Ctx = createContext<I18n>({ lang: "en", dir: "ltr", t: (k) => STRINGS[k][0], tr: (en) => en,
  setLang: () => undefined, theme: "system", setTheme: () => undefined });

function read<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key) as T | null;
    return v && allowed.includes(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode: the choice just isn't remembered */
  }
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => read("modwen.lang", ["en", "ar"] as const, "en"));
  const [theme, setThemeState] = useState<Theme>(() => read("modwen.theme", ["system", "light", "dark"] as const, "system"));
  useEffect(() => {
    const root = document.documentElement;
    root.lang = lang;
    root.dir = lang === "ar" ? "rtl" : "ltr";
  }, [lang]);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
  }, [theme]);
  const setLang = useCallback((l: Lang) => { setLangState(l); write("modwen.lang", l); }, []);
  const setTheme = useCallback((t: Theme) => { setThemeState(t); write("modwen.theme", t); }, []);
  const value = useMemo<I18n>(() => ({
    lang, dir: lang === "ar" ? "rtl" : "ltr", t: (k) => STRINGS[k][lang === "ar" ? 1 : 0],
    tr: (en, ar) => (lang === "ar" ? ar : en), setLang, theme, setTheme,
  }), [lang, theme, setLang, setTheme]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useI18n = () => useContext(Ctx);

/** Dates and times in the interface language. */
export function fmtDate(iso: string | null | undefined, lang: Lang, opts: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" }) {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat(lang === "ar" ? "ar" : undefined, opts).format(new Date(iso));
  } catch {
    return new Date(iso).toLocaleString();
  }
}
