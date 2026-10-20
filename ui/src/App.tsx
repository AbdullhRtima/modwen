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
import Sidebar from "./components/Sidebar";
import { I } from "./components/icons";
import { useI18n } from "./i18n";

export type Ctx = {
  recorder: RecorderStatus;
  tier: string;
  llmActive: string;
  events: BBEvent[];
  lastEvent: BBEvent | null;
  toast: (msg: string, kind?: "ok" | "error") => void;
  fail: (err: unknown) => void;
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
  const { t } = useI18n();
  const [menu, setMenu] = useState(false);
  const [offline, setOffline] = useState(false);
  const [recorder, setRecorder] = useState<RecorderStatus>({ recording: false });
  const [tier, setTier] = useState("?");
  const [llmActive, setLlmActive] = useState("");
  const [authError, setAuthError] = useState(false);
  const [lastEvent, setLastEvent] = useState<BBEvent | null>(null);
  const [events, setEvents] = useState<BBEvent[]>([]);
  const [toastMsg, setToastMsg] = useState<{ msg: string; kind: "ok" | "error" } | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);
  const firstRunChecked = useRef(false);

  const toast = useCallback((msg: string, kind: "ok" | "error" = "ok") => {
    setToastMsg({ msg, kind });
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToastMsg(null), kind === "error" ? 6000 : 2200);
  }, []);
  const fail = useCallback((err: unknown) => {
    toast(err instanceof Error && err.message ? err.message : t("wrong"), "error");
  }, [toast, t]);
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
        setOffline(false);
        if (!s.setup_complete && !firstRunChecked.current && !location.hash.startsWith("#/pill")) {
          firstRunChecked.current = true;
          if (!location.hash.startsWith("#/welcome")) location.hash = "#/welcome";
        }
        firstRunChecked.current = true;
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) setAuthError(true);
        else if (!(e instanceof ApiError)) setOffline(true);  // the server didn't answer at all
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

  const ctx: Ctx = { recorder, tier, llmActive, events, lastEvent, toast, fail, go, refreshStatus };
  const path = hash.replace(/^#/, "") || "/";
  let page = <Home ctx={ctx} />;
  let narrow = false;
  if (path.startsWith("/start")) { page = <Start ctx={ctx} />; narrow = true; }
  else if (path.startsWith("/live")) page = <Live ctx={ctx} />;
  else if (path.startsWith("/meetings/")) page = <Review key={path.split("/")[2]} ctx={ctx} id={path.split("/")[2]} />;
  else if (path.startsWith("/settings")) page = <Settings ctx={ctx} />;
  else if (path.startsWith("/people")) page = <People ctx={ctx} />;
  else if (path.startsWith("/welcome")) { page = <Welcome ctx={ctx} />; narrow = true; }

  if (path.startsWith("/pill") && !authError) {
    return <Pill ctx={ctx} />;
  }

  if (authError) {
    return (
      <div className="page narrow" style={{ paddingTop: 80 }}>
        <h1 className="page-title">Modwen · مُدوَّن</h1>
        <div className="banner err" style={{ marginTop: 16 }}>{t("needsLink")}</div>
      </div>
    );
  }

  const closeMenu = () => setMenu(false);
  return (
    <div className={`app ${menu ? "menu" : ""}`}>
      <Sidebar ctx={ctx} path={path} onNavigate={closeMenu} />
      <main className="main">
        <div className="topbar">
          <button className="btn ghost" aria-label={t("meetings")} onClick={() => setMenu(true)}><I.menu size={18} /></button>
          <b style={{ fontFamily: "var(--display)" }}>Modwen</b>
        </div>
        {offline && <div className="conn" role="status">{t("offline")}</div>}
        <div className={`page ${narrow ? "narrow" : ""}`}>{page}</div>
      </main>
      {recorder.recording && <RecordingBar ctx={ctx} onLive={path.startsWith("/live")} />}
      {toastMsg && <div className={`toast ${toastMsg.kind}`} role={toastMsg.kind === "error" ? "alert" : "status"}>
        {toastMsg.kind === "error" ? <I.warn /> : <I.check />} {toastMsg.msg}</div>}
    </div>
  );
}
