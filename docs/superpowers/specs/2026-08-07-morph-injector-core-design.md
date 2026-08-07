# Design: C++ Daz Studio SDK Injector Core (JIT Binary Morph Loader — Subsystem B)

**Status:** Draft — architecture proposal. Real-library formula-operator-vocabulary scan complete (§3.1); scene-attachment strategy resolved against the actual Daz Studio 4.5 win32 SDK headers (§4) — `DzFormula` for algebra formulas, `DzERCLink` (keyed) for spline formulas. Remaining items are implementation-time verification, not open design questions.
**Parent spec:** `docs/superpowers/specs/2026-08-06-jit-morph-loader-parent-spec.md` — JIT Binary Morph Loader & Semantic Asset Manager (Option 3 Architecture), v1.0
**Scope:** This document covers only Subsystem B (the C++ native plugin core that injects morphs and their ERC formula dependencies into a live Daz Studio scene). Subsystem A (Python offline ingest) is done — see `2026-08-06-morph-ingest-transpiler-design.md`. Subsystems C (scene lifecycle / manifest persistence) and D (Qt search UI / ChromaDB bridge) are out of scope here and get their own specs once Subsystem B's core injection API exists to build against.

## 1. Problem & Goal

Given a `DzNode` (a scene figure) and a morph GUID, Subsystem B must:
1. Look up the morph's metadata in `morph_index.db` (built by Subsystem A)
2. Read its `.tmb` binary vertex-delta file
3. Instantiate a live `DzMorph` + `DzFloatProperty` on the target node
4. Recursively resolve and inject any morphs it formula-depends on, evaluating the raw RPN operation stack against live scene properties (not just other morphs — bone rotations and other property types are legal operands)
5. Do all of this within the parent spec's latency targets (§1.3: <100ms per morph including its ERC dependency chain)

Unlike Subsystem A, this is a from-scratch C++/Qt toolchain with no code shared with the Python side — it consumes Subsystem A's output files as a stable on-disk contract (`morph_index.db` schema, `.tmb` byte layout) and nothing more.

## 2. Key Architectural Gap: The Parent Spec's ERC Model Doesn't Match What Subsystem A Built

The parent spec's §4.2 sketch (`resolveAndInjectDependencies` / `applyErcLink`) assumes a flattened dependency table:

```sql
erc_relationships(link_id, parent_morph_id, child_morph_id, stage, scalar_multiplier)
```

...and a single-scalar attach:

```cpp
stage->setScalar(scalarMultiplier);
childProperty->addFormula(formula);
```

This table was never built. Subsystem A's actual schema (`src/managers/morph_index_manager.py`) instead has:

```sql
morph_dependencies(link_id, dependent_morph_id, referenced_morph_id)  -- edge only, no stage/scalar
```

with the real ERC logic captured verbatim on each `morphs` row as `formulas_json` — a JSON array of formula objects, each an RPN operation stack:

```json
[{
  "output": "GnHdCloak_G3_23369:#pJCMCloakBend_m90?value",
  "operations": [
    { "op": "push", "url": "Cloak:/data/.../GnHdCloak_G3_23369.dsf#Cloak?rotation/x" },
    { "op": "push", "val": -0.01111111 },
    { "op": "mult" }
  ]
}]
```

`morph_dependencies` is a **best-effort** edge table: it only contains an edge when a formula operand resolved to another *indexed morph* (`src/dsf_parser.py`'s `extract_referenced_guids` + `morph_index_manager.py`'s `rebuild_dependencies`). Two resolution forms exist:
- **Path-form** operand (`"Label:/data/.../Target.dsf#Node?property"`) → matched against `morphs.guid`.
- **Pathless form** (`"Label:#PropertyName?value"`, the overwhelmingly more common real-world shape) → emitted as a synthetic `"name:PropertyName"` marker, resolved by `(target_figure, name)` — **scoped to the dependent morph's own target figure**, because morph names like `pJCM*` repeat across different base figures and a global name lookup would cross-wire unrelated figures.

Operands that don't resolve to any indexed morph (bone rotations, etc.) get **no** `morph_dependencies` row — they only exist in the dependent morph's own `formulas_json`, unresolved, waiting for Subsystem B to evaluate them live.

**Consequence for Subsystem B:** there is no scalar to plug into a native "ERC delta-add with multiplier" primitive. Subsystem B needs an actual RPN evaluator that walks the operation stack and, for each `push url` operand, resolves it against either (a) another injected morph's live property value (recursing through `morph_dependencies` first) or (b) a non-morph scene property (bone rotation, etc.) read directly off the `DzNode` graph. `applyErcLink`'s single-`setScalar` sketch from the parent spec cannot represent this and should not be implemented as written.

## 3. Component Architecture

Split into two link boundaries: **SDK-independent** components with no Daz Studio SDK or Qt dependency (unit-testable in ordinary CI, no Daz Studio install required), and **SDK-dependent** components that only build and run inside a Daz Studio plugin context (verified manually, per §6).

```
                    SDK-INDEPENDENT (injector_core)              SDK-DEPENDENT (daz_plugin)
                    ─────────────────────────────────            ──────────────────────────
morph_index.db ──▶  MorphIndexReader                                     │
  .tmb file    ──▶  TmbReader                                            │
                          │                                              │
                          ▼                                              ▼
                    FormulaCompiler   ────IR (algebra/spline)──▶  FormulaControllerBuilder
                    (formulas_json                                (builds DzFormula or
                     -> IR, no SDK)                                 DzERCLink, resolves
                                                                     operands via)
                                                                        │
                                                                        ▼
                                                                 PropertySourceAdapter
                                                                 (real DzNode/DzFloatProperty
                                                                  + bone-rotation reads)
                                                                        │
                                                                        ▼
                                                                 InjectorCore
                                                                 (ModernMorphInjector)
                                                                        │
                                                                        ▼
                                                                 PluginMain (SDK registration)
```

### 3.1 SDK-independent layer (`injector_core` target)

- **`TmbReader`** — parses `.tmb` files. Must match `src/tmb_format.py`'s exact byte layout, confirmed against the current implementation:
  ```
  HEADER (16 bytes): magic "TMB1" (4) | vertex_count int32 (4, SIGNED) |
                      delta_count uint32 (4) | reserved (4, zero)
  DATA: delta_count × 16 bytes { vertex_index uint32, dx/dy/dz float32 }
  ```
  `vertex_count` is **signed** — real `.dsf` files use `-1` as a documented DSON sentinel for "unspecified/same as base mesh" (this bit Subsystem A once: an earlier `uint32_t` packing silently dropped every affected file — see `2026-08-06-morph-ingest-transpiler-design.md` §5). Given the <100ms injection budget, `TmbReader` memory-maps the file and exposes the delta array as a view over the mapped region rather than deserializing into a `std::vector` — zero-copy read, no per-morph heap allocation proportional to delta count.

- **`MorphIndexReader`** — read-only `sqlite3` C-API wrapper over `morph_index.db`. The schema is Python-authoritative (`src/managers/morph_index_manager.py`'s `_SCHEMA`); this component never writes or migrates, only opens read-only and queries. Required operations:
  - `find_by_guid(guid) -> MorphRecord` (all `morphs` columns, including raw `formulas_json`)
  - `find_by_name(target_figure, name) -> MorphRecord` — mirrors the same `(target_figure, name)` scoping `rebuild_dependencies()` uses in Python, needed because at injection time we may need to resolve a `"name:X"` formula operand against a morph that Subsystem A's own dependency rebuild already matched (fast path: read the precomputed `morph_dependencies` edge) or re-derive the same lookup live (e.g. if `morph_dependencies` wasn't rebuilt since a partial re-index — `MorphIndexReader` should not assume the edge table is authoritative, only a cache of a derivable relationship).
  - `dependencies_of(dependent_morph_id) -> list<morph_id>` — reads precomputed `morph_dependencies` edges.

- **`FormulaCompiler`** — parses a morph's `formulas_json` into an SDK-independent intermediate representation (IR), classifying each formula as either an **algebra formula** (a chain of `push`/`mult`/`neg`/etc. ops with no spline) or a **keyed-spline formula** (`push`×N control points + a terminal `spline_tcb`/`spline_linear`). This IR is what `daz_plugin`'s `FormulaControllerBuilder` (§3.2) mechanically translates into native SDK controller objects — `FormulaCompiler` itself never evaluates anything and touches no Daz SDK types, so it's fully unit-testable on fixture JSON without a Daz Studio install.
  ```cpp
  struct AlgebraOp { enum Kind { Push, Mult, Neg /* ... */ } kind; };
  struct PushConst { double value; };
  struct PushOperand { std::string operand; };   // resolved later, by PropertySourceAdapter

  struct AlgebraFormula {
      std::vector<std::variant<AlgebraOp, PushConst, PushOperand>> ops;
  };
  struct SplineKey { double key, value, t, c, b; bool has_tcb; };  // has_tcb=false for spline_linear's (key,value) pairs
  struct SplineFormula {
      std::string driving_operand;      // the single push:url before the key pushes
      std::vector<SplineKey> keys;
      bool tcb_interpolation;           // spline_tcb vs spline_linear
  };

  std::variant<AlgebraFormula, SplineFormula> compileFormula(const nlohmann::json& formula);
  ```

  **Real-library operator vocabulary scan — done, and it directly determined this design.** Subsystem A's own fixture sample (2 `.dsf` files) only exercised `push` and `mult`, too small to design against. This design pass ran a full scan over the real, already-ingested `morph_index.db` (188,906 morphs, 23,596 with `formulas_json`, 0 malformed) and found the **complete, closed operator vocabulary is just five operators** — no `add`/`sub`/`div`/`clamp` appear anywhere in the real library:

  | op | occurrences | notes |
  |---|---|---|
  | `push` | 1,928,000 | `url` form (968,725) or `val` form (959,275) |
  | `mult` | 956,392 | binary: pops 2, pushes 1 |
  | `spline_tcb` | 732 | see below |
  | `spline_linear` | 83 | see below |
  | `neg` | 26 | unary: pops 1, pushes 1 |

  A second scan confirmed the **structural shape** of every spline formula (815 total): each is *exclusively* a run of `push` ops followed by one terminal `spline_tcb`/`spline_linear` — never mixed with `mult`/`neg`, and the spline op is always last. The push run itself has a fixed shape: `push(driving-property-url)`, then `push(control-point)` × N, then `push(N)` (the key count), then the spline op. Control-point pushes are `[5]`-element arrays for `spline_tcb` (`[key, value, t, c, b]` — Kochanek-Bartels tension/continuity/bias) and `[2]`-element arrays for `spline_linear` (`[key, value]`, no tangent params needed). The observed push-count histogram (`{4: 517, 5: 218, 6: 57, 7: 14, ...}`) is exactly `2 + num_keys` (driving-url push + count push + one push per key), consistent across all 815 instances with zero exceptions — this is a closed, mechanical shape, not a loose pattern.

  **This shape is not a coincidence — it's a direct serialization of `DzERCLink`'s native keyed-ERC API** (see §3.2/§4): `DzERCLink::addKeyValue(key, value, t, c, b)` takes exactly a TCB tuple, and `ERCKeyInterpolation::{TCB_INTERP, LINEAR_INTERP}` are exactly `spline_tcb`/`spline_linear`. `FormulaCompiler` doesn't need to interpret spline math at all — it only needs to parse the fixed push-run shape into a `SplineFormula` IR that `FormulaControllerBuilder` feeds straight into `addKeyValue()` calls, letting the SDK's own spline evaluator do the actual math. Likewise, `mult`/`neg`/`push` map directly onto `DzFormula::Operation`'s `OpMultiply`/`OpNegate`/`OpPushInput`/`OpPushConstant` — `FormulaCompiler`'s `AlgebraFormula` IR is a 1:1 restatement of what `DzFormula::addOp`/`addOpPush` already expects.

  This scan is complete and both tables above should be treated as closed against this library snapshot — but re-run it (one-off query, no new code needed) if `morph_index.db` is rebuilt against a substantially different or newer content library, since a closed vocabulary from one snapshot isn't a guarantee against all possible DAZ content.

### 3.2 SDK-dependent layer (`daz_plugin` target)

- **`PropertySourceAdapter`** — the only component that touches real `DzNode`/`DzFloatProperty` objects to resolve an operand string into a live `DzNumericProperty*`. Operand strings come in two forms (§2): path-form (`/data/.../Target.dsf#Node?property`) resolved via `MorphIndexReader::find_by_guid` → recursively ensure that morph is injected (via `InjectorCore`) → `getValueChannel()` on its `DzMorph`; and pathless `name:PropertyName` form, which for non-morph operands (bone rotations etc.) resolves directly against the live scene graph via `DzElement::findProperty(name)` (`dzelement.h`) on the appropriate node — the exact DSON-property-name → SDK-internal-property-name mapping (e.g. whether `rotation/x` maps directly to a `findProperty` lookup or needs per-axis accessor methods) is a mechanical detail to confirm during implementation, not an open architectural question.

- **`FormulaControllerBuilder`** — translates `FormulaCompiler`'s IR (§3.1) into native SDK controller objects and attaches them to the morph's `DzFloatProperty` via `DzNumericProperty::insertController()` (`dznumericproperty.h`). Two straightforward, mechanical cases, both confirmed against the actual `daz-studio-45-sdk-win32` headers (`dzformula.h`, `dzerclink.h`, `dznumericcontroller.h`):
  - **`AlgebraFormula` → `DzFormula` + `DzFormulaController`.** `DzFormula` (`dzformula.h`) is itself a generic RPN stack machine: `addOp(DzFormula::Operation)` and two `addOpPush` overloads (`addOpPush(DzNumericProperty*)` for a live operand, `addOpPush(float)` for a literal constant) build the op stack directly — `OpMultiply`/`OpNegate` map 1:1 onto the confirmed `mult`/`neg` vocabulary, and `OpAdd`/`OpSubtract`/`OpDivide`/etc. are already defined in the enum if the operator vocabulary ever expands beyond the current closed set (§3.1). Built `DzFormula` objects are added to a `DzFormulaController` via `addFormula()`, and the controller is attached to the slave property with `insertController()`.
  - **`SplineFormula` → `DzERCLink` (keyed).** `DzERCLink` (`dzerclink.h`) has `ERCType::ERCKeyed` and `ERCKeyInterpolation::{TCB_INTERP, LINEAR_INTERP}` — literally the native representation of `spline_tcb`/`spline_linear`. `addKeyValue(key, value, t, c, b)` matches `SplineFormula`'s `SplineKey{key, value, t, c, b}` tuple directly (the 2-argument `addKeyValue(key, value)` overload covers `spline_linear`'s tangent-free pairs). `setProperty()` binds the link's driving/control property (resolved via `PropertySourceAdapter` from `driving_operand`), and the built link is attached the same way, via `insertController()` on the slave property.

  Because both native primitives (`DzFormula`, `DzERCLink`) are themselves `DzNumericController` subclasses hooked into the property's own `applyControllers()` path, **Daz Studio's existing dependency/dirty-propagation system re-evaluates them automatically** on every relevant property change — Subsystem B never needs its own re-evaluation loop or cache invalidation logic. This was the original parent spec's intended benefit of a "native operator chain" approach (§4, historical); it turns out to be exactly what the SDK provides, once the real op vocabulary is known.

- **`InjectorCore`** (the parent spec's `ModernMorphInjector`) — orchestrates the full sequence from parent spec §5.1, revised against the real dependency model and the SDK APIs actually available:
  1. Duplicate-modifier check: `targetNode->getObject()->findModifier(name)`.
  2. `MorphIndexReader::find_by_guid` for metadata.
  3. `TmbReader` mmap + read.
  4. Create `DzMorph`, bulk-load deltas via `DzMorphDeltas::addDeltas(indexes, deltas)` (`dzmorphdeltas.h` — a batch API, not the parent spec's original per-delta loop), `addModifier`. **No separate `DzFloatProperty` creation step is needed** — `DzMorph`'s constructor already creates its own value-channel property internally (`createProperties()`, private) and exposes it via `getValueChannel()` (`dzmorph.h`); this simplifies the parent spec §4.2 sketch, which assumed a manual `createProperty` step.
  5. `MorphIndexReader::dependencies_of` → for each dependency, recursively `InjectorCore::injectMorph` first (so referenced morphs exist and have a live property to reference before this morph's formula is compiled).
  6. If `formulas_json` is non-null: `FormulaCompiler::compileFormula` → `FormulaControllerBuilder` attaches the resulting native controller to `getValueChannel()`.

- **`PluginMain`** — Daz SDK plugin registration boilerplate (`DZ_PLUGIN`/`DZ_IMPLEMENT_CLASS` macros per SDK convention); no logic beyond registering `InjectorCore`'s entry point.

## 4. Resolved: How an Evaluated Formula Attaches to the Scene Graph

The parent spec's §4.2 sketch attaches ERC via a single native primitive (`DzERCDeltaAdd`, `stage->setScalar(...)`) — a shape that only fits a flattened parent→child scalar link, which is not what real formulas are. This design pass originally flagged two candidate strategies (native operator chaining vs. a custom black-box operator) as unresolvable without the actual SDK headers. Those headers are now available at `y:/working/BlueMoonFoundry/daz-studio-sdks/daz-studio-45-sdk-win32` (Studio 4.5 win32, used as the investigation baseline per direction — Studio 6 and macOS variants are expected to expose the same classes, to be confirmed when `daz_plugin`'s CMake target is actually built against each), and inspecting them resolves the question decisively in favor of **Strategy A (native operator chaining) — no custom black-box operator is needed**:

- `DzFormula` (`dzformula.h`) is *already* a generic RPN stack machine in the SDK — `addOp(Operation)` / `addOpPush(DzNumericProperty*)` / `addOpPush(float)` — with an `Operation` enum that already includes `OpMultiply`, `OpNegate`, `OpAdd`, `OpSubtract`, `OpDivide`, and more. This isn't a coincidence: it's presumably how Daz Studio's own asset loader represents native `.duf`/`.dsf` formulas internally, which is exactly the same problem Subsystem B has.
- `DzERCLink` (`dzerclink.h`) already supports keyed spline ERC natively: `ERCType::ERCKeyed` + `ERCKeyInterpolation::{TCB_INTERP, LINEAR_INTERP}`, with `addKeyValue(key, value, t, c, b)` matching the real library's confirmed `spline_tcb` control-point shape exactly (§3.1), and the 2-argument `addKeyValue(key, value)` overload matching `spline_linear`.
- Both are `DzNumericController` subclasses attached to a property via `DzNumericProperty::insertController()` (`dznumericproperty.h`), so both plug into Daz Studio's existing property dependency/recompute system rather than needing bespoke dirty-tracking.

This means `FormulaCompiler` (§3.1) and `FormulaControllerBuilder` (§3.2) are not a guess — they're a direct mechanical mapping onto SDK classes that were built for exactly this purpose, confirmed by reading `dzformula.h`, `dzerclink.h`, `dznumericcontroller.h`, `dznumericproperty.h`, `dzmorph.h`, and `dzmorphdeltas.h` from the real SDK. **Remaining implementation-time (not architectural) unknowns**, to be resolved with actual code + a running Daz Studio instance rather than more header-reading:
1. The exact DSON property-name → SDK-internal-property-name mapping for non-morph operands (e.g. `rotation/x` → whatever `DzElement::findProperty()` actually expects) — a lookup-table/mapping detail, not a strategy choice.
2. Whether Studio 6's headers differ from Studio 4.5's for these specific classes (expected not to, but unconfirmed — check when `daz_plugin` is first built against both SDK variants).
3. `DzERCLink`'s exact TCB spline math semantics vs. the DSON spec's, to confirm `addKeyValue`'s `t`/`c`/`b` parameters are interpreted identically to what generated the source `.dsf` files (an evaluation-correctness unit test, not a design question).

## 5. CMake / Build Layout

A new top-level `cpp/` tree, deliberately separate from the Python `src/`/`tests/` layout — unlike Subsystem A (which folded into the existing Python project structure because it shared conventions and tooling with the rest of the codebase), Subsystem B shares no buildable artifacts with the Python side and is a genuinely different toolchain. This matches the parent spec §6's original structure better than Subsystem A's decision to deviate from it.

```
cpp/
├── CMakeLists.txt              # top-level: DAZ_SDK_DIR cache var, subdirs below
├── injector_core/
│   ├── CMakeLists.txt          # SDK-independent static lib
│   ├── TmbReader.{h,cpp}
│   ├── MorphIndexReader.{h,cpp}
│   ├── FormulaCompiler.{h,cpp}
│   └── FormulaIR.h
├── daz_plugin/
│   ├── CMakeLists.txt          # links injector_core + Qt + Daz SDK -> .dsx
│   ├── PropertySourceAdapter.{h,cpp}
│   ├── FormulaControllerBuilder.{h,cpp}
│   ├── InjectorCore.{h,cpp}
│   └── PluginMain.cpp
└── tests/
    ├── CMakeLists.txt          # links only injector_core; no SDK/Qt dependency
    ├── test_tmb_reader.cpp
    ├── test_morph_index_reader.cpp
    └── test_formula_compiler.cpp
```

- `DAZ_SDK_DIR` is a required CMake cache variable pointing at a locally-installed Daz Studio SDK — the SDK is proprietary and is never vendored into this repo (matches how this project already treats other external, non-redistributable dependencies). Four SDK variants already exist locally for development/testing, at `y:/working/BlueMoonFoundry/daz-studio-sdks/{daz-studio-45-sdk,daz-studio-6-sdk}-{win32,macos}`; this design was investigated against `daz-studio-45-sdk-win32` specifically (its headers ship pre-extracted-able from `daz_sdk_headers.zip` in that directory). `daz_plugin`'s CMake should accept `DAZ_SDK_DIR` pointing at any of the four without needing separate build logic per variant, falling back to per-variant `#ifdef`s only if a header-level difference actually turns up (none expected per §4, unconfirmed for Studio 6 until built).
- `injector_core` links only the C++ standard library and a statically-linked `sqlite3` (same amalgamation approach as Subsystem A's project would use if it needed a C SQLite dependency — here it's the only DB dependency since there's no ORM equivalent in C++).
- `daz_plugin` is the only target that touches `DAZ_SDK_DIR` and Qt; it cannot be built or tested without a real SDK install.
- `injector_core_tests` builds and runs in ordinary CI (no Daz Studio required) — this is the CI-relevant boundary for Subsystem B, analogous to how `.venv\Scripts\python -m pytest tests\` is Subsystem A's CI-relevant boundary.

## 6. Testing Strategy

- **Unit tests (CI-able, no Daz Studio install required)** — via `injector_core_tests` (GoogleTest or Catch2; pick whichever this project's eventual C++ toolchain setup favors, not decided here):
  - `TmbReader` round-trip: reuse or regenerate the same fixture `.tmb` files Subsystem A's Python tests already produce (`tests/fixtures/dsf/*.dsf` run through the existing `write_tmb`), for cross-language byte-layout parity — a `.tmb` file written by Python must read identically in C++.
  - `MorphIndexReader`: query against a fixture `morph_index.db` built by actually running Subsystem A's `MorphIndexManager` once and checking the resulting file into `cpp/tests/fixtures/` — this way schema drift between the Python writer and C++ reader is caught by CI rather than discovered at runtime inside Daz Studio.
  - `FormulaCompiler`: fixture-based tests against real `formulas_json` blobs pulled straight from `morph_index.db` (both an algebra-formula fixture like `pJCMCloakBend_m90`'s and a spline-formula fixture like `body_cbs_foot_Back_l`'s, both already identified during §3.1/§4's scan) — asserting the IR classification (`AlgebraFormula` vs `SplineFormula`) and parsed contents (op sequence, or key list) are correct, with no Daz SDK types involved.

- **Manual/integration** — `FormulaControllerBuilder`, `PropertySourceAdapter`, and `InjectorCore` all touch live SDK objects and can only be exercised inside a running Daz Studio instance with the real plugin loaded (this is now a known, fixed boundary — not an open question, since §4 confirmed exactly which SDK classes are involved). This follows the same posture Subsystem A used for its own non-automatable step (`docs/superpowers/plans/2026-08-06-morph-ingest-transpiler.md`'s "Post-Plan: Real Library Smoke Test" — manual, not CI, but still gated as a required check before considering the subsystem done): inject a known algebra-formula morph and a known spline-formula morph, and confirm both the vertex deltas and the live ERC-driven value match expected results as the driving bone property is moved.

## 7. Threading & Memory Rules

Carried forward verbatim from the parent spec §8 as binding constraints on `daz_plugin`:
- All Daz Studio SDK scene-graph mutations occur on the main GUI thread only. `TmbReader`'s mmap read may happen off-thread (pure file I/O, no SDK objects touched), but the resulting delta buffer is only ever handed to SDK object construction (`DzMorph::setDeltas`, etc.) on the main thread.
- Objects derived from `DzBase` (e.g. `DzMorph`, `DzFormula`, `DzFloatProperty`) are Daz-Studio-managed once attached via `addModifier()`/`addProperty()` — never manually `delete` them after attachment.
- Per-morph injection failures (missing `.tmb` file, truncated data, missing SQLite row) log via `dzApp->log()` and skip that morph rather than crashing the host process — mirrors Subsystem A's own per-file try/except posture (`2026-08-06-morph-ingest-transpiler-design.md` §9), applied here at per-morph-injection granularity instead of per-file-ingest granularity.

## 8. Explicitly Out of Scope (Deferred to Later Specs)

- Subsystem C (scene lifecycle hooks — `aboutToLoadScene`/`sceneLoaded`, the custom `DzCustomData` manifest for fast scene reopen).
- Subsystem D (Qt dockable search panel, ChromaDB IPC bridge).
- Verifying `spline_tcb`/`spline_linear` evaluation *semantics* (the operator vocabulary itself is closed and confirmed — see §3.1's real-library scan) — deferred to implementation-time unit tests that check evaluated output against Daz Studio's own rendered result for a known control-point set, since static analysis of `morph_index.db` can confirm operand shape but not evaluation correctness.
