// Plain-logic test for the onboarding-notice / step-ordering helpers in liveNamesOnboarding.ts.
//
// This repo has no UI component-test runner wired up (no vitest/jest in ui/package.json, no
// other *.test.* file under ui/src — frontend's round-1 report on the chart item flagged this
// as unresolved). Rather than skip testing silently, this file is a plain TypeScript test of the
// one pure, framework-free module this task added: no React, no DOM rendering, just data and a
// tiny localStorage polyfill. It type-checks as part of `ui`'s normal `tsc -b` (nothing here
// needs excluding from the build), but nothing imports it, so it is not bundled into dist/.
//
// To actually execute it (until a real runner is wired up), compile it standalone with the
// project's own `tsc` (no new dependency) and run the output with Node, e.g. from `ui/`:
//   npx tsc --module commonjs --target es2020 --lib es2020,dom \
//     --outDir ../.tmp/t src/liveNamesOnboarding.ts src/liveNamesOnboarding.test.ts
//   node ../.tmp/t/liveNamesOnboarding.test.js
import {
  WELCOME_STEP_ORDER,
  hasSeenLiveNamesNotice,
  markLiveNamesNoticeSeen,
  liveNamesCurrentlyOn,
  liveNamesNoticeEligible,
} from "./liveNamesOnboarding";

declare const globalThis: any;

function assertEqual(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg}: expected ${e}, got ${a}`);
}
function assertTrue(v: unknown, msg: string) {
  if (!v) throw new Error(msg);
}

type Store = Record<string, string>;
// Wraps `backing` directly (not a copy), so a persisted write is visible to a later wrapper
// created over the same backing object — simulating real localStorage surviving a page/app
// reload, where the JS wrapper object is new but the underlying store is not.
function fakeLocalStorage(backing: Store = {}) {
  return {
    getItem(k: string): string | null { return k in backing ? backing[k] : null; },
    setItem(k: string, v: string): void { backing[k] = v; },
  };
}

let failures = 0;
function test(name: string, fn: () => void) {
  try {
    fn();
    console.log(`ok   - ${name}`);
  } catch (e: any) {
    failures++;
    console.log(`FAIL - ${name}`);
    console.log("     " + (e && e.message ? e.message : String(e)));
  }
}

// --- First-launch ordering ------------------------------------------------
test("the new step is immediately after permissions and before hardware/models/notifications", () => {
  assertEqual(WELCOME_STEP_ORDER, ["permissions", "liveNames", "hardware", "models", "notifications"], "step order");
  assertEqual(WELCOME_STEP_ORDER.indexOf("liveNames"), WELCOME_STEP_ORDER.indexOf("permissions") + 1, "liveNames right after permissions");
  assertTrue(WELCOME_STEP_ORDER.indexOf("liveNames") < WELCOME_STEP_ORDER.indexOf("hardware"), "liveNames before hardware");
  assertTrue(WELCOME_STEP_ORDER.indexOf("liveNames") < WELCOME_STEP_ORDER.indexOf("models"), "liveNames before models");
  assertTrue(WELCOME_STEP_ORDER.indexOf("liveNames") < WELCOME_STEP_ORDER.indexOf("notifications"), "liveNames before notifications");
});

// --- Returning users: notice shows exactly once, persists across a "restart" ---
test("notice is unseen by default (fresh profile / first launch)", () => {
  globalThis.localStorage = fakeLocalStorage();
  assertEqual(hasSeenLiveNamesNotice(), false, "unseen by default");
});

test("marking the notice seen makes hasSeenLiveNamesNotice() true immediately", () => {
  globalThis.localStorage = fakeLocalStorage();
  assertEqual(hasSeenLiveNamesNotice(), false, "starts unseen");
  markLiveNamesNoticeSeen();
  assertEqual(hasSeenLiveNamesNotice(), true, "seen after marking");
});

test("acknowledgment survives a simulated app restart (fresh read of the same backing store)", () => {
  const backing: Store = {};
  globalThis.localStorage = fakeLocalStorage(backing);
  markLiveNamesNoticeSeen();
  assertEqual(backing["modwen.liveNamesNoticeSeen"], "1", "backing store was written");
  // Simulate "app restart": a brand-new localStorage object wrapping the SAME persisted backing
  // store (as a real browser/webview would hand back after a reload), not the same JS object.
  globalThis.localStorage = fakeLocalStorage(backing);
  assertEqual(hasSeenLiveNamesNotice(), true, "must still read seen=true after a fresh store read");
});

test("storage failures (private mode) degrade safely: unseen, no throw", () => {
  globalThis.localStorage = {
    getItem() { throw new Error("SecurityError"); },
    setItem() { throw new Error("SecurityError"); },
  };
  assertEqual(hasSeenLiveNamesNotice(), false, "treated as unseen when storage throws");
  let threw = false;
  try { markLiveNamesNoticeSeen(); } catch { threw = true; }
  assertTrue(!threw, "markLiveNamesNoticeSeen must not throw when storage throws");
});

// --- Already-disabled users: conditional wording -------------------------
test("liveNamesCurrentlyOn is true by default (unknown settings, or live_names not present)", () => {
  assertEqual(liveNamesCurrentlyOn(null), true, "null settings");
  assertEqual(liveNamesCurrentlyOn(undefined), true, "undefined settings");
  assertEqual(liveNamesCurrentlyOn({}), true, "empty settings");
});

test("liveNamesCurrentlyOn is false exactly when diarization.live_names === false", () => {
  assertEqual(liveNamesCurrentlyOn({ "diarization.live_names": false }), false, "live_names false");
  assertEqual(liveNamesCurrentlyOn({ "diarization.live_names": true }), true, "live_names true");
  // diarization.live_names_room alone (the feature's sub-switch) does not flip the headline
  // "currently off for you" line — it only matters once live_names itself is on, matching
  // Settings.tsx's own namesOn/roomOn gating (the room checkbox is disabled whenever
  // live_names is off).
  assertEqual(liveNamesCurrentlyOn({ "diarization.live_names": true, "diarization.live_names_room": false }), true, "room alone off doesn't flip it");
});

// --- Task 15: dashboard-only placement + active-meeting deferral --------
// NOTE on "page visibility": task 15 moved <LiveNamesNotice> from a global render in App.tsx to
// a render inside Home.tsx only (see App.tsx / Home.tsx / LiveNamesNotice.tsx). That placement is
// structural — which page mounts the component — not a runtime flag liveNamesNoticeEligible()
// checks, and this repo has no component/route test runner (no React Testing Library, no
// vitest/jest) to mount <App>/<Home> and assert the banner is absent on, say, Settings or Live.
// That half genuinely cannot be unit-tested here; it is verified instead by inspection (grep):
// LiveNamesNotice is imported/rendered in exactly one place, ui/src/pages/Home.tsx, and nowhere
// else (ui/src/App.tsx no longer imports or renders it). What *is* pure, exported logic — and is
// tested below — is the eligibility predicate the component calls once it IS mounted on Home:
// unseen + feature-state-known + not-currently-recording.
test("eligible: setup complete, unseen, not recording -> show", () => {
  assertEqual(liveNamesNoticeEligible({ setupComplete: true, seen: false, recording: false }), true, "shows on the dashboard when idle");
});

test("active-meeting deferral: eligible-but-recording -> not shown", () => {
  assertEqual(liveNamesNoticeEligible({ setupComplete: true, seen: false, recording: true }), false, "deferred while a meeting is active");
});

test("active-meeting deferral: eligible-and-not-recording -> shown", () => {
  assertEqual(liveNamesNoticeEligible({ setupComplete: true, seen: false, recording: false }), true, "shown once recording stops");
});

test("deferring while recording does not mark the notice seen (it can still show later)", () => {
  // liveNamesNoticeEligible() itself never calls markLiveNamesNoticeSeen() - deferral is just a
  // `false` return, so a caller that defers on `recording` never persists "seen". Simulate that
  // caller: check eligibility while recording (false, so no mark), then again once idle (true).
  globalThis.localStorage = fakeLocalStorage();
  const whileRecording = liveNamesNoticeEligible({ setupComplete: true, seen: hasSeenLiveNamesNotice(), recording: true });
  assertEqual(whileRecording, false, "not shown while recording");
  assertEqual(hasSeenLiveNamesNotice(), false, "still unseen - deferring must not mark it seen");
  const onceIdle = liveNamesNoticeEligible({ setupComplete: true, seen: hasSeenLiveNamesNotice(), recording: false });
  assertEqual(onceIdle, true, "shows on the next idle dashboard visit");
});

test("not eligible before setup is complete, or once already seen, regardless of recording", () => {
  assertEqual(liveNamesNoticeEligible({ setupComplete: false, seen: false, recording: false }), false, "feature state not known yet");
  assertEqual(liveNamesNoticeEligible({ setupComplete: true, seen: true, recording: false }), false, "already dismissed (one-time dismissal, unchanged)");
});

if (failures > 0) {
  console.log(`\n${failures} test(s) failed.`);
  throw new Error(`${failures} test(s) failed`);
} else {
  console.log("\nAll tests passed.");
}
