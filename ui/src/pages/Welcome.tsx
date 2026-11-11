import { useEffect, useState } from "react";
import { setup, SetupStatus } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import JobProgress, { useJob } from "../components/JobProgress";
import { useServerEvents } from "../events";
import { useI18n } from "../i18n";
import { liveNamesCurrentlyOn, markLiveNamesNoticeSeen, WELCOME_STEP_ORDER, WelcomeStepId } from "../liveNamesOnboarding";
import { AudioTest } from "./Settings";

export default function Welcome({ ctx }: { ctx: Ctx }) {
  const { t, tr } = useI18n();
  const [step, setStep] = useState(0);
  const [st, setSt] = useState<SetupStatus | null>(null);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const reload = () => setup.status().then(setSt);
  useEffect(() => {
    reload();
    setup.settings().then((r) => setSettings(r.settings)).catch(() => undefined);
  }, []);
  useServerEvents(ctx, (e) => {
    if (e.type === "job" || e.type === "resync") reload();
  });

  const labels: Record<WelcomeStepId, string> = {
    permissions: tr("Permissions", "الأذونات"),
    liveNames: tr("Live speaker names", "التعرّف المباشر على المتحدثين"),
    hardware: tr("This computer", "هذا الجهاز"),
    models: tr("Models", "النماذج"),
    notifications: tr("Notifications", "الإشعارات"),
  };
  const steps = WELCOME_STEP_ORDER.map((id) => labels[id]);
  const lastStep = WELCOME_STEP_ORDER.length - 1;
  if (!st) return <p className="muted"><I.spin className="spin-anim" /> {tr("Checking this computer…", "جارٍ فحص هذا الجهاز…")}</p>;
  const finish = async () => {
    await setup.complete();
    // Whoever finishes (or skips) onboarding has had the chance to see the new "live speaker
    // names" step, so the one-time returning-user notice never needs to show for them either.
    markLiveNamesNoticeSeen();
    ctx.toast(tr("Setup complete", "اكتمل الإعداد"));
    ctx.go("#/");
  };

  return (
    <>
      <div className="page-head"><div>
        <h1>{tr("Welcome to Modwen", "مرحباً بك في مُدوَّن")}</h1>
        <p className="lead">{tr("A few quick checks, then you're ready to record. Everything stays on this computer.",
          "بضع خطوات سريعة ثم تبدأ التسجيل. كل شيء يبقى على هذا الجهاز.")}</p></div></div>
      <ol className="wsteps">
        {steps.map((name, i) => (
          <li key={name}><button className={i < step ? "done" : i === step ? "on" : ""} onClick={() => setStep(i)}
            aria-current={i === step ? "step" : undefined}>
            <span className="n">{i < step ? <I.check size={13} /> : i + 1}</span>{name}</button></li>
        ))}
      </ol>
      <div className="card wcard">
        {WELCOME_STEP_ORDER[step] === "permissions" && <Permissions />}
        {WELCOME_STEP_ORDER[step] === "liveNames" && <LiveNamesStep settings={settings} />}
        {WELCOME_STEP_ORDER[step] === "hardware" && <Hardware st={st} />}
        {WELCOME_STEP_ORDER[step] === "models" && <Models ctx={ctx} st={st} reload={reload} />}
        {WELCOME_STEP_ORDER[step] === "notifications" && <NotifyStep />}
      </div>
      <div className="wnav">
        <button className="btn" disabled={step === 0} onClick={() => setStep(step - 1)}><I.back size={15} />{tr("Back", "رجوع")}</button>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn ghost" onClick={finish}>{tr("Skip setup", "تخطَّ الإعداد")}</button>
          {step < lastStep ? <button className="btn primary" onClick={() => setStep(step + 1)}>{tr("Continue", "متابعة")}</button>
            : <button className="btn primary" onClick={finish}><I.check size={15} />{tr("Finish", "إنهاء")}</button>}
        </div>
      </div>
      <p className="faint" style={{ fontSize: 12, marginTop: 14 }}><I.lock size={13} /> {t("localOnly")}</p>
    </>
  );
}

function Permissions() {
  const { tr } = useI18n();
  return (
    <>
      <h2 className="wtitle"><I.record size={18} />{tr("Let Modwen hear the meeting", "اسمح لمُدوَّن بسماع الاجتماع")}</h2>
      <p>{tr("Modwen records your microphone and the meeting audio (what plays on your speakers). Press the button, then talk and play any video for 3 seconds. Your computer may ask for permission: click Allow.",
        "يسجّل مُدوَّن الميكروفون وصوت الاجتماع (ما يُشغَّل على السماعات). اضغط الزر، ثم تحدّث وشغّل أي فيديو لمدة 3 ثوانٍ. قد يطلب جهازك الإذن: اضغط سماح.")}</p>
      <AudioTest big />
      <div className="banner info" style={{ marginTop: 14, flexWrap: "nowrap", alignItems: "flex-start" }}><I.info style={{ marginTop: 2 }} />
        <span className="grow">{tr("macOS: allow Microphone and System Audio Recording (System Settings → Privacy & Security) for the app you started Modwen from. Windows: Settings → Privacy → Microphone → allow desktop apps.",
          "macOS: اسمح بالميكروفون وتسجيل صوت النظام (إعدادات النظام ← الخصوصية والأمان) للتطبيق الذي شغّلت منه مُدوَّن. Windows: الإعدادات ← الخصوصية ← الميكروفون ← اسمح لتطبيقات سطح المكتب.")}</span></div>
    </>
  );
}

/** New onboarding step (task 14), per reports/13-designer-onboarding.md §3-4. Placed right after
 * Permissions (WELCOME_STEP_ORDER). Disclosure only — no checkbox, Continue/Back/Skip work
 * exactly as on every other step (see that report §3's last bullet). */
function LiveNamesStep({ settings }: { settings: Record<string, unknown> | null }) {
  const { tr } = useI18n();
  const on = liveNamesCurrentlyOn(settings);
  return (
    <>
      <h2 className="wtitle"><I.voice size={18} />{tr("Live speaker names (on by default)", "التعرّف المباشر على المتحدثين (مفعّل تلقائياً)")}</h2>
      <p>{tr(
        "By default, while you record, Modwen compares each speaker's voice against the voices you've already saved in People — with their consent, from earlier meetings. If it recognizes a saved voice, that person's name appears on the live caption while the meeting is still going (sometimes with a question mark, like ⁨“Sara?”⁩, when it isn't sure). Anyone whose voice you haven't saved stays labeled Call or Room, as usual.",
        // Arabic: the CEO (a native Arabic speaker) reviewed and explicitly approved this reconciled
        // wording on 2026-11-07 — see eleven/decisions/0001-arabic-disclosure-wording.md. It merges the
        // decision record's onboarding-on paragraphs 1-2 into this single English paragraph's <p>.
        "أثناء التسجيل، يقارن مُدوَّن صوت كل متحدث ببصمات الأصوات التي سبق حفظها في صفحة الأشخاص بموافقة أصحابها. إذا تعرّف التطبيق على صوت محفوظ، يظهر اسم صاحبه على النص المباشر أثناء الاجتماع. وقد تظهر علامة استفهام بجانب الاسم، مثل «سارة؟»، إذا لم يكن التطبيق متأكداً من المطابقة. أما المتحدثون الذين لا يتطابق صوتهم مع أي بصمة محفوظة، فيستمر ظهورهم باسم «المكالمة» أو «الغرفة»، كالمعتاد.")}</p>
      <p>{tr(
        "Modwen never enrolls a new voice during a meeting and never groups unknown speakers into a profile on its own — only people already saved in People, by their own consent, can ever be named live. The comparison happens in memory and is discarded immediately after each caption; nothing about any voice is written down or sent anywhere.",
        // Arabic: same 2026-11-07 CEO approval as above (decision record 0001) — merges the decision
        // record's onboarding-on paragraphs 3-4 into this single English paragraph's <p>.
        "لا يحفظ مُدوَّن بصمات صوت جديدة أثناء الاجتماع، ولا يجمع المتحدثين غير المعروفين في مجموعات أو ينشئ لهم ملفات صوتية تلقائياً. يقتصر التعرّف المباشر على الأشخاص الذين سبق حفظ بصمات أصواتهم بموافقتهم. تتم المقارنة باستخدام بيانات صوتية مؤقتة في الذاكرة، ويُتخلّص من هذه البيانات بعد إتمام المقارنة. ولا تُحفظ هذه البيانات المؤقتة ولا تُرسل إلى نموذج الذكاء الاصطناعي.")}</p>
      <p>{on
        ? tr("You can turn this off any time in Settings → Transcription, or read the full explanation in Settings → Privacy.",
          // Arabic: CEO-approved 2026-11-07, decision record 0001 (onboarding on, closing sentence).
          "يمكنك إيقاف هذه الميزة في أي وقت من الإعدادات ← تحويل الكلام إلى نص، أو قراءة الشرح الكامل في الإعدادات ← الخصوصية.")
        : tr("This is currently turned off for you. You can turn it back on any time in Settings → Transcription, or read the full explanation in Settings → Privacy.",
          // Arabic: CEO-approved 2026-11-07, decision record 0001 (onboarding off piece).
          "هذا الخيار متوقف حالياً. يمكنك إعادة تفعيله في أي وقت من الإعدادات ← تحويل الكلام إلى نص، أو قراءة الشرح الكامل في الإعدادات ← الخصوصية.")}</p>
      <p><a href="#/settings/privacy">{tr("How voices are handled", "كيف يتعامل مُدوَّن مع الأصوات")}</a></p>
    </>
  );
}

function Hardware({ st }: { st: SetupStatus }) {
  const { tr } = useI18n();
  const p = st.plan;
  return (
    <>
      <h2 className="wtitle"><I.chip size={18} />{tr("What this computer can do", "ما يستطيعه هذا الجهاز")}</h2>
      <div className="tier">
        <div className="big">{p.tier}</div>
        <div><b>{tr("Tier", "الفئة")} {p.tier}: {p.label}</b>
          <div className="muted" style={{ fontSize: 13 }}>{tr(`A 2-hour meeting should be ready about ${p.est_2h_minutes[0]}–${p.est_2h_minutes[1]} min after Stop.`,
            `اجتماع مدته ساعتان يجهز بعد حوالي ${p.est_2h_minutes[0]}–${p.est_2h_minutes[1]} دقيقة من الإيقاف.`)}</div></div>
      </div>
      <dl className="kv">
        <dt>{tr("Live transcript", "النص المباشر")}</dt><dd dir="ltr">Whisper {st.whisper.models.find((m) => m.role === "live")?.name} ({st.whisper.backend})</dd>
        <dt>{tr("Final transcript", "النص النهائي")}</dt><dd dir="ltr">Whisper {st.whisper.models.find((m) => m.role === "final")?.name} ({st.whisper.backend})</dd>
        <dt>{tr("Recommended AI model", "النموذج الموصى به")}</dt><dd dir="ltr">{p.llm_model} · {p.llm_context.toLocaleString()} context</dd>
        <dt>{tr("Memory · free disk", "الذاكرة · المساحة الحرة")}</dt><dd dir="ltr">{st.hardware.ram_gb} GB RAM · {st.hardware.free_disk_gb} GB free</dd>
      </dl>
      {p.warnings.map((w) => <div key={w} className="banner warn" style={{ marginTop: 10 }}><I.warn />{w}</div>)}
    </>
  );
}

function Models({ ctx, st, reload }: { ctx: Ctx; st: SetupStatus; reload: () => void }) {
  const { tr } = useI18n();
  const running = (k: string) => st.jobs.find((j) => j.kind === k && j.status === "running") ?? null;
  const [wJob, setWJob] = useJob(ctx, running("whisper"));
  const [lJob, setLJob] = useJob(ctx, running("llm"));
  const [choice, setChoice] = useState(st.llm.managed.model_choice || st.llm.recommended);
  const [mode, setMode] = useState<"managed" | "external">(st.llm.managed.enabled || !st.llm.online ? "managed" : "external");
  const [url, setUrl] = useState("http://localhost:11434/v1");
  const [model, setModel] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const whisperReady = st.whisper.models.every((m) => m.present);
  const m = st.llm.managed;
  const chosen = st.llm.choices.find((c) => c.id === choice);

  const connect = async () => {
    setMsg(null);
    try {
      const r = await setup.useExternal({ base_url: url, model: model || undefined });
      setMsg({ ok: true, text: tr(`Connected: ${r.base_url} · model ${r.model}`, `تم الاتصال: ${r.base_url} · النموذج ${r.model}`) });
      reload();
      ctx.refreshStatus();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  return (
    <>
      <h2 className="wtitle"><I.download size={18} />{tr("Download the models", "نزّل النماذج")}</h2>
      <p className="muted" style={{ marginTop: 0 }}>{tr("Downloaded once into Modwen's own folder, then everything works offline.", "تُنزَّل مرة واحدة في مجلد مُدوَّن، ثم يعمل كل شيء دون اتصال.")}</p>

      <div className="wblock">
        <div className="wblock-head"><b>1 · {tr("Speech models (Whisper)", "نماذج الكلام (Whisper)")}</b>
          {whisperReady && <span className="badge ok"><I.check size={13} />{tr("Ready", "جاهز")}</span>}</div>
        <div className="model-list">
          {st.whisper.models.map((x) => (
            <div key={x.name + x.role} className="model-row"><b className="mono">{x.name}</b>
              {x.present ? <I.ok size={15} style={{ color: "var(--ok)" }} /> : <span className="faint">{tr("not downloaded", "لم يُنزَّل")}</span>}</div>
          ))}
        </div>
        {!whisperReady && <button className="btn primary" style={{ marginTop: 10 }} disabled={wJob?.status === "running"}
          onClick={async () => setWJob((await setup.downloadWhisper()).job)}><I.download size={15} />{tr("Download speech models", "نزّل نماذج الكلام")}</button>}
        <JobProgress job={wJob} />
      </div>

      <div className="wblock">
        <div className="wblock-head"><b>2 · {tr("AI model for the notes", "نموذج الذكاء الاصطناعي للملاحظات")}</b>
          {st.llm.online && <span className="badge ok"><span className="dot" />{tr("online", "متصل")}</span>}</div>
        <div className="seg" style={{ margin: "4px 0 12px" }}>
          <button className={mode === "managed" ? "on" : ""} onClick={() => setMode("managed")}>{tr("Download Qwen for me (recommended)", "نزّل Qwen لي (موصى به)")}</button>
          <button className={mode === "external" ? "on" : ""} onClick={() => setMode("external")}>{tr("Use my own local AI server", "استخدم خادمي المحلي")}</button>
        </div>
        {mode === "managed" ? (
          <>
            <div className="field"><label>{tr("Model", "النموذج")}</label>
              <select className="select" value={choice} onChange={(e) => setChoice(e.target.value)}>
                {st.llm.choices.map((c) => (
                  <option key={c.id} value={c.id}>{c.id}{c.id === st.llm.recommended ? ` (${tr("recommended for this computer", "موصى به لهذا الجهاز")})` : ""} · {c.size_gb} GB</option>
                ))}
              </select></div>
            <p className="faint" style={{ fontSize: 12.5 }}>{tr(`Modwen downloads llama.cpp and ${chosen?.file} (~${chosen?.size_gb} GB) into its own folder and starts it automatically on 127.0.0.1:${m.port}. Nothing else is installed on your system.`,
              `يُنزّل مُدوَّن llama.cpp و${chosen?.file} (~${chosen?.size_gb} GB) في مجلده ويشغّله تلقائياً على 127.0.0.1:${m.port}. لا يُثبَّت شيء آخر على نظامك.`)}</p>
            {m.binary_installed && m.model_installed && m.model_choice === choice ? (
              <div className="row-wrap">
                <span className={`badge ${m.running ? "ok" : ""}`}>{m.running ? <><span className="dot pulse" />{tr("running", "يعمل")}</> : tr("installed, not running", "مثبّت، لا يعمل")}</span>
                {!m.running && <button className="btn sm" onClick={() => setup.startManaged().then(reload).catch((e) => setMsg({ ok: false, text: e.message }))}>
                  <I.play size={13} fill="currentColor" />{tr("Start", "تشغيل")}</button>}
                {st.llm.online && st.llm.active === "managed_qwen" && <span className="badge ok"><I.check size={13} />{tr("AI model online", "النموذج متصل")}</span>}
              </div>
            ) : (
              <button className="btn primary" disabled={lJob?.status === "running"}
                onClick={async () => setLJob((await setup.installManaged(choice)).job)}><I.download size={15} />{tr(`Download and start ${choice}`, `نزّل وشغّل ${choice}`)}</button>
            )}
            {m.port_busy_external && !m.running && <div className="banner warn" style={{ marginTop: 10 }}><I.warn />
              {tr(`Port ${m.port} is already used by another server. If that's your own AI server, choose "Use my own local AI server".`,
                `المنفذ ${m.port} مستخدم من خادم آخر. إذا كان خادمك، اختر "استخدم خادمي المحلي".`)}</div>}
            <JobProgress job={lJob} />
          </>
        ) : (
          <>
            <p className="faint" style={{ fontSize: 12.5 }}>{tr("Any OpenAI-compatible server on this computer: Ollama (http://localhost:11434/v1), LM Studio (http://localhost:1234/v1), llama.cpp (http://localhost:8080/v1), vLLM… Only localhost addresses are allowed.",
              "أي خادم متوافق مع OpenAI على هذا الجهاز: Ollama (http://localhost:11434/v1)، LM Studio (http://localhost:1234/v1)، llama.cpp (http://localhost:8080/v1)، vLLM… العناوين المحلية فقط مسموحة.")}</p>
            <div className="field"><label>{tr("Server URL", "عنوان الخادم")}</label><input className="input" dir="ltr" value={url} onChange={(e) => setUrl(e.target.value)} /></div>
            <div className="field"><label>{tr("Model name", "اسم النموذج")}</label><input className="input" dir="ltr" placeholder={tr("leave empty to use the server's first model", "اتركه فارغاً لاستخدام أول نموذج")}
              value={model} onChange={(e) => setModel(e.target.value)} /></div>
            <button className="btn primary" onClick={connect}><I.network size={15} />{tr("Test and use", "اختبر واستخدم")}</button>
          </>
        )}
        {msg && <div className={`banner ${msg.ok ? "info" : "err"}`} style={{ marginTop: 10 }}>{msg.ok ? <I.ok /> : <I.fail />}<span dir="auto">{msg.text}</span></div>}
        <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
          {tr("Active", "النشط")}: <b dir="ltr">{st.llm.active}</b> · {st.llm.online ? <span style={{ color: "var(--ok)" }}>{tr("online", "متصل")}</span> : tr("offline", "غير متصل")}</div>
      </div>
    </>
  );
}

function NotifyStep() {
  const { tr } = useI18n();
  const [sent, setSent] = useState(false);
  return (
    <>
      <h2 className="wtitle"><I.bell size={18} />{tr("Know when notes are ready", "اعرف متى تجهز الملاحظات")}</h2>
      <p>{tr("When a meeting is processed, Modwen shows a “Notes ready” notification. Send a test to make sure it reaches you (turn off Focus / Do Not Disturb if it doesn't appear).",
        "عند انتهاء معالجة الاجتماع يُظهر مُدوَّن إشعار “الملاحظات جاهزة”. أرسل اختباراً لتتأكد من وصوله (أوقف وضع التركيز / عدم الإزعاج إذا لم يظهر).")}</p>
      <button className="btn primary" onClick={() => setup.notifyTest().then(() => setSent(true))}><I.bell size={15} />{tr("Send test notification", "أرسل إشعاراً تجريبياً")}</button>
      {sent && <p className="muted mi" style={{ marginTop: 10 }}><I.check size={14} />{tr("Sent. Did you see “Notes ready: Test meeting”?", "أُرسل. هل رأيت “الملاحظات جاهزة: اجتماع تجريبي”؟")}</p>}
    </>
  );
}
