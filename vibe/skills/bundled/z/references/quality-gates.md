# Quality Gates Reference

**Gates 1, 2, 8, 9, 10, 12, 13** are universal — they apply to ALL generated prompts regardless of category. Gates 3–7 and 11 apply specifically to code translation, rewrite, and re-platform requests. Gates 14–18 apply specifically to **language migration** requests (code translation, rewrite, re-platform) and encode concurrency and type-system failure modes empirically observed in C→Rust, Python→Go, and similar migrations. All applicable gates MUST appear in the generated prompt's Validation Framework section.

**Gate 1 — End-to-End Boundary Verification**
The deliverable is NOT complete until the output artifact processes at least one real-world input and produces verifiably correct output. For daemons: must respond to a live request. For libraries: existing caller code must link and run. For compilers/transformers: must process a real input file. Unit tests that mock I/O do not satisfy this gate. State the concrete verification artifact in the prompt.

**Gate 2 — Zero-Warning Build Enforcement**
The build MUST be warning-clean before delivery. Enforce via language-appropriate flag (`RUSTFLAGS="-D warnings"`, `-Werror`, `--strict`, etc.) in CI. No warning suppressions permitted. Apply regardless of language, paradigm, or framework.

**Gate 3 — Performance Baseline Comparison**
A benchmark comparing the output to the original implementation MUST be included as a deliverable. Directional accuracy is sufficient for a first pass. The comparison must be measured and reported — assumed parity is not acceptable. Specify the benchmark tool and the key metric(s) to compare.

**Gate 4 — Named Real-World Validation Artifacts**
Specify 1–2 concrete upstream artifacts the output must process successfully (e.g., compile a real source file, serve a real protocol request, decompress a real corpus). "High test coverage" is not a substitute. Concrete artifacts expose integration failures that unit tests cannot.

**Gate 5 — API/Interface Contract Verification**
For any drop-in replacement claim (same ABI, same CLI flags, same wire protocol, same API surface): the contract MUST be verified at the boundary. Enumerate the interface elements to verify. Self-certification is not acceptable — a real caller or client must exercise the contract.

**Gate 6 — Unsafe/Low-Level Code Audit**
The count of dangerous patterns (unsafe blocks, raw pointers, unvalidated external input, shell interpolation, SQL string concatenation, eval) MUST be documented in the deliverable. Any count above 50 requires a formal review and per-site justification. Interface boundaries (FFI, IPC, subprocess, external commands) are highest risk — each requires a corresponding test.

**Gate 7 — Prompt Tier / Scope Matching**
The prompt complexity MUST match the project complexity. Use this mapping:
- Complex multi-subsystem daemon, compiler, or service with large CLI surface → Extended specification required
- Well-scoped library with bounded API surface → Medium specification acceptable; use Extended if performance or FFI completeness matters
- Simple utility or filter program → Medium specification acceptable
- Prototype or feasibility exploration → Minimal only; NOT suitable for production rewrites; add explicit production readiness caveat and plan promotion to Medium/Extended before deployment
Mismatching produces either wasted specification (over) or P0 integration gaps (under).

**Calibration note (from A/B empirical data):** Within the same tier, project complexity dominates outcome. A Medium-tier library rewrite (zlib: 68% production readiness) and a Medium-tier daemon rewrite (dnsmasq-mio: 62%) diverge by 15 points — same prompt tier, same template, different project scope. When a project sits at a tier boundary, choose Extended over Medium rather than under-specifying: the cost of an extra specification section is lower than the cost of a P0 wiring gap.

**Gate 8 — Integration Sign-Off Checklist Decoupled from Unit Test Pass Rate**
The prompt MUST include an explicit integration sign-off checklist that is separate from unit test pass rate. Feature completion is not integration verification. The checklist must include: live smoke test result, API contract verification result, performance baseline result, unsafe audit result. All four must be checked before the deliverable is accepted.

**Gate 9 — Integration Wiring Verification**
Every created component MUST be reachable from the application's entry point through the actual execution path. For each new component, the deliverable must verify: (1) a caller or registry entry exists that references it, (2) that caller is itself reachable from bootstrap/main/router, (3) the component is exercised by at least one integration or E2E test that traverses the real call chain. Components that compile and pass unit tests but are never invoked from the execution path do not count as delivered. This is the dominant failure mode in code generation — created, tested in isolation, never wired.

**Gate 10 — Test Execution Binding**
Test specifications MUST have a runnable execution path. At minimum: a CI job definition, orchestration script, or documented single-command invocation that deploys the system under test and runs the specs end-to-end. Specs without execution binding are documentation, not validation. If the test requires infrastructure (databases, message queues, external services), the execution path must include that infrastructure setup.

**Gate 11 — Consistency Model Delta Coverage (Re-Platforms)**
When a re-platform changes the consistency model (e.g., SQL transactions → eventual consistency, monolith → event choreography, synchronous → async), the deliverable MUST enumerate lost atomicity or ordering guarantees and either: (a) accept each with documented rationale, or (b) provide compensating mechanism tests (saga rollback, idempotency, dead-letter handling). Dependency substitutions (e.g., replacing one TLS library, database driver, or message broker with another) MUST enumerate known behavioral differences at the boundary.

**Gate 12 — Config Propagation Tracing (Data Layer)**
Every field on a config/options/shared-state struct MUST have a verified **write-site** AND a **read-site** reachable from the application entry point. The deliverable MUST include a propagation audit naming both sites per field. Fields intentionally without a read-site MUST be annotated `// UNREAD: reserved` and excluded from the public API.

**Gate 13 — Registration-Invocation Pairing**
Every registration API (callbacks, hooks, plugins, event listeners, middleware) MUST have a corresponding **invocation test** that: (1) registers a handler, (2) triggers the event through the normal execution path, (3) asserts the handler was invoked with correct arguments. For FFI boundaries the test MUST exercise the full foreign-language → native → foreign-language round-trip. Boolean flags tracking registration state MUST have at least one read-site that conditionally invokes the registered handler.

**Gate 14 — Runtime Ownership Model (Async Migrations)**
When migrating from sync to async, the deliverable MUST specify a **single runtime ownership model**: (1) exactly one component creates the runtime, (2) all others receive a handle — never create their own, (3) `block_on` / equivalent is called at exactly one call-stack level. Nested runtime creation MUST be a build error, not a runtime panic. The prompt MUST include a text runtime topology naming the owner, the handle-passing path, and sync/async boundary crossings.

**Gate 15 — Synchronization Primitive Matching**
Every mutex, lock, or semaphore MUST be annotated with its **execution context**: sync-only, async-only, or mixed. The deliverable MUST enforce context matching via lint rule, review checklist, or wrapper types. For shared state accessed by both sync and async paths, document the bridging strategy explicitly. Mandatory for any migration where the target language has distinct sync and async synchronization APIs.

**Gate 16 — Nullability Mapping**
Every sentinel value in the source language (`0`, `-1`, `NULL`, `""` meaning "unset/default") MUST map to the target language's optional type (`Option<T>`, `T?`, `Optional<T>`) unless a written justification exists (e.g., FFI ABI requires raw integer). The deliverable MUST include a sentinel audit for all config structs, API parameters, and return types. Sentinel checks (`if port == 0`) MUST be flagged as migration debt if `Option<T>` is viable.

**Gate 17 — Concurrency Access Analysis (New Concurrency)**
When the target introduces concurrency absent from the source, every shared data structure MUST include a **concurrency access analysis**: (1) enumerate independent access paths, (2) justify lock granularity — single locks protecting independent partitions require documented trade-off rationale, (3) identify contention hotspots under expected load. Deliverables that wrap every shared structure in a single coarse lock without analysis fail this gate.

**Gate 18 — Lossless Type Mapping**
Every narrowing numeric cast (e.g., `i64 → i32`, `size_t → int`) MUST use a checked or saturating conversion — not a bare cast. The target language's truncation lint MUST be set to deny/error (`#[deny(clippy::cast_possible_truncation)]`, `-Wconversion -Werror`). Every intentional narrowing cast that survives the lint MUST carry an inline `// TRUNCATION:` justification comment.
