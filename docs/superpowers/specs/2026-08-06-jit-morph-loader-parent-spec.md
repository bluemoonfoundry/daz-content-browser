# JIT Binary Morph Loader & Semantic Asset Manager (Option 3 Architecture) — Parent Spec

**Document Version:** 1.0
**Target Environment:** Daz Studio 4.x / 5.x (C++ SDK), Qt 5.x / 6.x, Python 3.10+ (Ingest/ChromaDB), SQLite 3
**Audience:** Software Engineers, AI Code Assistants (Claude, Codex, Cursor)
**Provenance:** Provided by the project owner at the start of the JIT loader work (2026-08-06). Saved verbatim here so it survives session clears — this is the source document Subsystem A's design was scoped down from.

## Status (as of 2026-08-07)

- **Subsystem A (Offline Ingest Engine / Transpiler) — DONE.** See `docs/superpowers/specs/2026-08-06-morph-ingest-transpiler-design.md` for its (revised) design and `docs/superpowers/plans/2026-08-06-morph-ingest-transpiler.md` for its implementation plan. Merged to branch `feature/jit-morph-loader` (pushed to origin, not yet merged to `main`). Proven against the real ~321K-file library: 188,906 morphs ingested, SQLite/Chroma parity confirmed, semantic search verified working.
- **Subsystems B (C++ SDK Core Injection), C (Scene Lifecycle & Persistence), D (UI & ChromaDB Bridge) — NOT STARTED.** This document is their starting point. Each should get its own brainstorming session → design spec → implementation plan, the same way Subsystem A did, rather than being implemented directly from this parent spec as-is.

### Known deviations from this spec, discovered building Subsystem A

Real DAZ `.dsf` files diverged from a few assumptions in this document. Whoever picks up Subsystem B should read these before assuming the schemas/formats below are final:

1. **ERC formulas are not simple parent→child scalar links.** Section 3.1's `erc_relationships` table (`parent_morph_id`, `child_morph_id`, `stage`, `scalar_multiplier`) doesn't fit reality. Real `.dsf` `formulas` are RPN-style operation stacks (`push`/`mult`/etc.) that can reference arbitrary properties — bone rotations, not just other morphs — and most real references use a pathless form (`Label:#PropertyName?value`) rather than the path form this doc assumed. The actual implementation stores the raw operation stack verbatim (`formulas_json` column) plus a best-effort `morph_dependencies` edge table resolved by `(target_figure, name)` matching. See `2026-08-06-morph-ingest-transpiler-design.md` §2 and §4 for the full rationale — Subsystem B's C++ injector will need to interpret these raw operation stacks itself (evaluate the RPN against live scene graph properties), which Section 4.2's `resolveAndInjectDependencies`/`applyErcLink` sketch below does not yet account for.
2. **The `.tmb` header's `vertex_count` field must be signed (int32), not `uint32_t`.** Real `.dsf` files legitimately use `-1` as a documented DSON sentinel meaning "unspecified/same as base mesh." Section 3.2's struct below is corrected in the note under its definition.
3. **`.dsf` files may be gzip-compressed** with no `.gz` suffix (detected via magic bytes), and some vendors append trailing padding bytes after the valid gzip stream that naive multistream-aware gzip readers choke on. Not mentioned in this spec's Python ingest section but confirmed essential — 37%+ of a real library sample was gzip-compressed.

The rest of this document is preserved as originally written, for Subsystems B–D to plan against.

---

## 1. Executive Summary & System Goals

### 1.1 Problem Statement
Daz Studio's native architecture performs a synchronous, single-threaded file-system scan of all registered `data/` directories whenever a base figure (e.g., Genesis 8/9) is instantiated or a scene is opened. With large asset libraries (50,000+ `.dsf` files), this leads to multi-minute load times, gigabytes of unnecessary RAM usage (Property Instantiation), UI sluggishness due to slider bloat, and long scene open/save cycles.

### 1.2 Solution Overview
The **JIT Binary Morph Loader** decouples asset discovery from Daz Studio's native `DzAssetMgr`. Assets are stored in an unregistered "Ghost Library" directory. Asset metadata and vector embeddings are stored in ChromaDB and SQLite.

At runtime, figures load as clean base meshes in milliseconds. When a user queries a morph via the custom Qt UI, a C++ SDK plugin uses **Just-In-Time (JIT) Injection** to stream binary vertex deltas and build ERC (Enhanced Remote Control) dependency chains directly in RAM, bypassing disk crawling and native JSON parsing.

### 1.3 Key Performance Targets
*   **Base Figure Load Time:** $< 2.0$ seconds (down from 1–3 minutes).
*   **Morph Search Latency:** $< 50$ ms (Semantic via ChromaDB + Metadata via SQLite).
*   **Morph Injection Latency:** $< 100$ ms per individual morph (including ERC dependencies).
*   **Scene Load Acceleration:** $10\times$ to $20\times$ faster scene opening using batch binary loading with scene updates suspended.

---

## 2. High-Level Architecture & Component Interaction

```
 [ Unregistered "Ghost Library" (.dsf JSON Files) ]
                         │
                         ▼
        ┌─────────────────────────────────┐
        │   Offline Ingest & Transpiler   │
        └────────────────┬────────────────┘
                         │
        ┌────────────────┴────────────────┐
        │                                 │
        ▼                                 ▼
┌───────────────┐               ┌──────────────────┐
│ ChromaDB      │               │ SQLite DB        │
│ (Embeddings/  │               │ (Metadata, ERC   │
│ Semantic UI)  │               │ Graphs, Paths)   │
└───────┬───────┘               └────────┬─────────┘
        │                                │
        │    ┌───────────────────────────┘
        ▼    ▼
┌──────────────────────────────────────────────────┐
│ Custom Qt UI Panel (In-Process Daz Plugin)       │
└───────────────────────┬──────────────────────────┘
                        │ (Triggers JIT Injection)
                        ▼
┌──────────────────────────────────────────────────┐
│ C++ Native Plugin Core (Daz Studio SDK)          │
│ ├─ JIT Injector (DzMorph + DzFormula)            │
│ ├─ Binary Delta Streamer (.tmb Parser)           │
│ └─ Scene Lifecycle Hook (DzSceneImporter)        │
└───────────────────────┬──────────────────────────┘
                        │ (Direct Memory Manipulation)
                        ▼
┌──────────────────────────────────────────────────┐
│ Daz Studio Core Engine / Viewport Scene Graph    │
└──────────────────────────────────────────────────┘
```

---

## 3. Database Schemas & File Formats

### 3.1 SQLite Dependency & Metadata Schema (`morph_index.db`)

> **Superseded for Subsystem A** by the schema in `2026-08-06-morph-ingest-transpiler-design.md` §4 — kept here for historical reference and because Subsystem B will need to know both what was actually built and what this document originally proposed.

```sql
-- Core Morph Entity Table
CREATE TABLE morphs (
    morph_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,             -- Unique string hash (e.g., "Genesis8Female:/data/.../MorphName.dsf")
    label TEXT NOT NULL,                  -- Display Label (e.g., "Nose Width")
    name TEXT NOT NULL,                   -- Internal Name (e.g., "FHMNoseWidth")
    target_figure TEXT NOT NULL,          -- Base Figure ID (e.g., "Genesis8Female")
    group_path TEXT,                      -- Path in Parameters/Shaping tab
    binary_blob_path TEXT NOT NULL,       -- Rel Path to compiled .tmb file
    min_value REAL DEFAULT 0.0,
    max_value REAL DEFAULT 1.0,
    is_clamped BOOLEAN DEFAULT 1
);

-- ERC / Dependency Graph Table
CREATE TABLE erc_relationships (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_morph_id INTEGER NOT NULL,     -- Controlling Morph
    child_morph_id INTEGER NOT NULL,      -- Controlled Morph (or sub-component)
    stage TEXT CHECK(stage IN ('ERCControlWithKey', 'ERCDeltaAdd', 'ERCDirectKey')),
    scalar_multiplier REAL DEFAULT 1.0,
    FOREIGN KEY(parent_morph_id) REFERENCES morphs(morph_id),
    FOREIGN KEY(child_morph_id) REFERENCES morphs(morph_id)
);

CREATE INDEX idx_target_figure ON morphs(target_figure);
CREATE INDEX idx_erc_parent ON erc_relationships(parent_morph_id);
```

### 3.2 Custom Binary Delta Format (`.tmb` - Turbo Morph Binary)
To eliminate JSON parsing overhead, raw `.dsf` vertex deltas are transpiled into a fixed-stride binary file.

```
[ HEADER (16 Bytes) ]
  - Magic Bytes: 4 Bytes ("TMB1")
  - Vertex Count (uint32_t): 4 Bytes
  - Flags / Compression (uint32_t): 4 Bytes
  - Reserved: 4 Bytes

[ DATA PAYLOAD ]
  - Vertex Array: Array of `TMB_VertexDelta` structs

  struct TMB_VertexDelta {
      uint32_t vertex_index; // Vertex Index in Base Mesh
      float dx;             // Delta X
      float dy;             // Delta Y
      float dz;             // Delta Z
  };
```

> **Correction (built into Subsystem A):** the "Vertex Count" field is **signed** (`int32_t`), not `uint32_t` — real `.dsf` files use `-1` as a valid DSON sentinel for "unspecified." The "Flags / Compression" field as originally specified was not implemented; Subsystem A's actual header is `magic(4) | vertex_count int32(4) | delta_count uint32(4) | reserved(4)` — delta_count (sparse rows present) replaces the flags word, since it's needed for bounds-checking and no compression scheme was ultimately used at the `.tmb` layer. See the design doc §5 for the byte-exact layout Subsystem B's C++ reader must match.

---

## 4. Subsystem Specifications

### 4.1 Subsystem A: Offline Ingest Engine (Transpiler)
*   **Language:** Python 3.10+
*   **Responsibility:** Reads uncompressed/gzipped `.dsf` files from the "Ghost Library." Parses JSON to extract vertex deltas, formulas (ERC), labels, and paths.

#### Key Operations:
1.  **JSON Stream Parsing:** Uses `ijson` or `simdjson` to read raw `.dsf` structures.
2.  **Transpiling Deltas:** Converts `modifier_library -> channel -> morph -> deltas` into raw binary `.tmb` files stored on disk.
3.  **Graph Construction:** Parses `formulas` arrays to extract Stage, Target, and Multipliers. Populate `erc_relationships` in `morph_index.db`.
4.  **Vector Embedding Generation:** Sends `label`, `group_path`, and vendor metadata strings to ChromaDB to generate embeddings for semantic search.

> **Built differently:** Subsystem A used stdlib `json` (not `simdjson`/`ijson` — unnecessary complexity at this file count, I/O and embedding dominate cost), reused this project's existing `embedding_utils.generate_embeddings` (BAAI/bge-large-en-v1.5, ONNX) rather than a separate embedding stack, and dropped vendor metadata from the embedding text (schema has no vendor column — `label` + `group_path` only). See the design doc for the actual implementation.

---

### 4.2 Subsystem B: C++ Native Plugin Core (Daz Studio SDK)
*   **Language:** C++17 / Qt 5.15 (matching Daz Studio SDK build specs)
*   **Responsibility:** Directly manipulate Daz Studio scene nodes and property graphs in RAM via C++ API.

#### Core C++ Interfaces to Implement:

```cpp
#include <dznode.h>
#include <dzobject.h>
#include <dzshape.h>
#include <dzmorph.h>
#include <dzfloatproperty.h>
#include <dzformula.h>

class ModernMorphInjector : public QObject {
    Q_OBJECT
public:
    explicit ModernMorphInjector(QObject *parent = nullptr);
    ~ModernMorphInjector();

    // Primary JIT Entry Point
    DzMorph* injectMorph(DzNode* targetNode, const QString& morphGuid);

private:
    // Helper Methods
    bool loadTmbBinary(const QString& tmbPath, QVector<int>& outIndices, QVector<DzVec3>& outDeltas);
    void resolveAndInjectDependencies(DzNode* targetNode, int parentMorphId);
    DzFloatProperty* createProperty(DzMorph* morph, const MorphMetaData& meta);
    void applyErcLink(DzFloatProperty* parentProp, DzFloatProperty* childProp, float scalar, const QString& stage);
};
```

#### Detailed Injection Sequence (`injectMorph` Logic):
1.  **Duplicate Check:** Check if `targetNode->getObject()->findModifier(morphName)` already exists. If yes, return pointer.
2.  **Fetch Metadata & Deltas:** Query SQLite for `morph_guid`. Open `.tmb` binary file via `QFile`/Memory Map (`mmap`).
3.  **Instantiate `DzMorph`:**
    ```cpp
    DzObject *obj = targetNode->getObject();
    DzShape *shape = obj ? obj->getShape() : nullptr;

    DzMorph *newMorph = new DzMorph();
    newMorph->setName(meta.name);
    newMorph->setLabel(meta.label);

    // Set Deltas
    DzMorphDeltas deltas;
    for (size_t i = 0; i < vertexCount; ++i) {
        deltas.addDelta(indices[i], vecDeltas[i]);
    }
    newMorph->setDeltas(deltas);

    obj->addModifier(newMorph);
    ```
4.  **Instantiate `DzFloatProperty`:** Create slider, set min/max/clamping, add to `DzMorph`.
5.  **Recursive ERC Resolution:**
    *   Query `erc_relationships` where `parent_morph_id == meta.id`.
    *   For each child: Call `injectMorph(targetNode, childGuid)`.
    *   Build `DzFormula`:
        ```cpp
        DzFormula *formula = new DzFormula();
        DzERCDeltaAdd *stage = new DzERCDeltaAdd(); // or appropriate stage
        stage->setSubscribingProperty(childProperty);
        stage->setTargetProperty(parentProperty);
        stage->setScalar(scalarMultiplier);
        formula->setStage(stage);
        childProperty->addFormula(formula);
        ```

> **Needs re-planning against Subsystem A's actual output:** step 5 above assumes the now-superseded `erc_relationships(parent_morph_id, child_morph_id, stage, scalar_multiplier)` table. The real `morph_dependencies` table only has `(dependent_morph_id, referenced_morph_id)` — no stage or scalar, because those live inside the raw RPN `formulas_json` operation stack on the `morphs` row instead. Subsystem B's design will need a real RPN evaluator (interpreting `push`/`mult`/etc. against live `DzFloatProperty` values, including bone-rotation properties, not just other morphs) rather than a simple scalar `applyErcLink`. This is the biggest architectural gap between this parent spec and what Subsystem A actually produced — budget real design time for it when brainstorming Subsystem B.

---

### 4.3 Subsystem C: Scene Lifecycle & Persistence Subsystem

#### 4.3.1 Custom Scene Manifest (`DzCustomData`)
To avoid missing file dialogs on scene reload, the plugin hooks into scene serialization to write an **Injected Asset Manifest**.

*   **SDK Class:** `DzCustomData` attached to the root `DzScene`.
*   **Saved Payload (JSON String in Scene):**
    ```json
    {
      "jit_loader_manifest": {
        "version": "1.0",
        "injected_morphs": [
          { "guid": "Genesis8Female:/data/.../NoseWidth.dsf", "val": 0.75 },
          { "guid": "Genesis8Female:/data/.../LipFullness.dsf", "val": 0.30 }
        ]
      }
    }
    ```

#### 4.3.2 Fast Scene Open Hook Workflow
To override native Daz load lag during `.duf` opens:

1.  **Register Hook:** Connect to `dzScene->aboutToLoadScene()` signal.
2.  **Suspend Scene Updates:**
    ```cpp
    dzScene->setUpdateAllManagers(false); // Freezes Viewport & Property recalculations
    ```
3.  **Read Manifest / Dirty Parse:** Extract the injected GUID list from the scene file or custom manifest.
4.  **Batch Inject:** Loop through GUIDs, stream binary `.tmb` files, and instantiate `DzMorph` objects in parallel threads where safe.
5.  **Resume Scene Updates:**
    ```cpp
    dzScene->setUpdateAllManagers(true);
    dzScene->timeChanged(); // Force single global redraw
    ```

> **Batch-suspension mechanism does not exist — confirmed absent, not just unimplemented (beads-daz-content-browser-jhq.5, closed):** `setUpdateAllManagers` was already flagged missing when Subsystem C was filed (see the epic's design notes). This task did a wider real-header sweep for *any* scene-wide update-suspension/batching primitive across `dzscene.h`, `dzelement.h`, `dznode.h`, `dzobject.h`, `dznumericproperty.h`, `dzprogress.h`, and `dzapp.h` (`daz-studio-45-sdk-win32`), and found none:
> - `DzProperty`/`DzElement`/`DzNode`/`DzObject::beginEdit()`/`finishEdit()`/`cancelEdit()` exist, but the SDK's own doc comment says plainly: *"When beginEdit is called, the property will create an undo item."* This is an undo-transaction API for a single property edit gesture, not a recompute-suspension mechanism — using it per-morph would add undo overhead, not remove work.
> - `DzProgress`/`DzBackgroundProgress` (`dzprogress.h`) are UI progress-bar feedback only (`step`/`update`/`finish`/`cancel`, plus a static `enable`/`pause`/`resume` that only toggles whether the progress dialog is shown) — no batching or update-suspension semantics at all.
> - `DzApp::enableMultiThreading(bool)` (`dzapp.h`) is a real, undocumented-in-scope global toggle ("If true, multi-threaded features are enabled") with no stated relationship to scene-graph recompute, and toggling a process-wide flag to affect unrelated Studio subsystems for one plugin's narrow benefit is out of scope regardless — it also doesn't touch the binding main-thread-only rule for SDK object construction (design doc §7), which callers must obey either way.
> - `dzScene->timeChanged()` above is itself invalid as written: it's a Qt *signal* (declared under `signals:` in `dzscene.h`), not a callable slot — external code cannot "force" it by calling it directly the way the sketch implies.
>
> **Conclusion:** no real batching/suspension mechanism exists in this SDK version. Task 4's `SceneManifestLoader::restore()` (`cpp/daz_plugin/SceneManifestLoader.cpp`) already implements the accepted fallback — a plain sequential loop over manifest entries calling `InjectorCore::injectMorphByGuid()` on the main thread, one morph at a time, matching Subsystem B's binding main-thread-only rule. This is also consistent with how the SDK's own dirty-propagation actually works (design doc §4.3/§4 of the injector-core design): `DzFormula`/`DzERCLink` controllers are lazily re-evaluated off property dirty flags, not eagerly recomputed on every `setValue()` call, so there is no synchronous "recompute storm" per injected morph for a suspension mechanism to batch away in the first place. Per the parent spec's own <100ms-per-morph budget (§1.3), N sequential morphs cost roughly N×100ms worst-case; `InjectorCore`'s own header note (`m_registry`'s doc comment) estimates realistic scene sessions at "dozens to low hundreds" of injections, i.e. a worst-case reopen cost on the order of single-digit seconds at the low-hundreds end — acceptable for a one-time scene-load event, not a per-frame cost. No further action taken; accepted as the design.

---

### 4.4 Subsystem D: User Interface (Qt Panel & ChromaDB Bridge)

*   **UI Framework:** Embedded Qt DockWindow (`DzDockableWidget`).
*   **Search Engine:** ChromaDB (Python Service running via IPC/gRPC or embedded Python C-API).

#### UI Workflow:
1.  **Query Input:** User types semantic search string (e.g., *"alien character with sharp cheekbones"*).
2.  **ChromaDB Query:** Returns ranked list of asset GUIDs.
3.  **SQLite Join:** Populates UI list view with Labels, Thumbnails, Categories, and current active status in the selected scene node.
4.  **User Action (Click / Slider Move):**
    *   **Single Click:** Triggers `ModernMorphInjector::injectMorph()`. Morph slider appears in custom UI.
    *   **Value Change:** Adjusts `DzFloatProperty::setValue()`.

---

## 5. Concrete Algorithmic Workflows

### 5.1 End-to-End JIT Morph Injection Algorithm

```
FUNCTION ApplyMorphToSelection(targetNode, morphGuid):
    1. IF targetNode IS NULL OR targetNode is not a DzSkeleton/DzNode:
           RETURN Error("Invalid Selection")

    2. Check SQLite for morphGuid
       IF NOT FOUND:
           RETURN Error("Morph missing from Shadow Library Index")

    3. Query SQLite for morph Metadata (Name, Label, Min, Max, TmbPath)

    4. // Step A: Check Scene Graph
       existingModifier = targetNode.getObject().findModifier(Metadata.Name)
       IF existingModifier IS NOT NULL:
           RETURN existingModifier.getProperty() // Already injected

    5. // Step B: Read Binary Deltas
       deltasArray = ReadBinaryTmbFile(Metadata.TmbPath)

    6. // Step C: SDK Injection
       dzMorph = Instantiate DzMorph
       dzMorph.setLabel(Metadata.Label)
       dzMorph.setName(Metadata.Name)
       dzMorph.setDeltas(deltasArray)

       targetNode.getObject().addModifier(dzMorph)

       floatProperty = Instantiate DzFloatProperty
       dzMorph.addProperty(floatProperty)

    7. // Step D: Recursive ERC Resolution
       childLinks = Query SQLite erc_relationships WHERE parent_guid == morphGuid

       FOR EACH link IN childLinks:
           childProp = ApplyMorphToSelection(targetNode, link.child_guid)

           dzFormula = Instantiate DzFormula
           dzFormula.setTarget(floatProperty)
           dzFormula.setSubscriber(childProp)
           dzFormula.setScalar(link.scalar_multiplier)

           childProp.addFormula(dzFormula)

    8. RETURN floatProperty
END FUNCTION
```

---

## 6. Project Directory Structure for Implementation

```
daz_jit_loader/
├── bin/                        # Compiled DLLs / .dsx plugins
├── src/
│   ├── cpp/                    # Native Daz Plugin (C++)
│   │   ├── CMakeLists.txt
│   │   ├── PluginMain.cpp      # SDK Registration
│   │   ├── InjectorCore.cpp    # DzMorph / DzFormula logic
│   │   ├── TmbReader.cpp       # Binary I/O
│   │   ├── SceneHook.cpp       # DzSceneImporter / Manifest Hook
│   │   └── QtUI/               # Custom Qt Widgets / Panels
│   │
│   ├── python/                 # Offline Tools & Search Bridge
│   │   ├── transpiler/
│   │   │   ├── dsf_parser.py   # Read .dsf JSON
│   │   │   ├── tmb_writer.py   # Write binary .tmb
│   │   │   └── erc_indexer.py  # Populate SQLite
│   │   ├── search/
│   │   │   ├── chromadb_service.py # Vector DB IPC Wrapper
│   │   │   └── sqlite_client.py
│   │
│   └── schemas/
│       ├── morph_index.sql     # SQLite DDL
│       └── tmb_spec.h          # C++ Binary Struct Definitions
└── tests/
    ├── benchmark_load.cpp
    └── test_erc_resolution.py
```

> **Built differently:** Subsystem A's actual layout lives directly under this repo's existing `src/`/`tests/` (following the codebase's existing conventions — `src/dsf_parser.py`, `src/tmb_format.py`, `src/managers/morph_index_manager.py`, `src/managers/morph_transpiler.py`, `tests/test_*.py`), not a separate `daz_jit_loader/` tree. Subsystem B (a genuinely separate C++/CMake toolchain) is a much better candidate for a structure resembling this section — it shares nothing buildable with the Python side.

---

## 7. Implementation Roadmap & Milestones

### Phase 1: Transpiler & Offline Indexer (Python) — DONE, see Subsystem A design/plan docs
*   [x] Write `.dsf` JSON parser (stdlib `json`, not `simdjson`).
*   [x] Build `.tmb` binary converter.
*   [x] Implement SQLite schema and populator for ERC relationships (raw op-stack + best-effort dependency edges, not the flattened schema this doc originally proposed).
*   [x] Connect ChromaDB vector database to generate embeddings for morph labels and paths.

### Phase 2: C++ SDK Core Injection Proof-of-Concept — NOT STARTED
*   [ ] Set up Daz Studio C++ SDK CMake environment.
*   [ ] Implement `TmbReader.cpp` to read `.tmb` files into memory buffers (note: signed `vertex_count`, see §3.2 correction).
*   [ ] Implement basic `DzMorph` creation and vertex delta assignment on a Genesis 8/9 character.
*   [ ] Implement single-level `DzFormula` (ERC) linking in C++ (needs an RPN evaluator for the real `formulas_json` shape, not the simple scalar link originally sketched).

### Phase 3: Recursive ERC Graph Resolver — NOT STARTED
*   [ ] Implement multi-level recursive dependency loading from SQLite in C++ (query `morph_dependencies`, evaluate each dependent's `formulas_json` operation stack).
*   [ ] Add memory caching for active binary deltas to minimize file I/O.
*   [ ] Test complex morphs (e.g., Body Shapes with hundreds of JCMs) for stability and visual correctness.

### Phase 4: UI & Scene Lifecycle Integration — NOT STARTED
*   [ ] Build Qt Dockable Widget inside Daz Studio.
*   [ ] Integrate ChromaDB search interface into the Qt widget.
*   [ ] Implement `DzScene` hooks (`aboutToLoadScene`, `sceneLoaded`) to write/read custom manifest blocks.
*   [ ] Implement batch update suspension (`setUpdateAllManagers(false)`) during scene opens.

---

## 8. Critical Directives for AI Code Assistants

When generating code from this specification, enforce the following constraints:
1.  **Daz SDK Memory Rules:** Objects derived from `DzBase` (like `DzMorph`, `DzFormula`, `DzFloatProperty`) are managed by Daz Studio's internal garbage collection when attached to scene elements (`addModifier()`, `addProperty()`). **Do not manually `delete` pointers** after attaching them to nodes/objects.
2.  **Thread Safety:** Daz Studio SDK scene manipulations **must occur on the main GUI thread**. Threading should only be used for reading `.tmb` binary files from disk into RAM buffers before applying them to the SDK objects.
3.  **Float Precision:** Ensure vertex delta coordinates align with Daz Studio's internal floating-point units (Centimeters).
4.  **Error Handling:** If a binary delta read fails or a SQLite parent ID is missing, fail gracefully with an inline C++ log (`dzApp->log()`) without crashing the application process.
