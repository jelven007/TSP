# Shared picker position regression

Status: FIXED. Scope: engineering candidate, not deployed.

The full frontend suite failed 25 tests across nine files after the shared picker
started following ancestor scrolling. The added offscreen check returned before
setting the initial popup position when the trigger rectangle was zero-sized.
JSDOM supplies that rectangle by default, so date/time popup content was absent.
The failure was reproducible in the existing date tests; it was not an animation
timeout or a changed business date expectation.

The fix removes the added offscreen early return. Position updates still follow
ancestor scroll and resize; outside click and Escape retain their existing behavior.
It introduces no environment detection and changes no test expectations.

Verification on 2026-11-19:

- Initial complete run: 25 failed, 780 passed.
- Fix: existing DatePicker and TimePicker suites, 10 passed.
- Temporarily reinsert early return: DatePicker, 2 failed and 1 passed.
- Restore fix: full frontend, 805 passed in 14.46 seconds.
- Lint: 0 errors, 2 existing Hook warnings in unchanged files.
- TypeScript/production build passed; existing large-chunk warning retained.
- Five browser business journeys at desktop and narrow widths: 10 passed in 45.4 seconds.

The browser suite also covers the separately identified same-route mobile drawer
issue: clicking the active route must close the drawer even without a pathname
change. Explicit NavLink click handlers implement that behavior.

Pattern inspection: the shared hook serves DatePicker and TimePicker and the
nine failing consumers. Watchlist has a separate viewport-aware search popup
whose real-browser visibility behavior is covered by the repeated watchlist journey;
its offscreen behavior is not part of this regression.
