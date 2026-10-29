# TSP rustdx v2.0.4 adaptations

Upstream: `https://github.com/jelven007/rustdx` at
`c4c0273456874898aed50447fd900521d9ffd3b8`.

All 101 production Rust source files are retained. No CLI, runner, history,
runtime data or additional business engine is imported.

- `Cargo.toml`: standalone workspace values and exact package version; retain
  dependencies including sha2 and v2.0.4 Windows lock features; omit missing
  examples, benches and integration harnesses. Rust 1.99 style-only parser
  lints are allowed at manifest scope so audited protocol source remains
  unchanged. The required bar-range fixture is included.
- `src/tcp/ip/probe.rs`, `daily.rs`, `ip.rs`: immutable per-pool probe mode.
  The default remains 044e security count. Financial pools use the checked
  0004 HeartbeatAck, retain a null security count and require all three rounds.
  Old serialized rounds default to the security-count mode.
- `src/tcp/ip/coordinator.rs`, `daily.rs`: worker count and whole-pool timeout
  use the configured local/shared quota instead of assuming eight probes.
  No partial snapshot is published after cancellation, timeout or quota failure.
- `coordinator.rs`, `probe.rs`, `daily.rs`, `ip.rs`, `governance.rs`: task
  probe labels share one group permit and one physical-endpoint permit, in
  addition to the process probe budget. Existing governor file locks enforce
  the same dimensions across cooperating processes. Scoped pools use one
  worker, including their whole-pool timeout calculation. Explicit pool stop
  cancels active probes and wakes the daily thread; it cannot revive a pool.
- `src/tcp/heartbeat.rs`: derive Deserialize for the actual recorded ack.
- `src/tcp/mod.rs`: narrowly allow fetch_update's deprecation on Rust 1.99
  while preserving the upstream minimum Rust 1.89 API.

Validation: core unit and doc tests with external integration tests disabled;
local fragmented-frame server tests; bridge mixed quote/report traffic with
three groups of eight business permits plus one probe each; independent live read-only validation.
The v2.0.4 Windows cache lock source and required `windows-sys` features are
retained unchanged and cross-checked with the Windows GNU Rust target.

`UPSTREAM.json` records original and adapted hashes. `SOURCE-SHA256SUMS`
records the compiled core and test assets. Refresh identities explicitly with
`python scripts/vendor_rustdx.py --refresh --upstream /path/to/rustdx`;
normal checks and builds are offline and reject unregistered source drift.
The installed bridge exposes upstream revision, core digest and patch digest.
