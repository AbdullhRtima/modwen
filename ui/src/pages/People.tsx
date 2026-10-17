import { useEffect, useState } from "react";
import { PersonT, setup } from "../api";
import type { Ctx } from "../App";

// People Modwen can name in meetings. Names here are suggested in the Speakers drawer.
// Voiceprints (automatic recognition by voice) need the optional diarization extra and come in a later release.
export default function People({ ctx }: { ctx: Ctx }) {
  const [people, setPeople] = useState<PersonT[] | null>(null);
  const [form, setForm] = useState<{ id?: string; display_name: string; display_name_ar: string; email: string } | null>(null);
  const load = () => setup.people().then((r) => setPeople(r.people));
  useEffect(() => {
    load();
  }, []);

  const save = async () => {
    if (!form?.display_name.trim()) return;
    const body = { display_name: form.display_name.trim(), display_name_ar: form.display_name_ar.trim() || undefined,
      email: form.email.trim() || undefined };
    if (form.id) await setup.editPerson(form.id, body);
    else await setup.addPerson(body);
    setForm(null);
    load();
    ctx.toast("Saved");
  };

  return (
    <>
      <div className="page-head"><h1>People</h1>
        <button className="btn primary" onClick={() => setForm({ display_name: "", display_name_ar: "", email: "" })}>+ Add person</button></div>
      <p className="muted" style={{ marginTop: -6 }}>People you meet often. Their names are suggested when you name speakers in a
        meeting, and the Arabic spelling helps the notes. <span className="faint">Voice recognition (voiceprints): coming soon.</span></p>
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
                  <td><b>{p.display_name}</b></td>
                  <td dir="rtl">{p.display_name_ar}</td>
                  <td className="muted">{p.email || "—"}</td>
                  <td><span className="badge">Not enrolled</span></td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn sm" onClick={() => setForm({ id: p.id, display_name: p.display_name,
                      display_name_ar: p.display_name_ar ?? "", email: p.email ?? "" })}>Edit</button>{" "}
                    <button className="btn sm ghost" onClick={() => setup.deletePerson(p.id).then(load)}>Delete</button>
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
