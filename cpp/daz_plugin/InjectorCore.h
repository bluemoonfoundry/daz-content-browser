/**********************************************************************
    InjectorCore -- the parent spec's "ModernMorphInjector". Top-level
    orchestration for just-in-time morph injection: given a live DzNode and a
    morph identity (guid or morph_id), materialise that morph -- and, depth-first,
    everything it depends on -- as real DzMorph modifiers with real vertex deltas
    and real, natively-evaluated ERC controllers.

    Design: docs/superpowers/specs/2026-08-07-morph-injector-core-design.md
    sections 3.2 (this component's numbered sequence), 4 (controller attachment),
    7 (threading & memory rules).

    Sequence per injection (design section 3.2):
      1. Duplicate-modifier check -- DzObject::findModifier(name) (dzobject.h).
      2. Metadata -- MorphIndexReader::find_by_guid / find_by_id.
      3. Deltas -- injector_core::TmbReader (mmap, zero-copy).
      4. DzMorph + DzMorphDeltas::addDeltas(indexes, deltas) batch load, then
         DzObject::addModifier(). No separate DzFloatProperty is created:
         DzMorph makes its own value channel (dzmorph.h getValueChannel()).
      5. MorphIndexReader::dependencies_of() -> recursive injectMorph() for each,
         BEFORE step 6, so every referenced morph has a live property to bind to.
      6. formulas_json (if non-null) -> injector_core::compileFormulaSet ->
         FormulaControllerBuilder::attachFormulaSet onto the value channel. The
         whole array is compiled and attached as one ordered set so that entries
         sharing an output combine via their "stage" instead of clobbering each
         other (beads-w56).

    THREADING (design section 7, binding constraint):
      Every public method here mutates the live scene graph and MUST be called on
      the main GUI thread only. There are deliberately no threading primitives in
      this class -- there is nothing to synchronise if the entire call chain is
      single-threaded by contract, and adding a mutex would only paper over a
      violation of that contract. The only off-GUI-thread-safe piece is the
      TmbReader file read itself, which is a pure mmap of an on-disk file and
      touches no SDK object; hoisting it onto a worker thread is a future
      optimisation this class's structure permits but does not perform.

    MEMORY (design section 7):
      DzMorph / DzMorphDeltas / DzFormula / DzERCLink become Daz-Studio-managed
      the moment they are handed over (addModifier(), insertController()). This
      class only ever `delete`s an object it built but never successfully handed
      over -- the same posture FormulaControllerBuilder takes.

    FAILURE POSTURE (design section 7):
      A missing .tmb, malformed .tmb, missing morph_index.db row, unresolvable
      formula operand, or rejected addModifier() logs via dzApp->log() and
      returns 0 for *that morph only*. Nothing here throws to its caller; an
      unusable dependency degrades the dependent morph's formula rather than
      aborting the whole injection or the host process.
**********************************************************************/

#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include <QtCore/QString>

#include "MorphIndexReader.h"

#include "FormulaControllerBuilder.h"
#include "PropertySourceAdapter.h"

class DzFloatProperty;
class DzNode;
class DzNumericProperty;

namespace daz_plugin {

/**
    Owns the morph index connection and the per-injection recursion state.

    Cheap to keep alive for a session (it holds a read-only sqlite handle) and
    cheap enough to construct per invocation; the smoke-test action constructs
    one per click, Subsystem D will want a longer-lived one.
**/
class InjectorCore : public MorphInjectionEnsurer
{
public:
    /**
        One morph InjectorCore has successfully injected during this object's
        lifetime: enough to both identify it (guid/target_figure -- the same pair
        Task 3's manifest serializes, see injector_core::InjectedMorphEntry in
        SceneManifest.h) and read its live state back out (node + channel).

        `node` and `channel` are raw SDK pointers into the live scene, not
        owned here and not re-validated on every access -- see m_registry below
        for why that is safe under this design's read-lazily-at-save-time rule.
    **/
    struct InjectedMorphRecord
    {
        int64_t morphId = 0;
        QString guid;
        QString targetFigure;
        DzNode* node = 0;
        DzFloatProperty* channel = 0;
    };

    /**
        @param morphIndexDbPath  path to Subsystem A's morph_index.db.
        @param morphCacheRoot    root that MorphRecord::tmb_path is relative to
                                 (Subsystem A stores tmb_path relative to
                                 MORPH_CACHE_PATH -- see
                                 src/managers/morph_transpiler.py's tmb_rel_path).

        Never throws: a database that cannot be opened leaves the object in a
        !isOpen() state with lastError() set, so a construction failure is
        reportable rather than fatal inside a plugin entry point.
    **/
    InjectorCore( const QString& morphIndexDbPath, const QString& morphCacheRoot );
    virtual ~InjectorCore();

    //! False if morph_index.db could not be opened; every inject call then no-ops.
    bool isOpen() const { return m_index != 0; }

    //! Why the last call failed. Empty after a successful call.
    const QString& lastError() const { return m_lastError; }

    /**
        Injects the morph with `guid` (a morph_index.db `morphs.guid`, i.e. the
        DSON asset id / content path) onto `targetNode`, together with its whole
        dependency closure. Returns the morph's live value channel, or 0 on
        failure (see FAILURE POSTURE above).

        Idempotent: a second call for the same (node, guid) finds the existing
        DzMorph via DzObject::findModifier() and returns its existing value
        channel without re-injecting.
    **/
    DzFloatProperty* injectMorphByGuid( DzNode* targetNode, const QString& guid );

    //! As injectMorphByGuid, keyed on morph_index.db's primary key instead.
    DzFloatProperty* injectMorph( DzNode* targetNode, int64_t morphId );

    /**
        MorphInjectionEnsurer (PropertySourceAdapter.h).

        Called *from inside* an in-progress injection, when a formula operand
        names another indexed-but-not-yet-injected morph. Re-entrancy safe:
        see the m_inFlight / m_valueChannels notes on those members.

        Only meaningful during an injection: returns 0 if called outside one (no
        target node is established). The referenced morph is injected onto the
        node matching ITS OWN target_figure -- normally the same node the
        enclosing injectMorph() call is targeting, but not necessarily
        (daz-content-browser-e9y); a cross-figure operand whose figure is not in
        the scene is logged and skipped rather than injected onto the wrong node.
    **/
    virtual DzNumericProperty* ensureInjectedAndGetValueChannel( int64_t morphId );

    /**
        Every morph InjectorCore has successfully injected during this object's
        lifetime (i.e. across every top-level injectMorph (or
        ensureInjectedAndGetValueChannel) call so far, not just the most recent
        one -- unlike m_valueChannels/m_inFlight, this is NOT cleared when a
        top-level call returns).

        Entries are deduplicated on (node, morph_id), NOT morph_id alone: two
        separate instances of the same figure in the scene (e.g. two "Genesis
        9" clones) can each legitimately carry their own injection of the same
        morph, onto two different DzObject::findModifier() targets, and each
        needs its own manifest row. Keying by morph_id alone would silently
        collapse the second instance's entry into the first's.

        This is what Task 3's sceneSaveStarting() hook walks to build the saved
        manifest (reading channel->getValue() lazily, per this class's header
        note on not hooking per-property change signals), and what Task 4's
        sceneLoaded() hook re-derives after scene reopen re-injects everything.
    **/
    const std::vector<InjectedMorphRecord>& injectedMorphs() const { return m_registry; }

    /**
        Default locations, read from the environment so a Daz Studio session can
        be pointed at a specific index without a rebuild. Falls back to the same
        variable names src/main.py already uses, then to the repo-root defaults.

          DAZ_MORPH_INDEX_DB  / MORPH_INDEX_DB_PATH -> "morph_index.db"
          DAZ_MORPH_CACHE_DIR / MORPH_CACHE_PATH    -> "morph_cache"
    **/
    static QString defaultMorphIndexDbPath();
    static QString defaultMorphCacheRoot();

    /**
        The single InjectorCore instance for this Daz Studio session, constructed
        against defaultMorphIndexDbPath()/defaultMorphCacheRoot() on first call.

        Session-lived so that injectedMorphs() accumulates across every caller
        within one run: the smoke-test action (MorphInjectorSmokeTestAction.cpp)
        and Task 3's sceneSaveStarting() hook (SceneManifestWriter.cpp) both need
        to see the SAME registry -- otherwise a save immediately after an
        injection would find nothing to write. Constructed lazily rather than at
        plugin load so it only ever runs on the GUI thread that first calls in
        (matching every other InjectorCore entry point's threading contract),
        with a function-local static for thread-safe-once initialization.
    **/
    static InjectorCore& sharedInstance();

private:
    //! Steps 1-6 for one already-fetched record, against m_targetNode.
    DzFloatProperty* injectRecord( const injector_core::MorphRecord& record );

    /**
        Records (or updates, if an entry for the same (node, morph_id) already
        exists) `record`'s entry in m_registry, against whatever node it
        actually landed on -- m_targetNode at the point of the call, which by
        the time injectRecord() reaches either of its return paths already
        reflects any daz-content-browser-e9y retargeting. Called from both
        injectRecord() branches (existing-modifier hit and newly-created), so a
        session's registry covers morphs this object merely rediscovered via
        DzObject::findModifier() as well as ones it actually created.
    **/
    void registerInjectedMorph( const injector_core::MorphRecord& record, DzFloatProperty* channel );

    /**
        find_by_id + injectRecord, with the re-entrancy guards applied.

        @param retargetToOwnFigure  when true (every *nested* injection: a
               dependencies_of edge or a formula operand), the fetched record's
               own target_figure decides which node it is injected onto, rather
               than inheriting the enclosing injection's m_targetNode
               (daz-content-browser-e9y). When false (the top-level
               injectMorph/injectMorphByGuid entry points), the caller-supplied
               node is authoritative and is never second-guessed.
    **/
    DzFloatProperty* injectById( int64_t morphId, bool retargetToOwnFigure = false );

    /**
        The live scene node a morph whose target_figure is `figureName` belongs
        on, or 0 if that figure is not in the scene.

        Fast path first: a same-figure dependency -- 99.96% of the precomputed
        dependency edges -- is answered by two string compares against the node
        already being injected onto, with no scene traversal at all. Only a
        genuine mismatch pays for the scene-wide lookup, which follows the same
        findNode -> findNodeByLabel fallback chain PropertySourceAdapter::
        resolveNode uses for its scene-global step.
    **/
    DzNode* resolveNodeForFigure( const QString& figureName ) const;

    //! Step 3+4: TmbReader -> DzMorphDeltas -> DzMorph -> addModifier.
    DzFloatProperty* createAndAttachMorph( const injector_core::MorphRecord& record );

    //! Step 6: formulas_json -> compileFormulaSet -> FormulaControllerBuilder.
    void attachFormulas( const injector_core::MorphRecord& record, DzFloatProperty* channel );

    //! MorphRecord::tmb_path (cache-root-relative) -> an absolute filesystem path.
    QString resolveTmbPath( const std::string& tmbPath ) const;

    //! dzApp->log() with this plugin's prefix; also records m_lastError.
    void logFailure( const QString& message );
    void logInfo( const QString& message ) const;

    // Non-copyable: owns a sqlite handle and hands out references to itself.
    InjectorCore( const InjectorCore& );
    InjectorCore& operator=( const InjectorCore& );

    std::unique_ptr<injector_core::MorphIndexReader> m_index;
    std::unique_ptr<PropertySourceAdapter> m_adapter;
    FormulaControllerBuilder m_builder;

    QString m_cacheRoot;
    QString m_lastError;

    /**
        The node the current injection (and every recursive sub-injection it
        triggers) attaches to. Set for the duration of a public injectMorph* call
        and restored afterwards; 0 outside one.

        A dependency closure is *usually* figure-local (Subsystem A's
        rebuild_dependencies() scopes pathless references by target_figure), but
        it is NOT guaranteed to be: a Genesis9Eyes morph can legitimately depend
        on a Genesis9 one, and injecting the latter's deltas onto the eye mesh is
        a correctness bug (daz-content-browser-e9y). So every nested injection
        re-derives its own node from the fetched record's target_figure and
        saves/restores this member around the sub-injection, exactly the way the
        public injectMorph() entry point does for its caller-supplied node.
    **/
    DzNode* m_targetNode;

    //! Nesting depth of injectById; 0 means "no injection in progress".
    int m_depth;

    /**
        Re-entrancy guard, part 1: morph_ids whose injection has *started* but
        whose value channel does not exist yet (i.e. between the duplicate check
        and a successful addModifier). Re-entering one of these means a genuine
        cycle at the delta-loading stage, which cannot be satisfied -- it is
        logged and broken by returning 0 rather than recursing again.
    **/
    std::set<int64_t> m_inFlight;

    /**
        Re-entrancy guard, part 2: morph_id -> live value channel for morphs
        already injected during the *current* top-level call. Published as soon
        as addModifier() succeeds -- i.e. before dependencies and formulas are
        wired -- so a diamond or a cycle that re-enters after that point gets the
        existing (possibly still partially-wired) property back and terminates.
        Daz Studio resolves the value itself once every controller is attached,
        so handing back a partially-wired property is safe.

        Cleared when the outermost call returns, so no DzNode/DzProperty pointer
        is ever cached across calls (they can be deleted by scene edits between
        them); cross-call idempotency is provided by the DzObject::findModifier
        duplicate check instead, which reads the live scene rather than a cache.
    **/
    std::map<int64_t, DzFloatProperty*> m_valueChannels;

    /**
        Session-lived record of every morph successfully injected so far,
        deduplicated on (node, morph_id) -- see injectedMorphs() above for why
        morph_id alone is not a safe key. Unlike m_valueChannels/m_inFlight,
        this survives past the outermost injectMorph* call returning -- it is
        what Task 3/Task 4's save and load hooks read.

        A std::vector rather than a map: entries are found by linear scan in
        registerInjectedMorph() (a session realistically accumulates dozens to
        low hundreds of injections, not a count where that scan is measurable
        next to the delta-loading/formula-compilation work each injection
        already does), and enumeration -- the whole point of this member --
        gets insertion order for free instead of morph_id order.

        Deliberately not kept fresh against scene edits (a node/modifier deleted
        after injection leaves a stale entry here): the acceptance criterion is
        enumerating what THIS OBJECT has injected, not re-validating the live
        scene on every access. A save-time consumer that dereferences a stale
        pointer is a pre-existing hazard shared with m_valueChannels, not one
        introduced here.
    **/
    std::vector<InjectedMorphRecord> m_registry;
};

}  // namespace daz_plugin
