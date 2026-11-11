// Logic for disclosing "live speaker names" (on by default) during onboarding, and for the
// one-time notice shown to users who completed onboarding before this disclosure existed.
// Kept as plain, UI-framework-free functions so they can be unit-tested without a component
// renderer (this repo has no UI component-test runner wired up yet).

/** Order of Welcome.tsx's onboarding steps. "liveNames" sits right after "permissions" and
 * before the capability/setup steps (hardware/models/notifications), per the design report
 * (reports/13-designer-onboarding.md §3): it's a privacy-adjacent disclosure, so it stays next
 * to the other privacy-adjacent step instead of being scattered among setup steps. */
export const WELCOME_STEP_ORDER = ["permissions", "liveNames", "hardware", "models", "notifications"] as const;
export type WelcomeStepId = (typeof WELCOME_STEP_ORDER)[number];

/** localStorage key for the one-time "live speaker names" disclosure notice shown to users who
 * already had `setup_complete: true` before this Welcome.tsx step existed (or who skipped it).
 * This mirrors the existing convention for lightweight, per-browser, non-account UI flags
 * (i18n.tsx's `modwen.lang`/`modwen.theme`, UpdateBanner.tsx's `modwen.update.later`) rather than
 * a server-side settings field: it is not a preference, not biometric consent, and not
 * enrollment permission — only a "have they seen this banner" flag for this browser profile.
 * Acknowledging it must never be read by any consent/enrollment code path. */
const NOTICE_SEEN_KEY = "modwen.liveNamesNoticeSeen";

export function hasSeenLiveNamesNotice(): boolean {
  try {
    return localStorage.getItem(NOTICE_SEEN_KEY) === "1";
  } catch {
    // Private mode / storage unavailable: treat as unseen. Worst case the notice reappears,
    // which is safe (it is informational, not a gate).
    return false;
  }
}

export function markLiveNamesNoticeSeen(): void {
  try {
    localStorage.setItem(NOTICE_SEEN_KEY, "1");
  } catch {
    /* private mode: the acknowledgment just isn't remembered */
  }
}

/** Whether the one-time returning-user notice should render right now, given it is unseen and
 * the feature state is known (`setupComplete`). Task 15: the notice now lives on the dashboard
 * only (LiveNamesNotice is rendered from Home.tsx, not globally from App.tsx — see App.tsx/
 * Home.tsx), so "which page" is enforced structurally by where the component is mounted, not by
 * a flag this function checks. What this function *does* own is the other half of task 15: if the
 * notice becomes eligible while a meeting is active, it must defer rather than show (or, worse,
 * be marked seen) — `recording` mirrors the existing `ctx.recorder.recording` convention already
 * used for the same "is a meeting active" question elsewhere (UpdateBanner.tsx's `busyNow`,
 * Home.tsx's own record button, Sidebar.tsx, Start.tsx). Deferring here never calls
 * markLiveNamesNoticeSeen() — the notice simply renders null and re-evaluates next time the
 * component mounts/updates, so it shows the next time the user is on the dashboard and not
 * recording, exactly as required. */
export function liveNamesNoticeEligible(opts: { setupComplete: boolean; seen: boolean; recording: boolean }): boolean {
  return opts.setupComplete && !opts.seen && !opts.recording;
}

/** Whether live speaker names is currently enabled for this user, reading the same settings
 * keys and the same "!== false" (default-true) semantics Settings.tsx already uses
 * (Settings.tsx's `namesOn`/`roomOn`). The master switch is `diarization.live_names`;
 * `diarization.live_names_room` only matters once that is on (Settings.tsx disables the room
 * checkbox whenever `live_names` is off), so "currently off for you" tracks `live_names` alone. */
export function liveNamesCurrentlyOn(settings: Record<string, unknown> | null | undefined): boolean {
  if (!settings) return true; // unknown yet: assume the default (on) rather than warn wrongly
  return settings["diarization.live_names"] !== false;
}
