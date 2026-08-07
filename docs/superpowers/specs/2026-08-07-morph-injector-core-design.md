# Design: C++ Daz Studio SDK Injector Core (JIT Binary Morph Loader — Subsystem B)

**Status:** Draft — architecture proposal. Real-library formula-operator-vocabulary scan complete (§3.1); pending an SDK header spike to resolve the scene-attachment strategy (§4).
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
                    FormulaEvaluator  ◀── IPropertySource ──  PropertySourceAdapter
                    (RPN stack eval)      (abstract)          (real DzNode/DzFloatProperty
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

- **`FormulaEvaluator`** — evaluates one morph's `formulas_json` RPN stack against an abstract property source:
  ```cpp
  class IPropertySource {
  public:
      virtual ~IPropertySource() = default;
      virtual double resolve(const std::string& operand) = 0;
  };

  // A stack cell is either a scalar or a small control-point array (spline
  // operators push arrays, not just numbers -- see the real-library scan
  // below). No heap allocation needed: the largest observed array is 5
  // floats, so a fixed-capacity inline variant covers every case seen.
  struct StackValue {
      double scalar;
      std::array<double, 5> array;
      uint8_t array_len;   // 0 => scalar, else array of this length
  };

  double evaluateFormula(const std::string& formulas_json, IPropertySource& source);
  ```
  No Daz SDK types appear anywhere in this class — it is pure stack-machine evaluation, fully unit-testable with a fake `IPropertySource`.

  **Real-library operator vocabulary scan — done.** Subsystem A's own fixture sample (2 `.dsf` files) only exercised `push` and `mult`, which was too small to design an evaluator against. This design pass ran a full scan over the real, already-ingested `morph_index.db` (188,906 morphs, 23,596 with `formulas_json`, 0 malformed) and found the **complete, closed operator vocabulary is just five operators** — no `add`/`sub`/`div`/`clamp` appear anywhere in the real library:

  | op | occurrences | notes |
  |---|---|---|
  | `push` | 1,928,000 | `url` form (968,725) or `val` form (959,275) |
  | `mult` | 956,392 | binary: pops 2, pushes 1 |
  | `spline_tcb` | 732 | see below |
  | `spline_linear` | 83 | see below |
  | `neg` | 26 | unary: pops 1, pushes 1 |

  `push:val` is **not always a scalar** — of the 959,275 `val` pushes, 957,207 are plain numbers (float/int) but 2,068 push a small fixed-size array: `[5]`-element arrays (1,847 occurrences) and `[2]`-element arrays (221 occurrences). These are spline control points feeding a following `spline_tcb`/`spline_linear` op, e.g. a real fixture (`body_cbs_foot_Back_l.dsf`):
  ```json
  { "op": "push", "url": "Genesis9/l_foot:.../Genesis9.dsf#l_foot?rotation/x" },
  { "op": "push", "val": [27.6, 0, 0, 0, 0] },
  { "op": "push", "val": [65, 1, 0, 0, 0] },
  { "op": "push", "val": 2 },
  { "op": "spline_tcb" }
  ```
  i.e. `spline_tcb` pops a control-point count, N control-point arrays, and an input value, and evaluates a TCB (Kochanek–Bartels) spline through those control points at the input value — the input is a live bone-rotation property, and the spline is the ERC curve shape. This is why `StackValue` above must carry an array variant, not just a `double`: a purely-scalar stack cannot represent this operator's operands. `spline_linear` presumably differs only in interpolation method between control points (linear vs. TCB tangent-based); exact semantics of both need confirmation against the Daz Studio SDK's own spline evaluation (or DSON spec) when `FormulaEvaluator` is implemented, since this scan can confirm operator *shape* from the JSON but not verify evaluation *semantics* — that's an implementation-time correctness check (unit tests comparing against Daz Studio's own rendered result for a known control-point set), not something resolvable from static analysis of `morph_index.db` alone.

  This scan is complete and this section's operator table should be treated as closed against this library snapshot — but re-run it (one-off query, no new code needed) if `morph_index.db` is rebuilt against a substantially different or newer content library before implementing `FormulaEvaluator`, since a closed vocabulary from one snapshot isn't a guarantee against all possible DAZ content.

### 3.2 SDK-dependent layer (`daz_plugin` target)

- **`PropertySourceAdapter`** — the only component that touches real `DzNode`/`DzFloatProperty` objects to implement `IPropertySource::resolve()`. Translates an operand string (path-form `/data/.../Target.dsf#Node?property` or pathless `name:PropertyName`) into a live property read: if the operand resolves (via `MorphIndexReader`) to another indexed morph, recursively ensure that morph is injected (via `InjectorCore`) and read its current `DzFloatProperty` value; otherwise, resolve it as a direct scene-graph property path (bone rotation, etc.) via the Daz SDK's node/property lookup APIs. This is the component most exposed to the open SDK question in §4.

- **`InjectorCore`** (the parent spec's `ModernMorphInjector`) — orchestrates the full sequence from parent spec §5.1, adjusted for the real dependency model:
  1. Duplicate-modifier check: `targetNode->getObject()->findModifier(name)`.
  2. `MorphIndexReader::find_by_guid` for metadata.
  3. `TmbReader` mmap + read.
  4. Create `DzMorph`, set deltas, `addModifier`.
  5. Create `DzFloatProperty`, set min/max/clamp from metadata, `addProperty`.
  6. `MorphIndexReader::dependencies_of` → for each dependency, recursively `InjectorCore::injectMorph` first (so referenced morphs exist and have live property values before this morph's formula is evaluated).
  7. If `formulas_json` is non-null: `FormulaEvaluator::evaluateFormula` via a `PropertySourceAdapter` bound to `targetNode`, then attach the result to the live scene graph per whichever strategy §4's spike resolves.

- **`PluginMain`** — Daz SDK plugin registration boilerplate (`DZ_PLUGIN`/`DZ_IMPLEMENT_CLASS` macros per SDK convention); no logic beyond registering `InjectorCore`'s entry point.

## 4. The Open Question This Doc Does Not Resolve: How Does an Evaluated Formula Attach to the Scene Graph?

The parent spec's §4.2 sketch attaches ERC via a single native primitive (`DzERCDeltaAdd`, `stage->setScalar(...)`) — a shape that only fits a flattened parent→child scalar link, which is not what real formulas are. Two candidate strategies exist for the real RPN-stack shape, and **neither can be committed to without the actual Daz Studio SDK headers in hand**, which this design pass does not have access to:

- **Strategy A — Chain of native operators.** If the SDK's `DzFormula`/`DzOperator` primitives are general enough to represent `push`/`mult`/etc. as a composable chain of individually-typed operator objects (mirroring how the SDK's own formula system presumably represents native content), build the stack as a sequence of native SDK objects. This would let Daz Studio's own dependency/dirty-propagation system drive re-evaluation automatically, but requires the SDK to expose these primitives at a fine enough grain to match arbitrary formula shapes — unconfirmed.
- **Strategy B — Custom black-box operator.** Implement a single custom `DzOperator`-derived (or equivalent SDK extension point) class that holds the entire `formulas_json` stack and evaluates it internally via `FormulaEvaluator`, exposing only the final scalar to the SDK's property/dependency system as one opaque node. Simpler to implement correctly against `FormulaEvaluator` as already designed, but forgoes any native-primitive benefits (e.g. the SDK's own formula introspection/UI, if any) and needs its own dirty-tracking to know when to re-evaluate.

**This is flagged as an explicit unresolved question, not a design decision.** Resolving it requires a hands-on spike against the real Daz Studio SDK headers (specifically `dzformula.h`, `dzoperator.h`, and whatever ERC-stage classes the SDK actually ships, none of which are available in this repository or to this design pass). **This spike must be the first task of Subsystem B's implementation plan**, before `InjectorCore`'s formula-attachment step (§3.2 step 7) or `PropertySourceAdapter` are implemented — everything else in this document (`TmbReader`, `MorphIndexReader`, `FormulaEvaluator`, the CMake layout) is independent of this question's answer and can proceed first.

## 5. CMake / Build Layout

A new top-level `cpp/` tree, deliberately separate from the Python `src/`/`tests/` layout — unlike Subsystem A (which folded into the existing Python project structure because it shared conventions and tooling with the rest of the codebase), Subsystem B shares no buildable artifacts with the Python side and is a genuinely different toolchain. This matches the parent spec §6's original structure better than Subsystem A's decision to deviate from it.

```
cpp/
├── CMakeLists.txt              # top-level: DAZ_SDK_DIR cache var, subdirs below
├── injector_core/
│   ├── CMakeLists.txt          # SDK-independent static lib
│   ├── TmbReader.{h,cpp}
│   ├── MorphIndexReader.{h,cpp}
│   ├── FormulaEvaluator.{h,cpp}
│   └── IPropertySource.h
├── daz_plugin/
│   ├── CMakeLists.txt          # links injector_core + Qt + Daz SDK -> .dsx
│   ├── PropertySourceAdapter.{h,cpp}
│   ├── InjectorCore.{h,cpp}
│   └── PluginMain.cpp
└── tests/
    ├── CMakeLists.txt          # links only injector_core; no SDK/Qt dependency
    ├── test_tmb_reader.cpp
    ├── test_morph_index_reader.cpp
    └── test_formula_evaluator.cpp
```

- `DAZ_SDK_DIR` is a required CMake cache variable pointing at a locally-installed Daz Studio SDK — the SDK is proprietary and is never vendored into this repo (matches how this project already treats other external, non-redistributable dependencies).
- `injector_core` links only the C++ standard library and a statically-linked `sqlite3` (same amalgamation approach as Subsystem A's project would use if it needed a C SQLite dependency — here it's the only DB dependency since there's no ORM equivalent in C++).
- `daz_plugin` is the only target that touches `DAZ_SDK_DIR` and Qt; it cannot be built or tested without a real SDK install.
- `injector_core_tests` builds and runs in ordinary CI (no Daz Studio required) — this is the CI-relevant boundary for Subsystem B, analogous to how `.venv\Scripts\python -m pytest tests\` is Subsystem A's CI-relevant boundary.

## 6. Testing Strategy

- **Unit tests (CI-able, no Daz Studio install required)** — via `injector_core_tests` (GoogleTest or Catch2; pick whichever this project's eventual C++ toolchain setup favors, not decided here):
  - `TmbReader` round-trip: reuse or regenerate the same fixture `.tmb` files Subsystem A's Python tests already produce (`tests/fixtures/dsf/*.dsf` run through the existing `write_tmb`), for cross-language byte-layout parity — a `.tmb` file written by Python must read identically in C++.
  - `MorphIndexReader`: query against a fixture `morph_index.db` built by actually running Subsystem A's `MorphIndexManager` once and checking the resulting file into `cpp/tests/fixtures/` — this way schema drift between the Python writer and C++ reader is caught by CI rather than discovered at runtime inside Daz Studio.
  - `FormulaEvaluator`: hand-constructed op stacks against a fake `IPropertySource`, covering both path-form and `name:`-form operands, plus (once §3.1's real-library operator scan lands) one test per confirmed real-world operator.

- **Manual/integration (not CI-able until §4 resolves what's isolable)** — actual `DzMorph`/`DzFloatProperty` injection inside a running Daz Studio instance with the real plugin loaded. This follows the same posture Subsystem A used for its own non-automatable step (`docs/superpowers/plans/2026-08-06-morph-ingest-transpiler.md`'s "Post-Plan: Real Library Smoke Test" — manual, not CI, but still gated as a required check before considering the subsystem done). No CI substitute exists for `daz_plugin` until the SDK spike (§4) shows how much of the attach-to-scene-graph logic can be pulled into `injector_core` behind another testable seam.

## 7. Threading & Memory Rules

Carried forward verbatim from the parent spec §8 as binding constraints on `daz_plugin`:
- All Daz Studio SDK scene-graph mutations occur on the main GUI thread only. `TmbReader`'s mmap read may happen off-thread (pure file I/O, no SDK objects touched), but the resulting delta buffer is only ever handed to SDK object construction (`DzMorph::setDeltas`, etc.) on the main thread.
- Objects derived from `DzBase` (e.g. `DzMorph`, `DzFormula`, `DzFloatProperty`) are Daz-Studio-managed once attached via `addModifier()`/`addProperty()` — never manually `delete` them after attachment.
- Per-morph injection failures (missing `.tmb` file, truncated data, missing SQLite row) log via `dzApp->log()` and skip that morph rather than crashing the host process — mirrors Subsystem A's own per-file try/except posture (`2026-08-06-morph-ingest-transpiler-design.md` §9), applied here at per-morph-injection granularity instead of per-file-ingest granularity.

## 8. Explicitly Out of Scope (Deferred to Later Specs)

- Subsystem C (scene lifecycle hooks — `aboutToLoadScene`/`sceneLoaded`, the custom `DzCustomData` manifest for fast scene reopen).
- Subsystem D (Qt dockable search panel, ChromaDB IPC bridge).
- Resolving §4's native-vs-custom-operator question — explicitly deferred to a dedicated SDK header spike, which becomes the first task of Subsystem B's implementation plan.
- Verifying `spline_tcb`/`spline_linear` evaluation *semantics* (the operator vocabulary itself is closed and confirmed — see §3.1's real-library scan) — deferred to implementation-time unit tests that check evaluated output against Daz Studio's own rendered result for a known control-point set, since static analysis of `morph_index.db` can confirm operand shape but not evaluation correctness.
