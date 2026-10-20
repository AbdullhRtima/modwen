import { Fragment, useEffect, useState } from "react";
import { api, setup, SetupStatus } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import JobProgress, { useJob } from "../components/JobProgress";
import { Key, useI18n } from "../i18n";

type Tab = "general" | "audio" | "transcription" | "ai" | "notifications" | "privacy" | "about";
const TABS: [Tab, string, string, (p: { size?: number }) => JSX.Element][] = [
  ["general", "General", "عام", I.settings], ["audio", "Audio", "الصوت", I.record],
  ["transcription", "Transcription", "التفريغ", I.live], ["ai", "AI model", "نموذج الذكاء الاصطناعي", I.chip],
  ["notifications", "Notifications", "الإشعارات", I.bell], ["privacy", "Privacy", "الخصوصية", I.privacy],
  ["about", "About", "حول", I.info]];

type Profiles = Awaited<ReturnType<typeof api.profiles>>;
type TestResult = Awaited<ReturnType<typeof api.testLlm>>;

export default function Settings({ ctx }: { ctx: Ctx }) {
  const { t, lang } = useI18n();
  const initial = (location.hash.split("/")[2] as Tab) || "general";
  const [tab, setTab] = useState<Tab>(TABS.some(([x]) => x === initial) ? initial : "general");
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [st, setSt] = useState<SetupStatus | null>(null);
  const reload = () => {
    setup.settings().then((r) => setSettings(r.settings));
    setup.status().then(setSt);
  };
  useEffect(reload, []);
  useEffect(() => {
    if (ctx.lastEvent?.type === "job") setup.status().then(setSt);
  }, [ctx.lastEvent]);

  const save = async (patch: Record<string, unknown>) => {
    try {
      const r = await setup.saveSettings(patch);
      setSettings(r.settings);
      ctx.toast(t("saved"));
    } catch (e) {
      ctx.fail(e);
    }
  };
  const go = (t: Tab) => {
    setTab(t);
    history.replaceState(null, "", `#/settings/${t}`);
  };

  return (
    <>
      <div className="page-head"><h1>{t("settings")}</h1>
        <span className="faint" style={{ fontSize: 12 }} dir="ltr">{st?.config_path ?? "config.yaml"}</span></div>
      <div className="settings-wrap">
      <nav className="tabs" aria-label={t("settings")}>
        {TABS.map(([x, en, ar, Icon]) => <button key={x} className={tab === x ? "on" : ""} onClick={() => go(x)}>
          <Icon size={16} />{lang === "ar" ? ar : en}</button>)}
      </nav>
      <div style={{ minWidth: 0 }}>
      {!(settings && st) && <p className="muted">{t("loading")}</p>}
      {settings && st && (
        <>
          {tab === "general" && <General settings={settings} save={save} ctx={ctx} />}
          {tab === "audio" && <Audio settings={settings} save={save} st={st} />}
          {tab === "transcription" && <Transcription settings={settings} save={save} st={st} ctx={ctx} />}
          {tab === "ai" && <AIModel ctx={ctx} st={st} reload={reload} />}
          {tab === "notifications" && <Notifications settings={settings} save={save} />}
          {tab === "privacy" && <Privacy settings={settings} save={save} />}
          {tab === "about" && <About st={st} ctx={ctx} />}
        </>
      )}
      </div>
      </div>
    </>
  );
}

function Appearance() {
  const { t, lang, setLang, theme, setTheme } = useI18n();
  const themes: [string, Key, (p: { size?: number }) => JSX.Element][] = [["system", "system", I.system], ["light", "light", I.sun], ["dark", "dark", I.moon]];
  return (
    <>
      <div className="field"><label>{t("interfaceLang")}</label>
        <div className="seg"><button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>English</button>
          <button className={lang === "ar" ? "on" : ""} onClick={() => setLang("ar")}>العربية</button></div></div>
      <div className="field"><label>{t("theme")}</label>
        <div className="seg">{themes.map(([v, k, Icon]) => <button key={v} className={theme === v ? "on" : ""}
          onClick={() => setTheme(v as "system" | "light" | "dark")}><Icon size={14} />{t(k)}</button>)}</div></div>
    </>
  );
}

type SP = { settings: Record<string, unknown>; save: (p: Record<string, unknown>) => void };

function Select({ value, options, onChange }: { value: string; options: [string, string][]; onChange: (v: string) => void }) {
  return <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
    {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>;
}

/** A settings card with a heading, so every tab reads the same way. */
function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="card set-card">
      <div className="set-head"><h2>{title}</h2>{hint && <p className="faint">{hint}</p>}</div>
      {children}
    </section>
  );
}

function General({ settings, save, ctx }: SP & { ctx: Ctx }) {
  const { tr } = useI18n();
  const tzs = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  const sysTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <div className="set-stack">
      <Section title={tr("Appearance", "المظهر")}><Appearance /></Section>
      <Section title={tr("Notes", "الملاحظات")}>
        <div className="field"><label>{tr("Summary language", "لغة الملخص")}</label>
          <Select value={String(settings["notes.summary_language"])} onChange={(v) => save({ "notes.summary_language": v })}
            options={[["en", tr("English (default)", "الإنجليزية (افتراضي)")], ["ar", tr("Arabic", "العربية")], ["both", tr("English + Arabic", "الإنجليزية + العربية")]]} /></div>
        <div className="field"><label>{tr("Long meetings", "الاجتماعات الطويلة")}</label>
          <Select value={String(settings["notes.chunk_minutes"])} onChange={(v) => save({ "notes.chunk_minutes": Number(v) })}
            options={["10", "15", "20", "30"].map((n) => [n, tr(`Split every ${n} min`, `تقسيم كل ${n} دقيقة`)] as [string, string])} /></div>
        <div className="field"><label>{tr("Timezone (deadlines)", "المنطقة الزمنية (المواعيد)")}</label>
          <Select value={String(settings["notes.timezone"])} onChange={(v) => save({ "notes.timezone": v })}
            options={[["system", `${tr("System", "النظام")} (${sysTz})`], ...tzs.map((t) => [t, t] as [string, string])]} /></div>
      </Section>
      <Network ctx={ctx} save={save} />
      <Section title={tr("Setup", "الإعداد")} hint={tr("Check permissions, speech models and the AI model again.", "تحقّق مجدداً من الأذونات ونماذج الكلام ونموذج الذكاء الاصطناعي.")}>
        <div><button className="btn" onClick={() => ctx.go("#/welcome")}><I.regenerate size={15} />{tr("Run the setup wizard again", "شغّل معالج الإعداد مجدداً")}</button></div>
      </Section>
    </div>
  );
}

function Network({ ctx, save }: { ctx: Ctx; save: (p: Record<string, unknown>) => void }) {
  const { tr } = useI18n();
  const [net, setNet] = useState<Awaited<ReturnType<typeof setup.network>> | null>(null);
  const load = () => setup.network().then(setNet).catch(() => undefined);
  useEffect(() => {
    load();
  }, []);
  if (!net) return null;
  return (
    <Section title={tr("Other devices", "الأجهزة الأخرى")}
      hint={tr("Anyone with the link below can use Modwen. The connection is plain HTTP, so use trusted networks only (home / office). Recording still uses this computer's mic.",
        "أي شخص لديه الرابط أدناه يمكنه استخدام مُدوَّن. الاتصال HTTP عادي، لذا استخدم الشبكات الموثوقة فقط (المنزل / المكتب). التسجيل يبقى من ميكروفون هذا الجهاز.")}>
      <label className="check"><input type="checkbox" checked={net.lan_access_setting}
        onChange={async (e) => { await save({ "app.lan_access": e.target.checked }); load(); }} />
        <I.network size={15} />{tr("Allow access from other devices on this network", "اسمح بالوصول من الأجهزة الأخرى على هذه الشبكة")}</label>
      {net.restart_needed && <div className="banner warn" style={{ marginTop: 10 }}><I.warn />{net.lan_access_setting
        ? tr("Restart Modwen to turn on network access.", "أعد تشغيل مُدوَّن لتفعيل الوصول عبر الشبكة.")
        : tr("Restart Modwen to turn off network access.", "أعد تشغيل مُدوَّن لإيقاف الوصول عبر الشبكة.")}</div>}
      {net.lan_active && net.urls.map((u) => (
        <div key={u} className="url-row">
          <code dir="ltr">{u.split("?")[0]}</code>
          <button className="btn sm" onClick={() => navigator.clipboard.writeText(u).then(() => ctx.toast(tr("Link copied", "تم نسخ الرابط")))}>
            <I.copy size={14} />{tr("Copy link", "انسخ الرابط")}</button>
        </div>
      ))}
    </Section>
  );
}

/** A 3-second level test, shared by Settings → Audio and the setup wizard. */
export function AudioTest({ big = false }: { big?: boolean }) {
  const { tr } = useI18n();
  const [res, setRes] = useState<Awaited<ReturnType<typeof setup.audioTest>> | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      setRes(await setup.audioTest());
    } finally {
      setBusy(false);
    }
  };
  const lv = (k: string) => Math.round((res?.levels[k] ?? 0) * 100);
  return (
    <div className="audio-test">
      <button className={`btn ${big ? "primary" : ""}`} disabled={busy} onClick={run}>
        {busy ? <I.spin size={15} className="spin-anim" /> : <I.record size={15} />}
        {busy ? tr("Listening for 3 seconds…", "يستمع لمدة 3 ثوانٍ…") : tr("Test audio (3 seconds)", "اختبر الصوت (3 ثوانٍ)")}</button>
      {res && (
        <div className="levels">
          {["mic", "system"].map((k) => (
            <div key={k} className="level">
              <span className="lname">{k === "mic" ? tr("Microphone (you)", "الميكروفون (أنت)") : tr("Meeting audio (others)", "صوت الاجتماع (الآخرون)")}</span>
              {res.errors[k] ? <span className="lstate bad"><I.fail size={14} />{res.errors[k]}</span> : <>
                <span className="progress"><i style={{ width: `${lv(k)}%` }} /></span>
                {lv(k) > 15 ? <span className="lstate ok"><I.ok size={14} />{tr("Hearing sound", "يُسمع الصوت")}</span>
                  : <span className="lstate warn"><I.warn size={14} />{tr("No sound: talk or play audio and test again", "لا صوت: تحدّث أو شغّل صوتاً واختبر مجدداً")}</span>}
              </>}
              {res.sources[k] ? <span className="faint lsrc" dir="auto">{res.sources[k]}</span> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Audio({ settings, save, st }: SP & { st: SetupStatus }) {
  const { tr } = useI18n();
  const [devices, setDevices] = useState<{ index: number; name: string; default: boolean }[]>([]);
  useEffect(() => {
    api.devices().then((d) => setDevices(d.inputs));
  }, []);
  const auto = tr("Automatic", "تلقائي");
  return (
    <div className="set-stack">
      <Section title={tr("Sources", "المصادر")}>
        <div className="field"><label>{tr("Microphone", "الميكروفون")}</label>
          <Select value={settings["capture.mic_device"] == null ? "" : String(settings["capture.mic_device"])}
            onChange={(v) => save({ "capture.mic_device": v || null })}
            options={[["", tr("System default", "افتراضي النظام")], ...devices.map((d) => [d.name, d.name + (d.default ? ` (${tr("default", "افتراضي")})` : "")] as [string, string])]} /></div>
        <div className="field"><label>{tr("Meeting audio", "صوت الاجتماع")}</label>
          <Select value={String(settings["capture.system_backend"])} onChange={(v) => save({ "capture.system_backend": v })}
            options={[["auto", st.hardware.os === "Windows" ? `${auto} (WASAPI loopback)` : st.hardware.os === "macOS" ? `${auto} (Core Audio tap)` : auto],
              ["device", tr("A loopback input device (e.g. BlackHole)", "جهاز إدخال loopback (مثل BlackHole)")], ["none", tr("Off (mic only)", "إيقاف (الميكروفون فقط)")]]} /></div>
        {settings["capture.system_backend"] === "device" && (
          <div className="field"><label>{tr("Loopback device", "جهاز loopback")}</label>
            <Select value={String(settings["capture.system_device"] ?? "")} onChange={(v) => save({ "capture.system_device": v || null })}
              options={[["", tr("Choose…", "اختر…")], ...devices.map((d) => [d.name, d.name] as [string, string])]} /></div>
        )}
      </Section>
      <Section title={tr("Test", "اختبار")} hint={tr("Talk and play any video while it listens.", "تحدّث وشغّل أي فيديو أثناء الاستماع.")}><AudioTest /></Section>
    </div>
  );
}

function Transcription({ settings, save, st, ctx }: SP & { st: SetupStatus; ctx: Ctx }) {
  const { tr } = useI18n();
  const [glossary, setGlossary] = useState(((settings["stt.glossary"] as string[]) ?? []).join(", "));
  const [job, setJob] = useJob(ctx, st.jobs.find((j) => j.kind === "whisper" && j.status === "running") ?? null);
  const models = ["auto", "large-v3", "large-v3-turbo", "medium", "small", "base"];
  const auto = (m: string) => `${tr("Automatic", "تلقائي")} (${m})`;
  return (
    <div className="set-stack">
      <Section title={tr("Speech to text", "تحويل الكلام إلى نص")}>
        <div className="field"><label>{tr("Meeting language", "لغة الاجتماع")}</label>
          <Select value={String(settings["stt.meeting_language"])} onChange={(v) => save({ "stt.meeting_language": v })}
            options={[["auto", tr("Auto (Arabic + English)", "تلقائي (العربية + الإنجليزية)")], ["en", tr("English only", "الإنجليزية فقط")], ["ar", tr("Arabic only", "العربية فقط")]]} /></div>
        <div className="field"><label>{tr("Final transcript", "النص النهائي")}</label>
          <Select value={String(settings["stt.final.model"])} onChange={(v) => save({ "stt.final.model": v })}
            options={models.map((m) => [m, m === "auto" ? auto(st.plan.final_model) : m] as [string, string])} /></div>
        <div className="field"><label>{tr("Live captions", "الترجمة المباشرة")}</label>
          <div className="row-wrap">
            <Select value={String(settings["stt.live.provider"])} onChange={(v) => save({ "stt.live.provider": v })}
              options={[["local_whisper", tr("On", "تشغيل")], ["none", tr("Off", "إيقاف")]]} />
            <Select value={String(settings["stt.live.model"])} onChange={(v) => save({ "stt.live.model": v })}
              options={models.map((m) => [m, m === "auto" ? auto(st.plan.live_model) : m] as [string, string])} />
          </div></div>
        <div className="field" style={{ alignItems: "start" }}><label>{tr("Glossary", "المسرد")}</label>
          <div><textarea className="input" rows={2} value={glossary} onChange={(e) => setGlossary(e.target.value)} dir="auto"
            placeholder={tr("Names and terms you use: Lina, لينا, MVP, roadmap", "أسماء ومصطلحات تستخدمها: لينا، Lina، MVP، خطة")}
            onBlur={() => save({ "stt.glossary": glossary.split(/[,،]/).map((x) => x.trim()).filter(Boolean) })} />
            <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>{tr("Helps Whisper spell names and product terms. Keep it short.", "يساعد Whisper على كتابة الأسماء والمصطلحات. اجعله قصيراً.")}</div></div></div>
      </Section>
      <Section title={tr("Models on disk", "النماذج على الجهاز")} hint={tr("Downloaded once, then everything works offline.", "تُنزَّل مرة واحدة، ثم يعمل كل شيء دون اتصال.")}>
        <div className="model-list">
          {st.whisper.models.map((m) => (
            <div key={m.name + m.role} className="model-row">
              <span><b className="mono">{m.name}</b> <span className="faint">· {m.role === "live" ? tr("live", "مباشر") : m.role === "final" ? tr("final", "نهائي") : m.role}</span></span>
              {m.present ? <span className="badge ok"><I.check size={13} />{tr("ready", "جاهز")}</span> : <span className="badge warn"><I.warn size={13} />{tr("missing", "غير موجود")}</span>}
            </div>
          ))}
        </div>
        {st.whisper.models.some((m) => !m.present) && <button className="btn primary sm" style={{ marginTop: 10 }}
          onClick={async () => setJob((await setup.downloadWhisper()).job)}><I.download size={14} />{tr("Download", "تنزيل")}</button>}
        <JobProgress job={job} />
      </Section>
    </div>
  );
}

function AIModel({ ctx, st, reload }: { ctx: Ctx; st: SetupStatus; reload: () => void }) {
  const { t, tr } = useI18n();
  const [profiles, setProfiles] = useState<Profiles | null>(null);
  const [tests, setTests] = useState<Record<string, TestResult | "running">>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [lJob, setLJob] = useJob(ctx, st.jobs.find((j) => j.kind === "llm" && j.status === "running") ?? null);
  const [choice, setChoice] = useState(st.llm.managed.model_choice || st.llm.recommended);
  const load = () => api.profiles().then(setProfiles);
  useEffect(() => {
    load();
  }, []);
  const m = st.llm.managed;

  const runTest = async (name: string) => {
    setTests((t) => ({ ...t, [name]: "running" }));
    try {
      const r = await api.testLlm(name);
      setTests((t) => ({ ...t, [name]: r }));
    } catch (e) {
      setTests((t) => ({ ...t, [name]: { ok: false, error: (e as Error).message, base_url: "" } }));
    }
  };
  const activate = async (name: string) => {
    await api.setActive(name);
    load();
    reload();
    ctx.refreshStatus();
    ctx.toast(tr(`Active AI model: ${name}`, `نموذج الذكاء الاصطناعي النشط: ${name}`));
  };
  const startManaged = () => setup.startManaged().then(() => { reload(); load(); }).catch(ctx.fail);
  if (!profiles) return <p className="muted">{t("loading")}</p>;
  const active = profiles.profiles[profiles.active];

  return (
    <div className="set-stack">
      <Section title={tr("Active model", "النموذج النشط")}>
        <div className="row-wrap">
          <select className="select" style={{ maxWidth: 320 }} value={profiles.active} onChange={(e) => activate(e.target.value)}>
            {Object.keys(profiles.profiles).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className={`badge ${st.llm.online ? "ok" : ""}`}><span className="dot" />
            {st.llm.online ? <span dir="ltr">{tr("online", "متصل")} · {active?.model} · {Math.round((active?.context_window ?? 0) / 1024)}k</span> : tr("offline", "غير متصل")}</span>
        </div>
      </Section>

      <Section title={tr("Modwen-managed AI model", "نموذج يديره مُدوَّن")}
        hint={tr("Modwen downloads and runs llama.cpp + Qwen for you, on this computer only.", "يُنزّل مُدوَّن llama.cpp + Qwen ويشغّلهما لك، على هذا الجهاز فقط.")}>
        <div className="row-wrap">
          <select className="select" style={{ maxWidth: 360 }} value={choice} onChange={(e) => setChoice(e.target.value)}>
            {st.llm.choices.map((c) => <option key={c.id} value={c.id}>{c.id}{c.id === st.llm.recommended ? ` (${tr("recommended", "موصى به")})` : ""} · {c.size_gb} GB</option>)}
          </select>
          {m.binary_installed && m.model_installed && m.model_choice === choice ? (
            <>
              <span className={`badge ${m.running ? "ok" : ""}`}>{m.running ? <><span className="dot pulse" />{tr("running", "يعمل")}</> : tr("installed", "مثبّت")}</span>
              {m.running ? <button className="btn sm" onClick={() => setup.stopManaged().then(reload)}><I.stop size={12} fill="currentColor" />{tr("Stop", "إيقاف")}</button>
                : <button className="btn sm" onClick={startManaged}><I.play size={13} fill="currentColor" />{tr("Start", "تشغيل")}</button>}
              {profiles.active !== "managed_qwen" && profiles.profiles["managed_qwen"] && <button className="btn sm" onClick={() => activate("managed_qwen")}>{tr("Use", "استخدم")}</button>}
            </>
          ) : (
            <button className="btn primary sm" disabled={lJob?.status === "running"}
              onClick={async () => setLJob((await setup.installManaged(choice)).job)}><I.download size={14} />{tr("Download and start", "تنزيل وتشغيل")}</button>
          )}
        </div>
        {m.port_busy_external && !m.running && <div className="banner warn" style={{ marginTop: 10 }}><I.warn />{tr(`Port ${m.port} is used by another server.`, `المنفذ ${m.port} مستخدم من خادم آخر.`)}</div>}
        <JobProgress job={lJob} />
      </Section>

      <Section title={tr("Profiles", "الملفات الشخصية")} hint={tr("Changes are written to config.yaml. Non-localhost URLs are rejected: Modwen is local-only.",
        "تُكتب التغييرات في config.yaml. تُرفض العناوين غير المحلية: مُدوَّن محلي فقط.")}>
        <div style={{ overflowX: "auto" }}>
          <table className="ptable">
            <thead><tr><th>{tr("Name", "الاسم")}</th><th>{tr("Endpoint", "العنوان")}</th><th>{tr("Model", "النموذج")}</th><th>{tr("Context", "السياق")}</th><th /></tr></thead>
            <tbody>
              {Object.entries(profiles.profiles).map(([name, p]) => (
                <Fragment key={name}>
                  <tr>
                    <td><b>{name}</b> {profiles.active === name ? <span className="badge acc">{tr("active", "نشط")}</span>
                      : profiles.fallback.includes(name) ? <span className="badge">{tr("fallback", "احتياطي")}</span> : null}</td>
                    <td><code dir="ltr">{p.base_url}</code></td><td dir="ltr">{p.model}</td><td dir="ltr">{p.context_window.toLocaleString()}</td>
                    <td><div className="tbl-acts">
                      <button className="btn sm ghost" title={t("edit")} aria-label={t("edit")} onClick={() => setEditing(editing === name ? null : name)}><I.edit size={14} /></button>
                      <button className="btn sm" disabled={tests[name] === "running"} onClick={() => runTest(name)}>
                        {tests[name] === "running" ? <I.spin size={14} className="spin-anim" /> : null}{tests[name] === "running" ? tr("Testing…", "جارٍ الاختبار…") : tr("Test", "اختبار")}</button>
                      {profiles.active !== name && <button className="btn sm" onClick={() => activate(name)}>{tr("Use", "استخدم")}</button>}
                      {profiles.active !== name && <button className="btn sm ghost" title={tr("Delete profile", "احذف الملف")} aria-label={tr("Delete profile", "احذف الملف")}
                        onClick={() => setup.deleteProfile(name).then(load)}><I.trash size={14} /></button>}
                    </div></td>
                  </tr>
                  {editing === name && <tr><td colSpan={5}><ProfileForm name={name} p={p} onDone={() => { setEditing(null); load(); reload(); }} /></td></tr>}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        {editing === "__new"
          ? <div className="card" style={{ marginTop: 10 }}><ProfileForm name="" onDone={() => { setEditing(null); load(); }} /></div>
          : <button className="btn ghost sm" style={{ marginTop: 8 }} onClick={() => setEditing("__new")}><I.plus size={14} />{tr("Add local profile", "أضف ملفاً محلياً")}
            <span className="faint">(localhost / 127.0.0.1)</span></button>}
        {Object.entries(tests).map(([name, r]) => r !== "running" && (
          <div key={name} className={`banner ${r.ok ? "info" : "err"}`} style={{ marginTop: 10, display: "block" }}>
            <span className="mi">{r.ok ? <I.ok /> : <I.fail />}<b>{name}:</b> {r.ok ? tr(`replied in ${r.seconds}s`, `ردّ خلال ${r.seconds} ث`) : r.error}</span>
            {r.ok && <div dir="auto" style={{ whiteSpace: "pre-wrap", marginTop: 6, color: "var(--text)" }}>{r.reply}</div>}
          </div>
        ))}
      </Section>
    </div>
  );
}

function ProfileForm({ name, p, onDone }: { name: string; p?: Profiles["profiles"][string]; onDone: () => void }) {
  const { t, tr } = useI18n();
  const [f, setF] = useState({ name, base_url: p?.base_url ?? "http://localhost:11434/v1", model: p?.model ?? "",
    context_window: p?.context_window ?? 16384, temperature: p?.temperature ?? 0.3,
    structured_output: p?.structured_output ?? "json_schema", thinking: false, api_key: "" });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: string, v: unknown) => setF({ ...f, [k]: v });
  const submit = async () => {
    setErr(null);
    const key = (f.name || "").trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_");
    if (!key || !f.model) return setErr(tr("Name and model are required", "الاسم والنموذج مطلوبان"));
    try {
      await setup.saveProfile(key, { base_url: f.base_url, model: f.model, api_key: f.api_key || undefined,
        context_window: Number(f.context_window), temperature: Number(f.temperature),
        structured_output: f.structured_output, thinking: f.thinking });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="grid2">
      <div>
        {!name && <div className="field"><label>{tr("Name", "الاسم")}</label><input className="input" dir="ltr" value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="lm_studio" /></div>}
        <div className="field"><label>{tr("Endpoint", "العنوان")}</label><input className="input" dir="ltr" value={f.base_url} onChange={(e) => set("base_url", e.target.value)} /></div>
        <div className="field"><label>{tr("Model", "النموذج")}</label><input className="input" dir="ltr" value={f.model} onChange={(e) => set("model", e.target.value)} placeholder="qwen3:8b" /></div>
        <div className="field"><label>{tr("API key", "مفتاح API")}</label><input className="input" dir="ltr" value={f.api_key} onChange={(e) => set("api_key", e.target.value)}
          placeholder={tr("(unchanged / not needed)", "(بدون تغيير / غير مطلوب)")} /></div>
      </div>
      <div>
        <div className="field"><label>{tr("Context", "السياق")}</label><input className="input" type="number" value={f.context_window} onChange={(e) => set("context_window", e.target.value)} /></div>
        <div className="field"><label>{tr("Temperature", "درجة الحرارة")}</label><input className="input" type="number" step="0.1" value={f.temperature} onChange={(e) => set("temperature", e.target.value)} /></div>
        <div className="field"><label>{tr("Structured output", "المخرجات المنظّمة")}</label>
          <Select value={f.structured_output} onChange={(v) => set("structured_output", v)}
            options={[["json_schema", "json_schema"], ["json_object", "json_object"], ["prompt_only", "prompt_only"]]} /></div>
        <div className="field"><label>{tr("Thinking", "التفكير")}</label>
          <Select value={f.thinking ? "on" : "off"} onChange={(v) => set("thinking", v === "on")}
            options={[["off", tr("Off (faster, recommended)", "إيقاف (أسرع، موصى به)")], ["on", tr("On", "تشغيل")]]} /></div>
      </div>
      <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn primary" onClick={submit}>{t("save")}</button><button className="btn" onClick={onDone}>{t("cancel")}</button>
        {err && <span className="banner err" style={{ margin: 0 }}><I.fail />{err}</span>}
      </div>
    </div>
  );
}

function Notifications({ settings, save }: SP) {
  const { tr } = useI18n();
  const [sent, setSent] = useState(false);
  return (
    <Section title={tr("Notifications", "الإشعارات")} hint={tr("Turn off Focus / Do Not Disturb if the test doesn't appear.", "أوقف وضع التركيز / عدم الإزعاج إذا لم يظهر الاختبار.")}>
      <div className="check-list">
        <label className="check"><input type="checkbox" checked={Boolean(settings["notify.desktop"])}
          onChange={(e) => save({ "notify.desktop": e.target.checked })} />{tr("“Notes ready” notification when the AI model finishes", "إشعار “الملاحظات جاهزة” عند انتهاء النموذج")}</label>
        <label className="check"><input type="checkbox" checked={Boolean(settings["notify.sound"])}
          onChange={(e) => save({ "notify.sound": e.target.checked })} />{tr("Play a sound", "شغّل صوتاً")}</label>
      </div>
      <div className="row-wrap" style={{ marginTop: 14 }}>
        <button className="btn" onClick={() => setup.notifyTest().then(() => setSent(true))}><I.bell size={15} />{tr("Send test notification", "أرسل إشعاراً تجريبياً")}</button>
        {sent && <span className="muted mi"><I.check size={14} />{tr("Sent", "أُرسل")}</span>}
      </div>
    </Section>
  );
}

function Privacy({ settings, save }: SP) {
  const { tr } = useI18n();
  const days = (k: string) => String(settings[`privacy.retention.${k}_days`] || "∞");
  const points: [JSX.Element, string][] = [
    [<I.lock size={16} />, tr("Meeting audio and transcripts never leave this computer. Non-localhost AI endpoints are refused in code.",
      "صوت الاجتماعات ونصوصها لا تغادر هذا الجهاز أبداً. العناوين غير المحلية مرفوضة في الكود.")],
    [<I.privacy size={16} />, tr("Audio is stored encrypted (AES-256-GCM, a separate key per meeting). Deleting a meeting destroys its key.",
      "يُخزَّن الصوت مشفّراً (AES-256-GCM، مفتاح مستقل لكل اجتماع). حذف الاجتماع يُتلف مفتاحه.")],
    [<I.download size={16} />, tr("Downloads happen only when you press a Download button (speech models, llama.cpp, Qwen). No telemetry.",
      "لا تحدث التنزيلات إلا عند ضغطك زر التنزيل (نماذج الكلام، llama.cpp، Qwen). لا قياس عن بُعد.")],
    [<I.voice size={16} />, tr("Voiceprints are kept only for people you chose to remember (with their consent), encrypted, until you delete them.",
      "تُحفظ بصمات الصوت فقط لمن اخترت تذكّرهم (بموافقتهم)، مشفّرة، حتى تحذفها.")],
    [<I.trash size={16} />, tr(`Old data is deleted automatically: audio after ${days("audio")} days, transcripts after ${days("transcript")}, notes after ${days("notes")} (0 = keep forever). Pinned meetings are always kept.`,
      `تُحذف البيانات القديمة تلقائياً: الصوت بعد ${days("audio")} يوماً، النصوص بعد ${days("transcript")}، الملاحظات بعد ${days("notes")} (0 = الاحتفاظ دائماً). الاجتماعات المثبّتة تبقى دائماً.`)],
  ];
  return (
    <div className="set-stack">
      <Section title={tr("Your voice", "صوتك")}>
        <label className="check"><input type="checkbox" checked={Boolean(settings["diarization.remember_my_voice"])}
          onChange={(e) => save({ "diarization.remember_my_voice": e.target.checked })} />{tr("Remember my voice", "تذكّر صوتي")}</label>
        <p className="faint" style={{ fontSize: 12.5, margin: "6px 0 0", paddingInlineStart: 24 }}>{tr(
          "Keeps a voiceprint of you, taken from your microphone in online meetings, so Modwen can tell which voice is you in in-person meetings. Delete it any time on the People page.",
          "يحتفظ ببصمة صوتك من الميكروفون في الاجتماعات عبر الإنترنت، ليعرف مُدوَّن صوتك في الاجتماعات الحضورية. احذفها متى شئت من صفحة الأشخاص.")}</p>
      </Section>
      <Section title={tr("Local only, always", "محلي فقط، دائماً")}>
        <ul className="promise">{points.map(([icon, text], i) => <li key={i}><span className="pi">{icon}</span><span>{text}</span></li>)}</ul>
      </Section>
    </div>
  );
}

function About({ st, ctx }: { st: SetupStatus; ctx: Ctx }) {
  const { tr } = useI18n();
  return (
    <div className="set-stack">
      <Section title={`${tr("Tier", "الفئة")} ${st.plan.tier}: ${st.plan.label}`}>
        <dl className="kv">
          <dt>{tr("Speech-to-text", "تحويل الكلام")}</dt><dd dir="ltr">{st.whisper.backend} · final {st.plan.final_model} · live {st.plan.live_model}</dd>
          <dt>{tr("Recommended AI model", "النموذج الموصى به")}</dt><dd dir="ltr">{st.plan.llm_model} ({st.plan.llm_context} context)</dd>
          <dt>{tr("2-hour meeting", "اجتماع ساعتين")}</dt><dd>{tr(`about ${st.plan.est_2h_minutes[0]}–${st.plan.est_2h_minutes[1]} min after Stop`,
            `حوالي ${st.plan.est_2h_minutes[0]}–${st.plan.est_2h_minutes[1]} دقيقة بعد الإيقاف`)}</dd>
        </dl>
      </Section>
      <Section title={tr("Run your own llama.cpp instead", "شغّل llama.cpp بنفسك")}>
        <div className="codeblock">{st.plan.llama_server_command}</div>
        <button className="btn sm" style={{ marginTop: 8 }} onClick={() =>
          navigator.clipboard.writeText(st.plan.llama_server_command).then(() => ctx.toast(tr("Command copied", "تم نسخ الأمر")))}>
          <I.copy size={14} />{tr("Copy command", "انسخ الأمر")}</button>
      </Section>
      <Section title={tr("This computer", "هذا الجهاز")}>
        <dl className="kv">
          {Object.entries(st.hardware).map(([k, v]) => <Fragment key={k}><dt className="mono">{k}</dt><dd dir="ltr">{String(v)}</dd></Fragment>)}
        </dl>
      </Section>
    </div>
  );
}
