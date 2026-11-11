// Plain-logic test for liveSpeakerTooltip (Live.tsx's SpeakerLabel tooltip selection).
//
// This repo has no UI component-test runner wired up (no vitest/jest in ui/package.json — see
// liveNamesOnboarding.test.ts for the same note). This file follows that exact convention: plain
// TypeScript, no React, no DOM, just the pure string function. It type-checks as part of `ui`'s
// normal `tsc -b` (nothing here needs excluding from the build), but nothing imports it, so it is
// not bundled into dist/.
//
// To actually execute it, compile it standalone with the project's own `tsc` (no new dependency)
// and run the output with Node, e.g. from `ui/`:
//   npx tsc --module commonjs --target es2020 --lib es2020,dom \
//     --outDir ../.tmp/t2 src/liveSpeakerTooltip.ts src/liveSpeakerTooltip.test.ts
//   node ../.tmp/t2/liveSpeakerTooltip.test.js
import { liveSpeakerTooltip } from "./liveSpeakerTooltip";

function assertEqual(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg}: expected ${e}, got ${a}`);
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

// The real EN templates, matching i18n.tsx's liveNameGuess/liveNameMaybe after this task's fix.
const GUESS_TEMPLATE = "⁨{name}⁩. Final names are set after the meeting.";
const MAYBE_TEMPLATE = "Might be ⁨{name}⁩. Final names are set after the meeting.";
// The literal old, pre-fix string (no {name} placeholder at all) — the bug this task closes.
const OLD_GENERIC_GUESS = "Live guess. Final names are set after the meeting.";

test("maybe=false (named/confident) with a long name -> full interpolated string, name present", () => {
  const tip = liveSpeakerTooltip(false, "Mohammed Abdul Rahman Al-Khalidi", MAYBE_TEMPLATE, GUESS_TEMPLATE);
  assertEqual(tip, "⁨Mohammed Abdul Rahman Al-Khalidi⁩. Final names are set after the meeting.", "long name interpolated into the guess template");
});

test("maybe=false with a short name that would never be truncated -> same function, same shape of result (no truncation-conditional branch)", () => {
  const tip = liveSpeakerTooltip(false, "Sam", MAYBE_TEMPLATE, GUESS_TEMPLATE);
  assertEqual(tip, "⁨Sam⁩. Final names are set after the meeting.", "short name interpolated identically, proving there is no truncation check");
});

test("maybe=true -> existing liveNameMaybe-style interpolation, unchanged from before", () => {
  const tip = liveSpeakerTooltip(true, "Sara", MAYBE_TEMPLATE, GUESS_TEMPLATE);
  assertEqual(tip, "Might be ⁨Sara⁩. Final names are set after the meeting.", "maybe branch untouched");
});

test("an Arabic name string passes through the interpolation correctly", () => {
  const AR_GUESS_TEMPLATE = "⁨{name}⁩. تُحدَّد الأسماء النهائية بعد الاجتماع.";
  const tip = liveSpeakerTooltip(false, "سارة", MAYBE_TEMPLATE, AR_GUESS_TEMPLATE);
  assertEqual(tip, "⁨سارة⁩. تُحدَّد الأسماء النهائية بعد الاجتماع.", "Arabic name interpolated into the Arabic guess template");
  // Note: this only proves the string-replace logic places the Arabic text correctly inside the
  // template; it says nothing about how a browser would actually render that string visually
  // (bidi reordering, tooltip rendering direction, etc.) — that is out of this test's scope.
});

test("the old generic English string is never returned for the confident case (the bug is fixed, not just restructured)", () => {
  const tip = liveSpeakerTooltip(false, "Khalid", MAYBE_TEMPLATE, GUESS_TEMPLATE);
  assertEqual(tip === OLD_GENERIC_GUESS, false, "must not equal the old name-less generic string");
  assertTrueContainsName(tip, "Khalid");
});

function assertTrueContainsName(tip: string, name: string) {
  if (!tip.includes(name)) throw new Error(`tooltip "${tip}" does not contain the name "${name}"`);
}

if (failures > 0) {
  console.log(`\n${failures} test(s) failed.`);
  throw new Error(`${failures} test(s) failed`);
} else {
  console.log("\nAll tests passed.");
}
