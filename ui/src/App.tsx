import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, BBEvent, connectEvents, RecorderStatus } from "./api";
import Home from "./pages/Home";
import Start from "./pages/Start";
import Live from "./pages/Live";
import Review from "./pages/Review";
import Settings from "./pages/Settings";
import RecordingBar from "./components/RecordingBar";
import Pill from "./pages/Pill";
import People from "./pages/People";
import Welcome from "./pages/Welcome";

export type Ctx = {
  recorder: RecorderStatus;
  tier: string;
  llmActive: string;
  events: BBEvent[];
  lastEvent: BBEvent | null;
  toast: (msg: string) => void;
  go: (hash: string) => void;
  refreshStatus: () => void;
};

function useHash(): string {
  const [hash, setHash] = useState(location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export default function App() {
  const hash = useHash();
  const [recorder, setRecorder] = useState<RecorderStatus>({ recording: false });
  const [tier, setTier] = useState("?");
  const [llmActive, setLlmActive] = useState("");
  const [authError, setAuthError] = useState(false);
  const [lastEvent, setLastEvent] = useState<BBEvent | null>(null);
  const [events, setEvents] = useState<BBEvent[]>([]);
  const [toastMsg, setToastMsg] = useState<string | null>(null);
  const firstRunChecked = useRef(false);

  const toast = useCallback((msg: string) => {
    setToastMsg(msg);
    window.setTimeout(() => setToastMsg(null), 1800);
  }, []);
  const go = useCallback((h: string) => {
    location.hash = h;
  }, []);

  const refreshStatus = useCallback(() => {
    api
      .status()
      .then((s) => {
        setRecorder(s.recorder);
        setTier(s.tier);
        setLlmActive(s.llm_active);
        setAuthError(false);
        if (!s.setup_complete && !firstRunChecked.current && !location.hash.startsWith("#/pill")) {
          firstRunChecked.current = true;
          if (!location.hash.startsWith("#/welcome")) location.hash = "#/welcome";
        }
        firstRunChecked.current = true;
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) setAuthError(true);
      });
  }, []);

  useEffect(() => {
    refreshStatus();
    const stop = connectEvents((e) => {
      setLastEvent(e);
      setEvents((prev) => [...prev.slice(-300), e]);
      if (e.type === "recorder") setRecorder(e as unknown as RecorderStatus);
      if (e.type === "hello") setRecorder((e as unknown as { recorder: RecorderStatus }).recorder);
      if (["recording_started", "recording_stopped", "status"].includes(e.type)) refreshStatus();
    });
    const t = window.setInterval(refreshStatus, 5000);
    return () => {
      stop();
      window.clearInterval(t);
    };
  }, [refreshStatus]);

  const ctx: Ctx = { recorder, tier, llmActive, events, lastEvent, toast, go, refreshStatus };
  const path = hash.replace(/^#/, "") || "/";
  let page = <Home ctx={ctx} />;
  let nav = "meetings";
  if (path.startsWith("/start")) page = <Start ctx={ctx} />;
  else if (path.startsWith("/live")) page = <Live ctx={ctx} />;
  else if (path.startsWith("/meetings/")) page = <Review ctx={ctx} id={path.split("/")[2]} />;
  else if (path.startsWith("/settings")) {
    page = <Settings ctx={ctx} />;
    nav = "settings";
  } else if (path.startsWith("/people")) {
    page = <People ctx={ctx} />;
    nav = "people";
  } else if (path.startsWith("/welcome")) {
    page = <Welcome ctx={ctx} />;
    nav = "";
  }

  if (path.startsWith("/pill") && !authError) {
    return <Pill ctx={ctx} />;
  }

  if (authError) {
    return (
      <div className="main" style={{ maxWidth: 640, margin: "60px auto" }}>
        <h1>Modwen</h1>
        <div className="banner err">This page needs the session link. Open Modwen from the tray icon, or use the
          link printed by <code>modwen serve</code> (it ends with <code>?token=…</code>).</div>
      </div>
    );
  }

  return (
    <div className="app">
      <aside className="side">
        <div className="brand"><span className="logo">م</span>Modwen</div>
        <button className="btn danger" style={{ justifyContent: "center", marginBottom: 10 }}
          disabled={recorder.recording} onClick={() => go("#/start")}>● Start meeting</button>
        <a className={`nav ${nav === "meetings" ? "on" : ""}`} href="#/">▤ Meetings</a>
        <a className={`nav ${nav === "people" ? "on" : ""}`} href="#/people">☺ People</a>
        <a className={`nav ${nav === "settings" ? "on" : ""}`} href="#/settings">⚙ Settings</a>
        <span className="spacer" />
        <div className="status">🔒 Local only<br />Tier {tier} · {llmActive}</div>
      </aside>
      <main className="main">
        {recorder.recording && !path.startsWith("/live") && <RecordingBar ctx={ctx} compact />}
        {page}
      </main>
      {toastMsg && <div className="toast">✓ {toastMsg}</div>}
    </div>
  );
}
