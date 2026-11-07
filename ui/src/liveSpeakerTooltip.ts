// Tooltip-text selection for a live, provisional speaker label (Live.tsx's SpeakerLabel).
// Kept as a plain, UI-framework-free function so the string logic can be unit-tested without a
// component renderer (this repo has no UI component-test runner wired up — see
// liveNamesOnboarding.ts/.test.ts for the same convention).
//
// Task 46 (report 10, Low finding): the "named" (confident) state's tooltip used to be a fixed,
// generic string that never included the speaker's name, so a long name that got CSS-ellipsis-
// truncated (styles.css's .guess/.nm.named rules) had no way to be recovered on hover. The
// "maybe" (uncertain) state already interpolated the name unconditionally, truncated or not.
// The fix makes "named" mirror that exact pattern — no truncation detection, no new state: both
// branches just interpolate `{name}` into their own template.

/** Selects and fills in the tooltip template for a provisional speaker label.
 *  `maybeTemplate`/`guessTemplate` are the raw i18n strings (liveNameMaybe/liveNameGuess), each
 *  containing a literal `{name}` placeholder; both are filled unconditionally, regardless of
 *  whether the visible name is actually truncated. */
export function liveSpeakerTooltip(maybe: boolean, name: string, maybeTemplate: string, guessTemplate: string): string {
  return (maybe ? maybeTemplate : guessTemplate).replace("{name}", name);
}
