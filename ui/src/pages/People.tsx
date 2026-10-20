import { useEffect, useState } from "react";
import { PersonT, setup } from "../api";
import type { Ctx } from "../App";

// People Modwen can name in meetings. Names here are suggested in the Speakers drawer, and people whose voice
// was saved (with their consent) are recognised automatically in later meetings (design §5.1).

const voiceText = (p: PersonT) => {
  if (!p.voice) return null;
  const mins = p.voice.seconds / 60;
  const kinds = Object.entries(p.voice.kinds).map(([k, n]) => `${n} ${k === "remote" ? "call" : "mic/room"}`).join(", ");
  return `${p.voice.samples} sample${p.voice.samples > 1 ? "s" : ""} (${kinds}) · ${mins >= 1 ? `${mins.toFixed(1)} min` : `${Math.round(p.voice.seconds)} s`}`;
};
export default function People({ ctx }: { ctx: Ctx }) {
  const [people, setPeople] = useState<PersonT[] | null>(null);
  const [form, setForm] = useState<{ id?: string; display_name: string; display_name_ar: string; email: string } | null>(null);
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
        ctx.toast(r.meetings ? `Saved · updated in ${r.meetings} meeting${r.meetings > 1 ? "s" : ""}` : "Saved");
      } else {
        await setup.addPerson(body);
        ctx.toast("Saved");
      }
      setForm(null);  // only on success: a rejected form keeps what was typed
      load();
    } catch (e) {
      ctx.fail(e);
    }
  };
  const remove = async (p: PersonT) => {
    const voice = p.voice ? " and their saved voice" : "";
    if (!confirm(`Delete ${p.display_name}${voice}? In meetings they become "Person N" again. This cannot be undone.`)) return;
    try {
      const r = await setup.deletePerson(p.id);
      ctx.toast(r.meetings ? `Deleted · ${r.meetings} meeting${r.meetings > 1 ? "s" : ""} updated` : "Deleted");
    } catch (e) {
      ctx.fail(e);
    }
    load();
  };
  const thisIsMe = async (p: PersonT) => {
    if (!confirm(`Is ${p.display_name} you? Your own entry, and its saved voice, will be merged into ${p.display_name}, and your speaker in every meeting will show this name.`)) return;
    try {
      await setup.thisIsMe(p.id);
      ctx.toast(`${p.display_name} is you now`);
    } catch (e) {
      ctx.fail(e);
    }
    load();
  };
  const deleteVoice = async (p: PersonT) => {
    const extra = p.is_self ? " \"Remember my voice\" will be turned off too." : "";
    if (!confirm(`Delete ${p.display_name}'s voiceprint? Modwen will stop recognising them.${extra}`)) return;
    try {
      await setup.deleteVoiceprint(p.id);
      ctx.toast("Voiceprint deleted");
    } catch (e) {
      ctx.fail(e);
    }
    load();
  };

  return (
    <>
      <div className="page-head"><h1>People</h1>
        <button className="btn primary" onClick={() => setForm({ display_name: "", display_name_ar: "", email: "" })}>+ Add person</button></div>
      <p className="muted" style={{ marginTop: -6 }}>People you meet often. Their names are suggested when you name speakers in a
        meeting, and the Arabic spelling helps the notes. When you name a speaker and tick <i>Remember this voice</i>, Modwen
        recognises that person in later meetings. Voiceprints are encrypted and never leave this computer.</p>
      {form && (
        <div className="card" style={{ maxWidth: 620, marginBottom: 14 }}>
          <div className="field"><label>Name</label><input className="input" autoFocus value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })} placeholder="Sara Haddad" /></div>
          <div className="field"><label>Arabic name</label><input className="input" dir="rtl" value={form.display_name_ar}
            onChange={(e) => setForm({ ...form, display_name_ar: e.target.value })} placeholder="سارة حداد" /></div>
          <div className="field"><label>Email</label><input className="input" value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="sara@example.com (optional)" /></div>
          <div style={{ display: "flex", gap: 8 }}><button className="btn primary" onClick={save}>Save</button>
            <button className="btn" onClick={() => setForm(null)}>Cancel</button></div>
        </div>
      )}
      {people && people.length === 0 && !form && <div className="card">No people yet. Add the colleagues you meet most often.</div>}
      {people && people.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="ptable">
            <thead><tr><th>Name</th><th>Arabic name</th><th>Email</th><th>Voiceprint</th><th /></tr></thead>
            <tbody>
              {people.map((p) => (
                <tr key={p.id}>
                  <td><b>{p.display_name}</b>{p.is_self ? <span className="badge" style={{ marginInlineStart: 6 }}>you</span> : null}
                    {p.is_self && p.display_name === "You" ? <div className="faint" style={{ fontSize: 12 }}>Edit to set your name</div> : null}</td>
                  <td dir="rtl">{p.display_name_ar}</td>
                  <td className="muted">{p.email || "—"}</td>
                  <td>{p.voice
                    ? <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                        <span className="badge ok" title={p.consent_at ? `Consent recorded ${new Date(p.consent_at).toLocaleDateString()}` : undefined}>
                          ✓ {voiceText(p)}</span>
                        <button className="btn sm ghost" onClick={() => deleteVoice(p)}>Delete voice</button></span>
                    : <span className="badge">Not saved</span>}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn sm" onClick={() => setForm({ id: p.id, display_name: p.display_name,
                      display_name_ar: p.display_name_ar ?? "", email: p.email ?? "" })}>Edit</button>{" "}
                    {!p.is_self && <button className="btn sm ghost" onClick={() => thisIsMe(p)} title="Mark this entry as you">This is me</button>}{" "}
                    {!p.is_self && <button className="btn sm ghost" onClick={() => remove(p)}>Delete</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
