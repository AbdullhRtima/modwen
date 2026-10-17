import { useEffect, useState } from "react";
import { setup, SetupStatus } from "../api";
import type { Ctx } from "../App";
import JobProgress, { useJob } from "../components/JobProgress";

const STEPS = ["Permissions", "Hardware check", "AI model + downloads", "Test notification"];

export default function Welcome({ ctx }: { ctx: Ctx }) {
  const [step, setStep] = useState(0);
  const [st, setSt] = useState<SetupStatus | null>(null);
  const reload = () => setup.status().then(setSt);
  useEffect(() => {
    reload();
  }, []);
  useEffect(() => {
    const t = ctx.lastEvent?.type;
    if (t === "job") reload();
  }, [ctx.lastEvent]);

  if (!st) return <p className="muted">Checking this computer…</p>;
  const finish = async () => {
    await setup.complete();
    ctx.toast("Setup complete");
    ctx.go("#/");
  };

  return (
    <div className="card" style={{ maxWidth: 760 }}>
      <h1 style={{ marginTop: 0, fontSize: 19 }}>Welcome to Modwen <span className="muted" style={{ fontWeight: 400 }}>· مُدوَّن</span></h1>
      <div className="wsteps">
        {STEPS.map((name, i) => (
          <span key={name} className={i < step ? "done" : i === step ? "on" : ""} onClick={() => setStep(i)}
            style={{ cursor: "pointer" }}>{i + 1} · {name}{i < step ? " ✓" : ""}</span>
        ))}
      </div>
      {step === 0 && <Permissions />}
      {step === 1 && <Hardware st={st} />}
      {step === 2 && <Models ctx={ctx} st={st} reload={reload} />}
      {step === 3 && <NotifyStep />}
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 20 }}>
        <button className="btn" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</button>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn ghost" onClick={finish}>Skip setup</button>
          {step < 3 ? <button className="btn primary" onClick={() => setStep(step + 1)}>Continue</button>
            : <button className="btn primary" onClick={finish}>Finish</button>}
        </div>
      </div>
    </div>
  );
}

function Permissions() {
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
    <>
      <p>Modwen records <b>your microphone</b> and the <b>meeting audio</b> (what plays on your speakers). Press
        the button, then <b>talk and play any video</b> for 3 seconds. Your computer may ask for permission:
        click <b>Allow</b>.</p>
      <button className="btn primary" disabled={busy} onClick={run}>{busy ? "Listening for 3 seconds…" : "🎙 Test audio"}</button>
      {res && (
        <div className="grid2" style={{ marginTop: 14 }}>
          {["mic", "system"].map((k) => (
            <div key={k} className="card">
              <b>{k === "mic" ? "Microphone (You)" : "System audio (Others)"}</b>
              <div className="progress" style={{ height: 8 }}><i style={{ width: `${lv(k)}%` }} /></div>
              <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>
                {res.errors[k] ? <span style={{ color: "var(--rec)" }}>✕ {res.errors[k]}</span>
                  : lv(k) > 15 ? "✓ Hearing sound" : "No sound detected: talk / play audio and test again"}
                {res.sources[k] ? ` · ${res.sources[k]}` : ""}
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="faint" style={{ fontSize: 12.5, marginTop: 12 }}>macOS: allow <b>Microphone</b> and <b>System Audio
        Recording</b> (System Settings → Privacy &amp; Security) for the app you started Modwen from. Windows: Settings →
        Privacy → Microphone → allow desktop apps.</p>
    </>
  );
}

function Hardware({ st }: { st: SetupStatus }) {
  const p = st.plan;
  return (
    <>
      <div className="tier">
        <div className="big">{p.tier}</div>
        <div><b>Tier {p.tier}: {p.label}</b>
          <div className="muted" style={{ fontSize: 13 }}>A 2-hour meeting should be ready about <b>{p.est_2h_minutes[0]}–{p.est_2h_minutes[1]} min</b> after Stop.</div></div>
      </div>
      <dl className="kv">
        <dt>Live transcript</dt><dd>Whisper {st.whisper.models.find((m) => m.role === "live")?.name} ({st.whisper.backend})</dd>
        <dt>Final transcript</dt><dd>Whisper {st.whisper.models.find((m) => m.role === "final")?.name} ({st.whisper.backend})</dd>
        <dt>Recommended AI model</dt><dd>{p.llm_model} · {p.llm_context.toLocaleString()} context</dd>
        <dt>Memory · free disk</dt><dd>{st.hardware.ram_gb} GB RAM · {st.hardware.free_disk_gb} GB free</dd>
      </dl>
      {p.warnings.map((w) => <div key={w} className="banner warn" style={{ marginTop: 10 }}>⚠ {w}</div>)}
    </>
  );
}

function Models({ ctx, st, reload }: { ctx: Ctx; st: SetupStatus; reload: () => void }) {
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
      setMsg({ ok: true, text: `Connected: ${r.base_url} · model ${r.model}` });
      reload();
      ctx.refreshStatus();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  };

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <b>1. Speech models (Whisper)</b>
        <div className="muted" style={{ fontSize: 13, margin: "4px 0 8px" }}>
          {st.whisper.models.map((x) => `${x.name} ${x.present ? "✓" : ""}`).join(" · ")} · downloaded once, then works offline</div>
        {whisperReady ? <span className="badge ok">✓ Ready</span>
          : <button className="btn primary" disabled={wJob?.status === "running"}
            onClick={async () => setWJob((await setup.downloadWhisper()).job)}>⇩ Download speech models</button>}
        <JobProgress job={wJob} />
      </div>

      <div className="card">
        <b>2. AI model for the notes</b>
        <div className="seg" style={{ margin: "8px 0 12px" }}>
          <button className={mode === "managed" ? "on" : ""} onClick={() => setMode("managed")}>Download Qwen for me (recommended)</button>
          <button className={mode === "external" ? "on" : ""} onClick={() => setMode("external")}>Use my own local AI server</button>
        </div>
        {mode === "managed" ? (
          <>
            <div className="field"><label>Model</label>
              <select className="select" value={choice} onChange={(e) => setChoice(e.target.value)}>
                {st.llm.choices.map((c) => (
                  <option key={c.id} value={c.id}>{c.id}{c.id === st.llm.recommended ? " (recommended for this computer)" : ""} · {c.size_gb} GB</option>
                ))}
              </select></div>
            <p className="faint" style={{ fontSize: 12.5 }}>Modwen downloads llama.cpp and {chosen?.file} (~{chosen?.size_gb} GB) into its
              own folder and starts it automatically on 127.0.0.1:{m.port}. Nothing else is installed on your system.</p>
            {m.binary_installed && m.model_installed && m.model_choice === choice ? (
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span className={`badge ${m.running ? "ok" : ""}`}>{m.running ? "● running" : "installed, not running"}</span>
                {!m.running && <button className="btn sm" onClick={() => setup.startManaged().then(reload).catch((e) => setMsg({ ok: false, text: e.message }))}>Start</button>}
                {st.llm.online && st.llm.active === "managed_qwen" && <span className="badge ok">✓ AI model online</span>}
              </div>
            ) : (
              <button className="btn primary" disabled={lJob?.status === "running"}
                onClick={async () => setLJob((await setup.installManaged(choice)).job)}>⇩ Download and start {choice}</button>
            )}
            {m.port_busy_external && !m.running && <div className="banner warn" style={{ marginTop: 8 }}>
              Port {m.port} is already used by another server. If that's your own AI server, choose "Use my own local AI server".</div>}
            <JobProgress job={lJob} />
          </>
        ) : (
          <>
            <p className="faint" style={{ fontSize: 12.5 }}>Any OpenAI-compatible server on this computer: Ollama
              (http://localhost:11434/v1), LM Studio (http://localhost:1234/v1), llama.cpp (http://localhost:8080/v1), vLLM…
              Only localhost addresses are allowed.</p>
            <div className="field"><label>Server URL</label><input className="input" value={url} onChange={(e) => setUrl(e.target.value)} /></div>
            <div className="field"><label>Model name</label><input className="input" placeholder="leave empty to use the server's first model"
              value={model} onChange={(e) => setModel(e.target.value)} /></div>
            <button className="btn primary" onClick={connect}>Test and use</button>
          </>
        )}
        {msg && <div className={`banner ${msg.ok ? "info" : "err"}`} style={{ marginTop: 10 }}>{msg.ok ? "✓ " : "✕ "}{msg.text}</div>}
        <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
          Active: <b>{st.llm.active}</b> · {st.llm.online ? <span style={{ color: "var(--ok)" }}>online</span> : "offline"}</div>
      </div>
    </>
  );
}

function NotifyStep() {
  const [sent, setSent] = useState(false);
  return (
    <>
      <p>When a meeting is processed, Modwen shows a <b>"Notes ready"</b> notification. Send a test to make sure it
        reaches you (turn off Focus / Do Not Disturb if it doesn't appear).</p>
      <button className="btn primary" onClick={() => setup.notifyTest().then(() => setSent(true))}>🔔 Send test notification</button>
      {sent && <p className="muted">Sent. Did you see "Notes ready: Test meeting"?</p>}
    </>
  );
}
