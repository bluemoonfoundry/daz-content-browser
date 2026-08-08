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
      6. formulas_json (if non-null) -> injector_core::compileFormula ->
         FormulaControllerBuilder::attachFormula onto the value channel.

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

        Only meaningful during an injection -- it injects onto whatever node the
        enclosing injectMorph() call is targeting. Returns 0 if called outside
        one (no target node is established).
    **/
    virtual DzNumericProperty* ensureInjectedAndGetValueChannel( int64_t morphId );

    /**
        Default locations, read from the environment so a Daz Studio session can
        be pointed at a specific index without a rebuild. Falls back to the same
        variable names src/main.py already uses, then to the repo-root defaults.

          DAZ_MORPH_INDEX_DB  / MORPH_INDEX_DB_PATH -> "morph_index.db"
          DAZ_MORPH_CACHE_DIR / MORPH_CACHE_PATH    -> "morph_cache"
    **/
    static QString defaultMorphIndexDbPath();
    static QString defaultMorphCacheRoot();

private:
    //! Steps 1-6 for one already-fetched record, against m_targetNode.
    DzFloatProperty* injectRecord( const injector_core::MorphRecord& record );

    //! find_by_id + injectRecord, with the re-entrancy guards applied.
    DzFloatProperty* injectById( int64_t morphId );

    //! Step 3+4: TmbReader -> DzMorphDeltas -> DzMorph -> addModifier.
    DzFloatProperty* createAndAttachMorph( const injector_core::MorphRecord& record );

    //! Step 6: formulas_json -> compileFormula -> FormulaControllerBuilder.
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

        Deliberately a single node rather than a per-morph target: a morph's
        dependency closure is figure-local by construction (Subsystem A's
        rebuild_dependencies() scopes pathless references by target_figure), so
        every morph in one closure belongs on the same object.
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
};

}  // namespace daz_plugin
