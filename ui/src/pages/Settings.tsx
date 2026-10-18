import { Fragment, useEffect, useState } from "react";
import { api, setup, SetupStatus } from "../api";
import type { Ctx } from "../App";
import JobProgress, { useJob } from "../components/JobProgress";

type Tab = "general" | "audio" | "transcription" | "ai" | "notifications" | "privacy" | "about";
const TABS: [Tab, string][] = [["general", "General"], ["audio", "Audio"], ["transcription", "Transcription"],
  ["ai", "AI model"], ["notifications", "Notifications"], ["privacy", "Privacy"], ["about", "About"]];

type Profiles = Awaited<ReturnType<typeof api.profiles>>;
type TestResult = Awaited<ReturnType<typeof api.testLlm>>;

export default function Settings({ ctx }: { ctx: Ctx }) {
  const initial = (location.hash.split("/")[2] as Tab) || "ai";
  const [tab, setTab] = useState<Tab>(TABS.some(([t]) => t === initial) ? initial : "ai");
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
      ctx.toast("Saved");
    } catch (e) {
      alert((e as Error).message);
    }
  };
  const go = (t: Tab) => {
    setTab(t);
    history.replaceState(null, "", `#/settings/${t}`);
  };

  return (
    <>
      <div className="page-head"><h1>Settings</h1>
        <span className="faint" style={{ fontSize: 12 }}>Saved to {st?.config_path ?? "config.yaml"}</span></div>
      <div className="tabs">
        {TABS.map(([t, label]) => <button key={t} className={tab === t ? "on" : ""} onClick={() => go(t)}>{label}</button>)}
      </div>
      {settings && st && (
        <>
          {tab === "general" && <General settings={settings} save={save} ctx={ctx} />}
          {tab === "audio" && <Audio settings={settings} save={save} st={st} />}
          {tab === "transcription" && <Transcription settings={settings} save={save} st={st} ctx={ctx} />}
          {tab === "ai" && <AIModel ctx={ctx} st={st} reload={reload} />}
          {tab === "notifications" && <Notifications settings={settings} save={save} />}
          {tab === "privacy" && <Privacy />}
          {tab === "about" && <About st={st} ctx={ctx} />}
        </>
      )}
    </>
  );
}

type SP = { settings: Record<string, unknown>; save: (p: Record<string, unknown>) => void };

function Select({ value, options, onChange }: { value: string; options: [string, string][]; onChange: (v: string) => void }) {
  return <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
    {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>;
}

function General({ settings, save, ctx }: SP & { ctx: Ctx }) {
  const tzs = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  const sysTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (
    <div className="card" style={{ maxWidth: 680 }}>
      <div className="field"><label>Summary language</label>
        <Select value={String(settings["notes.summary_language"])} onChange={(v) => save({ "notes.summary_language": v })}
          options={[["en", "English (default)"], ["ar", "Arabic"], ["both", "English + Arabic"]]} /></div>
      <div className="field"><label>Long meetings</label>
        <Select value={String(settings["notes.chunk_minutes"])} onChange={(v) => save({ "notes.chunk_minutes": Number(v) })}
          options={[["10", "Split every 10 min"], ["15", "Split every 15 min"], ["20", "Split every 20 min"], ["30", "Split every 30 min"]]} /></div>
      <div className="field"><label>Timezone (deadlines)</label>
        <Select value={String(settings["notes.timezone"])} onChange={(v) => save({ "notes.timezone": v })}
          options={[["system", `System (${sysTz})`], ...tzs.map((t) => [t, t] as [string, string])]} /></div>
      <div className="field"><label>Setup</label>
        <div><button className="btn" onClick={() => ctx.go("#/welcome")}>Run the setup wizard again</button></div></div>
      <Network ctx={ctx} save={save} />
    </div>
  );
}

function Network({ ctx, save }: { ctx: Ctx; save: (p: Record<string, unknown>) => void }) {
  const [net, setNet] = useState<Awaited<ReturnType<typeof setup.network>> | null>(null);
  const load = () => setup.network().then(setNet).catch(() => undefined);
  useEffect(() => {
    load();
  }, []);
  if (!net) return null;
  return (
    <div className="field" style={{ alignItems: "start" }}><label>Other devices</label>
      <div>
        <label className="check"><input type="checkbox" checked={net.lan_access_setting}
          onChange={async (e) => { await save({ "app.lan_access": e.target.checked }); load(); }} />
          Allow access from other devices on this network</label>
        <div className="faint" style={{ fontSize: 12, marginTop: 4 }}>Anyone with the link below can use Modwen. The
          connection is plain HTTP, so use trusted networks only (home / office). Recording still uses this computer's mic.</div>
        {net.restart_needed && <div className="banner warn" style={{ marginTop: 8 }}>Restart Modwen to apply
          ({net.lan_access_setting ? "turn on" : "turn off"} network access).</div>}
        {net.lan_active && net.urls.map((u) => (
          <div key={u} style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
            <code style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 380 }}>{u.split("?")[0]}</code>
            <button className="btn sm" onClick={() => navigator.clipboard.writeText(u).then(() => ctx.toast("Link copied"))}>Copy link</button>
          </div>
        ))}
      </div>
    </div>
  );
}

function Audio({ settings, save, st }: SP & { st: SetupStatus }) {
  const [devices, setDevices] = useState<{ index: number; name: string; default: boolean }[]>([]);
  const [test, setTest] = useState<Awaited<ReturnType<typeof setup.audioTest>> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.devices().then((d) => setDevices(d.inputs));
  }, []);
  const lv = (k: string) => Math.round((test?.levels[k] ?? 0) * 100);
  const runTest = async () => {
    setBusy(true);
    try {
      setTest(await setup.audioTest());
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card" style={{ maxWidth: 680 }}>
      <div className="field"><label>Microphone</label>
        <Select value={settings["capture.mic_device"] == null ? "" : String(settings["capture.mic_device"])}
          onChange={(v) => save({ "capture.mic_device": v || null })}
          options={[["", "System default"], ...devices.map((d) => [d.name, d.name + (d.default ? " (default)" : "")] as [string, string])]} /></div>
      <div className="field"><label>Meeting audio</label>
        <Select value={String(settings["capture.system_backend"])} onChange={(v) => save({ "capture.system_backend": v })}
          options={[["auto", st.hardware.os === "Windows" ? "Automatic (WASAPI loopback)" : st.hardware.os === "macOS" ? "Automatic (Core Audio tap)" : "Automatic"],
            ["device", "A loopback input device (e.g. BlackHole)"], ["none", "Off (mic only)"]]} /></div>
      {settings["capture.system_backend"] === "device" && (
        <div className="field"><label>Loopback device</label>
          <Select value={String(settings["capture.system_device"] ?? "")} onChange={(v) => save({ "capture.system_device": v || null })}
            options={[["", "Choose…"], ...devices.map((d) => [d.name, d.name] as [string, string])]} /></div>
      )}
      <div className="field"><label>Test</label>
        <div>
          <button className="btn" disabled={busy} onClick={runTest}>{busy ? "Listening 3 s…" : "🎙 Test 3 seconds"}</button>
          {test && ["mic", "system"].map((k) => (
            <div key={k} style={{ marginTop: 8, fontSize: 12.5 }}>{k === "mic" ? "Mic" : "Meeting audio"}:{" "}
              {test.errors[k] ? <span style={{ color: "var(--rec)" }}>✕ {test.errors[k]}</span>
                : <><span className="progress" style={{ display: "inline-block", width: 160, verticalAlign: "middle" }}><i style={{ width: `${lv(k)}%` }} /></span> {lv(k) > 15 ? "✓" : "silent"}</>}
            </div>
          ))}
        </div></div>
    </div>
  );
}

function Transcription({ settings, save, st, ctx }: SP & { st: SetupStatus; ctx: Ctx }) {
  const [glossary, setGlossary] = useState(((settings["stt.glossary"] as string[]) ?? []).join(", "));
  const [job, setJob] = useJob(ctx, st.jobs.find((j) => j.kind === "whisper" && j.status === "running") ?? null);
  const models = ["auto", "large-v3", "large-v3-turbo", "medium", "small", "base"];
  return (
    <div className="card" style={{ maxWidth: 680 }}>
      <div className="field"><label>Meeting language</label>
        <Select value={String(settings["stt.meeting_language"])} onChange={(v) => save({ "stt.meeting_language": v })}
          options={[["auto", "Auto (Arabic + English)"], ["en", "English only"], ["ar", "Arabic only"]]} /></div>
      <div className="field"><label>Final transcript</label>
        <Select value={String(settings["stt.final.model"])} onChange={(v) => save({ "stt.final.model": v })}
          options={models.map((m) => [m, m === "auto" ? `Automatic (${st.plan.final_model})` : m] as [string, string])} /></div>
      <div className="field"><label>Live captions</label>
        <div style={{ display: "flex", gap: 8 }}>
          <Select value={String(settings["stt.live.provider"])} onChange={(v) => save({ "stt.live.provider": v })}
            options={[["local_whisper", "On"], ["none", "Off"]]} />
          <Select value={String(settings["stt.live.model"])} onChange={(v) => save({ "stt.live.model": v })}
            options={models.map((m) => [m, m === "auto" ? `Automatic (${st.plan.live_model})` : m] as [string, string])} />
        </div></div>
      <div className="field"><label>Glossary</label>
        <div><textarea className="input" rows={2} value={glossary} onChange={(e) => setGlossary(e.target.value)}
          placeholder="Names and terms you use: Lina, لينا, MVP, roadmap"
          onBlur={() => save({ "stt.glossary": glossary.split(",").map((x) => x.trim()).filter(Boolean) })} />
          <div className="faint" style={{ fontSize: 12 }}>Helps Whisper spell names and product terms. Keep it short.</div></div></div>
      <div className="field"><label>Models on disk</label>
        <div>{st.whisper.models.map((m) => <span key={m.name + m.role} className={`badge ${m.present ? "ok" : "warn"}`} style={{ marginInlineEnd: 6 }}>
          {m.role}: {m.name} {m.present ? "✓" : "missing"}</span>)}
          {st.whisper.models.some((m) => !m.present) && <div><button className="btn sm" style={{ marginTop: 6 }}
            onClick={async () => setJob((await setup.downloadWhisper()).job)}>⇩ Download</button></div>}
          <JobProgress job={job} /></div></div>
    </div>
  );
}

function AIModel({ ctx, st, reload }: { ctx: Ctx; st: SetupStatus; reload: () => void }) {
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
    ctx.toast(`Active AI model: ${name}`);
  };
  const startManaged = () => setup.startManaged().then(() => { reload(); load(); }).catch((e) => alert((e as Error).message));
  if (!profiles) return <p className="muted">Loading…</p>;
  const active = profiles.profiles[profiles.active];

  return (
    <>
      <div className="field" style={{ gridTemplateColumns: "130px minmax(0,320px) auto" }}>
        <label>Active profile</label>
        <select className="select" value={profiles.active} onChange={(e) => activate(e.target.value)}>
          {Object.keys(profiles.profiles).map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        <span className={`badge ${st.llm.online ? "ok" : ""}`}><span className="dot" />
          {st.llm.online ? `online · ${active?.model} · ${Math.round((active?.context_window ?? 0) / 1024)}k context` : "offline"}</span>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <b>Modwen-managed AI model</b> <span className="faint" style={{ fontSize: 12.5 }}>· Modwen downloads and runs llama.cpp + Qwen for you</span>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 10 }}>
          <select className="select" style={{ maxWidth: 360 }} value={choice} onChange={(e) => setChoice(e.target.value)}>
            {st.llm.choices.map((c) => <option key={c.id} value={c.id}>{c.id}{c.id === st.llm.recommended ? " (recommended)" : ""} · {c.size_gb} GB</option>)}
          </select>
          {m.binary_installed && m.model_installed && m.model_choice === choice ? (
            <>
              <span className={`badge ${m.running ? "ok" : ""}`}>{m.running ? "● running" : "installed"}</span>
              {m.running ? <button className="btn sm" onClick={() => setup.stopManaged().then(reload)}>Stop</button>
                : <button className="btn sm" onClick={startManaged}>Start</button>}
              {profiles.active !== "managed_qwen" && profiles.profiles["managed_qwen"] && <button className="btn sm" onClick={() => activate("managed_qwen")}>Use</button>}
            </>
          ) : (
            <button className="btn primary sm" disabled={lJob?.status === "running"}
              onClick={async () => setLJob((await setup.installManaged(choice)).job)}>⇩ Download and start</button>
          )}
        </div>
        {m.port_busy_external && !m.running && <div className="banner warn" style={{ marginTop: 8 }}>Port {m.port} is used by another server.</div>}
        <JobProgress job={lJob} />
      </div>

      <div className="section-title">Profiles</div>
      <div style={{ overflowX: "auto" }}>
        <table className="ptable">
          <thead><tr><th>Name</th><th>Endpoint</th><th>Model</th><th>Context</th><th /></tr></thead>
          <tbody>
            {Object.entries(profiles.profiles).map(([name, p]) => (
              <Fragment key={name}>
                <tr>
                  <td><b>{name}</b> {profiles.active === name ? <span className="badge acc">active</span>
                    : profiles.fallback.includes(name) ? <span className="badge">fallback</span> : null}</td>
                  <td><code>{p.base_url}</code></td><td>{p.model}</td><td>{p.context_window.toLocaleString()}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn sm" onClick={() => setEditing(editing === name ? null : name)}>Edit</button>{" "}
                    <button className="btn sm" disabled={tests[name] === "running"} onClick={() => runTest(name)}>
                      {tests[name] === "running" ? "Testing…" : "Test"}</button>{" "}
                    {profiles.active !== name && <button className="btn sm" onClick={() => activate(name)}>Use</button>}{" "}
                    {profiles.active !== name && <button className="btn sm ghost" title="Delete profile" onClick={() => setup.deleteProfile(name).then(load)}>✕</button>}
                  </td>
                </tr>
                {editing === name && <tr><td colSpan={5}><ProfileForm name={name} p={p} onDone={() => { setEditing(null); load(); reload(); }} /></td></tr>}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {editing === "__new"
        ? <div className="card" style={{ marginTop: 10 }}><ProfileForm name="" onDone={() => { setEditing(null); load(); }} /></div>
        : <button className="btn ghost sm" style={{ marginTop: 8 }} onClick={() => setEditing("__new")}>+ Add local profile <span className="faint">(localhost / 127.0.0.1 only)</span></button>}
      {Object.entries(tests).map(([name, t]) => t !== "running" && (
        <div key={name} className={`banner ${t.ok ? "info" : "err"}`} style={{ marginTop: 10, display: "block" }}>
          <b>{name}:</b> {t.ok ? <>✓ replied in {t.seconds}s<div dir="auto" style={{ whiteSpace: "pre-wrap", marginTop: 6, color: "var(--text)" }}>{t.reply}</div></>
            : <>✕ {t.error}</>}
        </div>
      ))}
      <p className="faint" style={{ fontSize: 12, marginTop: 12 }}>Changes are written to config.yaml. Non-localhost URLs are rejected: Modwen is local-only.</p>
    </>
  );
}

function ProfileForm({ name, p, onDone }: { name: string; p?: Profiles["profiles"][string]; onDone: () => void }) {
  const [f, setF] = useState({ name, base_url: p?.base_url ?? "http://localhost:11434/v1", model: p?.model ?? "",
    context_window: p?.context_window ?? 16384, temperature: p?.temperature ?? 0.3,
    structured_output: p?.structured_output ?? "json_schema", thinking: false, api_key: "" });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: string, v: unknown) => setF({ ...f, [k]: v });
  const submit = async () => {
    setErr(null);
    const key = (f.name || "").trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_");
    if (!key || !f.model) return setErr("Name and model are required");
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
        {!name && <div className="field"><label>Name</label><input className="input" value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="lm_studio" /></div>}
        <div className="field"><label>Endpoint</label><input className="input" value={f.base_url} onChange={(e) => set("base_url", e.target.value)} /></div>
        <div className="field"><label>Model</label><input className="input" value={f.model} onChange={(e) => set("model", e.target.value)} placeholder="qwen3:8b" /></div>
        <div className="field"><label>API key</label><input className="input" value={f.api_key} onChange={(e) => set("api_key", e.target.value)} placeholder="(unchanged / not needed)" /></div>
      </div>
      <div>
        <div className="field"><label>Context</label><input className="input" type="number" value={f.context_window} onChange={(e) => set("context_window", e.target.value)} /></div>
        <div className="field"><label>Temperature</label><input className="input" type="number" step="0.1" value={f.temperature} onChange={(e) => set("temperature", e.target.value)} /></div>
        <div className="field"><label>Structured output</label>
          <Select value={f.structured_output} onChange={(v) => set("structured_output", v)}
            options={[["json_schema", "json_schema"], ["json_object", "json_object"], ["prompt_only", "prompt_only"]]} /></div>
        <div className="field"><label>Thinking</label>
          <Select value={f.thinking ? "on" : "off"} onChange={(v) => set("thinking", v === "on")}
            options={[["off", "Off (faster, recommended)"], ["on", "On"]]} /></div>
      </div>
      <div style={{ gridColumn: "1 / -1", display: "flex", gap: 8, alignItems: "center" }}>
        <button className="btn primary" onClick={submit}>Save</button><button className="btn" onClick={onDone}>Cancel</button>
        {err && <span className="banner err" style={{ margin: 0 }}>{err}</span>}
      </div>
    </div>
  );
}

function Notifications({ settings, save }: SP) {
  return (
    <div className="card" style={{ maxWidth: 680 }}>
      <label className="check"><input type="checkbox" checked={Boolean(settings["notify.desktop"])}
        onChange={(e) => save({ "notify.desktop": e.target.checked })} />"Notes ready" notification when the AI model finishes</label>
      <label className="check" style={{ marginTop: 8 }}><input type="checkbox" checked={Boolean(settings["notify.sound"])}
        onChange={(e) => save({ "notify.sound": e.target.checked })} />Play a sound</label>
      <button className="btn" style={{ marginTop: 12 }} onClick={() => setup.notifyTest()}>🔔 Send test notification</button>
    </div>
  );
}

function Privacy() {
  return (
    <div className="card" style={{ maxWidth: 680 }}>
      <p className="lock" style={{ fontSize: 14 }}>🔒 Local only, always</p>
      <ul className="muted">
        <li>Meeting audio and transcripts never leave this computer. Non-localhost AI endpoints are refused in code.</li>
        <li>Audio is stored encrypted (AES-256-GCM, a separate key per meeting). Deleting a meeting destroys its key.</li>
        <li>Downloads happen only when you press a Download button (speech models, llama.cpp, Qwen). No telemetry.</li>
        <li>Retention: audio 7 days, transcripts 90 days, notes 365 days (automatic clean-up: next release).</li>
      </ul>
    </div>
  );
}

function About({ st, ctx }: { st: SetupStatus; ctx: Ctx }) {
  return (
    <div className="grid2">
      <div className="card">
        <b>Tier {st.plan.tier}</b>: {st.plan.label}
        <dl className="kv" style={{ marginTop: 10 }}>
          <dt>Speech-to-text</dt><dd>{st.whisper.backend} · final {st.plan.final_model} · live {st.plan.live_model}</dd>
          <dt>Recommended AI model</dt><dd>{st.plan.llm_model} ({st.plan.llm_context} context)</dd>
          <dt>2-hour meeting</dt><dd>about {st.plan.est_2h_minutes[0]}–{st.plan.est_2h_minutes[1]} min after Stop</dd>
        </dl>
      </div>
      <div className="card">
        <b>Run your own llama.cpp instead</b>
        <div className="codeblock" style={{ marginTop: 8 }}>{st.plan.llama_server_command}</div>
        <button className="btn sm" style={{ marginTop: 8 }} onClick={() =>
          navigator.clipboard.writeText(st.plan.llama_server_command).then(() => ctx.toast("Command copied"))}>Copy command</button>
      </div>
      <div className="card">
        <b>Machine</b>
        <dl className="kv" style={{ marginTop: 10 }}>
          {Object.entries(st.hardware).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>)}
        </dl>
      </div>
    </div>
  );
}
