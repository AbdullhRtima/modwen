import { useEffect, useState } from "react";
import { PersonT, setup } from "../api";
import type { Ctx } from "../App";
import { I } from "../components/icons";
import Modal from "../components/Modal";
import { useI18n } from "../i18n";

// People Modwen can name in meetings. Names here are suggested in the Speakers drawer, and people whose voice
// was saved (with their consent) are recognised automatically in later meetings (design §5.1).

const COLORS = ["var(--sp2)", "var(--sp3)", "var(--sp4)", "var(--sp5)", "var(--sp6)", "var(--sp7)", "var(--sp8)"];
const colorFor = (p: PersonT) => {
  if (p.is_self) return "var(--sp1)";
  let h = 5381;  // djb2 over the name: stable per person, spread across the palette
  for (const c of p.display_name) h = ((h << 5) + h + c.charCodeAt(0)) | 0;
  return COLORS[Math.abs(h) % COLORS.length];
};

type Form = { id?: string; display_name: string; display_name_ar: string; email: string };
type Pending = { kind: "person" | "voice" | "me"; p: PersonT } | null;

export default function People({ ctx }: { ctx: Ctx }) {
  const { t, tr, lang } = useI18n();
  const [people, setPeople] = useState<PersonT[] | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const load = () => setup.people().then((r) => setPeople(r.people), ctx.fail);
  useEffect(() => {
    load();
  }, []);

  const save = async () => {
    if (!form?.display_name.trim()) return;
    const body = { display_name: form.display_name.trim(), display_name_ar: form.display_name_ar.trim() || null,
      email: form.email.trim() || null };
    try {
      if (form.id) {
        const r = await setup.editPerson(form.id, body);
        ctx.toast(r.meetings ? `${t("saved")} · ${r.meetings} ${t("meetings").toLowerCase()}` : t("saved"));
      } else {
        await setup.addPerson(body);
        ctx.toast(t("saved"));
      }
      setForm(null);  // only on success: a rejected form keeps what was typed
      load();
    } catch (e) {
      ctx.fail(e);
    }
  };
  const confirm = async () => {
    if (!pending) return;
    const { kind, p } = pending;
    setPending(null);
    if (kind !== "voice") setForm(null);  // the person is gone (or merged): close their form
    try {
      if (kind === "person") await setup.deletePerson(p.id);
      else if (kind === "voice") await setup.deleteVoiceprint(p.id);
      else await setup.thisIsMe(p.id);
      ctx.toast(kind === "person" ? tr(`${shown(p)} deleted`, `تم حذف ${shown(p)}`)
        : kind === "voice" ? tr("Voice deleted", "تم حذف الصوت") : tr("Merged into your profile", "تم الدمج في ملفك"));
    } catch (e) {
      ctx.fail(e);
    }
    load();
  };
  const voice = (p: PersonT) => {
    if (!p.voice) return null;
    const secs = p.voice.seconds;
    return `${p.voice.samples} ${t("samples")} · ${secs >= 60 ? `${(secs / 60).toFixed(1)} min` : `${Math.round(secs)} s`}`;
  };
  const shown = (p: PersonT) => (p.is_self && p.display_name === "You" ? t("you") : lang === "ar" && p.display_name_ar ? p.display_name_ar : p.display_name);
  const other = (p: PersonT) => (lang === "ar" && p.display_name_ar ? p.display_name : p.display_name_ar);
  const editing = form?.id ? people?.find((x) => x.id === form.id) : undefined;
  const editor = form && (
    <Modal title={form.id ? tr(`Edit ${editing ? shown(editing) : ""}`, `تعديل ${editing ? shown(editing) : ""}`) : t("addPerson")}
      onClose={() => setForm(null)}
      footer={<>
        {editing && !editing.is_self && (
          <button className="btn ghost sm modal-aside" onClick={() => setPending({ kind: "me", p: editing })}
            title={tr("Is this actually you? Merge it into your own profile, with its voice.", "هل هذا أنت؟ ادمجه في ملفك الشخصي مع صوته.")}>
            <I.confirm size={14} />{t("thisIsMe")}</button>
        )}
        <button className="btn" onClick={() => setForm(null)}>{t("cancel")}</button>
        <button className="btn primary" onClick={save} disabled={!form.display_name.trim()}><I.check size={15} />{t("save")}</button>
      </>}>
      <form className="pform" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <div className="field"><label htmlFor="p-name">{t("name")}</label><input id="p-name" className="input" autoFocus dir="auto" value={form.display_name}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })} placeholder="Sara Haddad" /></div>
        <div className="field"><label htmlFor="p-ar">{t("arabicName")}</label><input id="p-ar" className="input" dir="rtl" value={form.display_name_ar}
          onChange={(e) => setForm({ ...form, display_name_ar: e.target.value })} placeholder="سارة حداد" /></div>
        <div className="field"><label htmlFor="p-email">{t("email")}</label><input id="p-email" className="input" dir="ltr" value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder={`sara@example.com (${t("optional")})`} /></div>
        {form.id && <p className="faint modal-hint">{tr("Renaming updates every meeting this person is in.", "تغيير الاسم يحدّث كل الاجتماعات التي يظهر فيها هذا الشخص.")}</p>}
        <button type="submit" hidden />
      </form>
    </Modal>
  );
  const question = pending && ({
    person: tr(`${shown(pending.p)} is removed from People and their saved voice is deleted. In past meetings they show as “Person 1”, “Person 2”… again.`,
      `يُحذف ${shown(pending.p)} من الأشخاص ويُحذف صوته المحفوظ. في الاجتماعات السابقة يظهر مجدداً باسم «Person 1»، «Person 2»…`),
    voice: tr(`Modwen forgets ${shown(pending.p)}'s voice and won't recognise them in new meetings. Their name stays.`,
      `ينسى مُدوَّن صوت ${shown(pending.p)} ولن يتعرّف عليه في الاجتماعات الجديدة. يبقى الاسم.`),
    me: tr(`Merge ${shown(pending.p)} into your profile? Their voice and meetings become yours.`,
      `دمج ${shown(pending.p)} في ملفك؟ يصبح صوته واجتماعاته لك.`),
  })[pending.kind];

  return (
    <>
      <div className="page-head">
        <div><h1>{t("people")}</h1><p className="lead">{t("peopleLead")}</p></div>
        <button className="btn primary" onClick={() => setForm({ display_name: "", display_name_ar: "", email: "" })}><I.addPerson />{t("addPerson")}</button>
      </div>



      {people === null && <p className="muted">{t("loading")}</p>}
      {people && people.length === 0 && !form && <div className="empty"><p>{t("noPeople")}</p></div>}
      {people && people.length > 0 && (
        <div className="pgrid">
          {people.map((p) => (
            <div key={p.id} className="card pcard">
              <div className="row">
                <span className="av" style={{ background: colorFor(p) }}>{(shown(p).trim()[0] ?? "?").toUpperCase()}</span>
                <div style={{ minWidth: 0 }}>
                  <div className="nm" dir="auto">{shown(p)}{p.is_self ? <span className="badge acc">{t("isYou")}</span> : null}</div>
                  <div className="sub" dir="auto">{other(p) || p.email || (p.is_self && p.display_name === "You" ? t("setYourName") : "")}</div>
                </div>
              </div>
              <>
                <div className={`state ${p.voice ? "ok" : "no"}`}>
                  <I.voice size={15} />{p.voice ? <>{t("voiceSaved")} <span className="faint">· {voice(p)}</span></> : t("noVoice")}
                </div>
                <div className="acts-row">
                  <button className="btn sm" onClick={() => setForm({ id: p.id, display_name: p.display_name,
                    display_name_ar: p.display_name_ar ?? "", email: p.email ?? "" })}><I.edit size={14} />{t("edit")}</button>
                  {p.voice && <button className="btn sm ghost" onClick={() => setPending({ kind: "voice", p })}><I.voice size={14} />{t("deleteVoice")}</button>}
                  {!p.is_self && <button className="btn sm ghost danger-text" onClick={() => setPending({ kind: "person", p })}><I.trash size={14} />{t("del")}</button>}
                </div>
              </>
            </div>
          ))}
        </div>
      )}
      {editor}
      {pending && (
        <Modal size="sm" title={pending.kind === "person" ? tr("Delete this person?", "حذف هذا الشخص؟")
            : pending.kind === "voice" ? tr("Delete this voice?", "حذف هذا الصوت؟") : tr("Merge into your profile?", "الدمج في ملفك؟")}
          onClose={() => setPending(null)}
          footer={<>
            <button className="btn" onClick={() => setPending(null)}>{t("cancel")}</button>
            <button className={`btn ${pending.kind === "me" ? "primary" : "danger"}`} onClick={confirm}>
              {pending.kind === "me" ? t("thisIsMe") : t("del")}</button>
          </>}>
          <p className="modal-text">{question}</p>
        </Modal>
      )}
    </>
  );
}
