# 0001 - Wrap existing screens in mobile chrome (SPEC D4)

Date: 2026-10-03 · Status: accepted

## Context
SPEC §5.2/§8 sketches a new Svelte+Vite app with a full `/api/v1`. The repo instead serves
Anki's own compiled frontend (reviewer, editor, deck options, graphs) through server-rendered
screens plus a small TypeScript shell, with 533 tests covering that behavior.

## Decision
Keep Anki's reviewer/editor/SvelteKit pages. Add phone-first chrome around them (bottom tabs,
fixed answer bar, safe areas, drill-down browser) in `shell_src/` + `ankiweb/shell/`. New
capabilities that are not Anki screens (session, health, decks summary) go behind a thin,
versioned `/api/v1`. No full custom reviewer in v1.

## Consequences
+ Smallest path to a usable phone UI; scheduler/render fidelity stays Anki's.
+ Existing tests remain valid.
- The mobile UX is bounded by what Anki's pages allow; deeper redesign needs a new ADR.
- `/api/v1` stays small until a screen is actually replaced.
