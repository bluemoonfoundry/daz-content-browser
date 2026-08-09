#include "InjectorCore.h"

#include <exception>
#include <vector>

#include <QtCore/QByteArray>
#include <QtCore/QDir>
#include <QtCore/QFileInfo>

#include "dzapp.h"
#include "dzerrorcodes.h"
#include "dzfloatproperty.h"
#include "dzmodifier.h"
#include "dzmorph.h"
#include "dzmorphdeltas.h"
#include "dznode.h"
#include "dznumericproperty.h"
#include "dzobject.h"
#include "dzscene.h"
#include "dztarray.h"
#include "dztypes.h"
#include "dzvec3.h"

#include "FormulaCompiler.h"
#include "FormulaIR.h"
#include "TmbReader.h"

namespace daz_plugin {

namespace {

//! std::string (UTF-8, as stored by Subsystem A) -> QString.
QString toQString( const std::string& s )
{
    return QString::fromUtf8( s.c_str(), static_cast<int>( s.size() ) );
}

//! QString -> std::string (UTF-8), for the injector_core APIs.
std::string toStdString( const QString& s )
{
    const QByteArray utf8 = s.toUtf8();
    return std::string( utf8.constData(), static_cast<size_t>( utf8.size() ) );
}

/*
    First non-empty of a list of environment variables, else `fallback`.
    qgetenv (QtCore/QtGlobal, pulled in by QString) is used rather than getenv so
    the value goes through Qt's own local-8-bit decoding, matching how the rest of
    the plugin handles paths.
*/
QString envOr( const char* primary, const char* secondary, const char* fallback )
{
    QString value = QString::fromLocal8Bit( qgetenv( primary ) );
    if ( value.isEmpty() )
    {
        value = QString::fromLocal8Bit( qgetenv( secondary ) );
    }
    if ( value.isEmpty() )
    {
        value = QString::fromLatin1( fallback );
    }
    return value;
}

}  // namespace

InjectorCore::InjectorCore( const QString& morphIndexDbPath, const QString& morphCacheRoot )
    : m_cacheRoot( morphCacheRoot ), m_targetNode( 0 ), m_depth( 0 )
{
    // MorphIndexReader's constructor throws if the file is missing or is not a
    // sqlite database. A plugin entry point must not propagate that into Daz
    // Studio, so it is converted into an !isOpen() object here.
    try
    {
        m_index.reset( new injector_core::MorphIndexReader( toStdString( morphIndexDbPath ) ) );
    }
    catch ( const std::exception& e )
    {
        m_index.reset();
        logFailure( QString( "could not open morph index '%1': %2" )
                        .arg( morphIndexDbPath )
                        .arg( QString::fromUtf8( e.what() ) ) );
        return;
    }

    // The adapter holds references to both the index and *this; both outlive it
    // because it is destroyed first (declared after m_index, before the members
    // it points at go away in ~InjectorCore).
    m_adapter.reset( new PropertySourceAdapter( *m_index, *this ) );
}

InjectorCore::~InjectorCore()
{
    // m_adapter must die before m_index (it holds a reference to it). unique_ptr
    // members are destroyed in reverse declaration order, and m_adapter is
    // declared after m_index, so this is already guaranteed -- made explicit
    // because the ordering is load-bearing, not incidental.
    m_adapter.reset();
    m_index.reset();
}

QString InjectorCore::defaultMorphIndexDbPath()
{
    return envOr( "DAZ_MORPH_INDEX_DB", "MORPH_INDEX_DB_PATH", "morph_index.db" );
}

QString InjectorCore::defaultMorphCacheRoot()
{
    return envOr( "DAZ_MORPH_CACHE_DIR", "MORPH_CACHE_PATH", "morph_cache" );
}

InjectorCore& InjectorCore::sharedInstance()
{
    static InjectorCore instance( defaultMorphIndexDbPath(), defaultMorphCacheRoot() );
    return instance;
}

void InjectorCore::logInfo( const QString& message ) const
{
    if ( dzApp )
    {
        dzApp->log( QString( "[daz_plugin] InjectorCore: %1" ).arg( message ) );
    }
}

void InjectorCore::logFailure( const QString& message )
{
    m_lastError = message;
    logInfo( message );
}

/*
    Subsystem A writes tmb_path relative to MORPH_CACHE_PATH
    (src/managers/morph_transpiler.py: tmb_rel_path =
    os.path.relpath(source, library_root) with the extension swapped), using
    native separators. An absolute value is honoured as-is so a differently
    configured index still works.
*/
QString InjectorCore::resolveTmbPath( const std::string& tmbPath ) const
{
    const QString relative = toQString( tmbPath );
    if ( relative.isEmpty() )
    {
        return QString();
    }
    if ( QFileInfo( relative ).isAbsolute() )
    {
        return QDir::toNativeSeparators( relative );
    }
    return QDir::toNativeSeparators( QDir( m_cacheRoot ).filePath( relative ) );
}

DzFloatProperty* InjectorCore::injectMorphByGuid( DzNode* targetNode, const QString& guid )
{
    m_lastError.clear();

    if ( !m_index )
    {
        logFailure( QString( "morph index is not open; cannot inject '%1'" ).arg( guid ) );
        return 0;
    }
    if ( !targetNode )
    {
        logFailure( QString( "no target node; cannot inject '%1'" ).arg( guid ) );
        return 0;
    }

    std::optional<injector_core::MorphRecord> record;
    try
    {
        record = m_index->find_by_guid( toStdString( guid ) );
    }
    catch ( const std::exception& e )
    {
        logFailure( QString( "morph index query failed for guid '%1': %2" )
                        .arg( guid )
                        .arg( QString::fromUtf8( e.what() ) ) );
        return 0;
    }

    if ( !record )
    {
        logFailure( QString( "no morph_index.db row for guid '%1'" ).arg( guid ) );
        return 0;
    }

    return injectMorph( targetNode, record->morph_id );
}

/*
    Which live node a morph belonging to `figureName` goes onto
    (daz-content-browser-e9y).

    FAST PATH FIRST, and it is the whole point of the ordering here: the
    overwhelming majority of dependencies are same-figure (a full corpus scan put
    the precomputed dependency edges at 99.96% same-figure), and those must not
    pay for a scene traversal on every edge -- the design's budget is <100ms per
    morph including its entire ERC chain (design section 1). Two string compares
    against the node already in hand answer them; only a real cross-figure
    mismatch falls through to the scene-wide lookup.

    Label is compared as well as name because target_figure comes from the
    content author's DSF, whereas the scene node carries whatever Daz Studio
    named/labelled it on load; the scene-wide step uses the same
    findNode -> findNodeByLabel fallback chain as
    PropertySourceAdapter::resolveNode's scene-global step, for one node-lookup
    convention across the plugin.

    Returns 0 only when the figure genuinely is not in the scene. Callers log and
    skip: silently falling back to the current node is precisely the bug this
    exists to prevent.
*/
DzNode* InjectorCore::resolveNodeForFigure( const QString& figureName ) const
{
    if ( figureName.isEmpty() )
    {
        // No figure recorded for this morph -- nothing to disagree with, so the
        // enclosing injection's node stands (this is also the pre-e9y behaviour).
        return m_targetNode;
    }

    if ( m_targetNode
         && ( m_targetNode->getName() == figureName || m_targetNode->getLabel() == figureName ) )
    {
        return m_targetNode;
    }

    if ( dzScene )
    {
        DzNode* node = dzScene->findNode( figureName );
        if ( !node )
        {
            node = dzScene->findNodeByLabel( figureName );
        }
        if ( node )
        {
            return node;
        }
    }

    return 0;
}

/*
    The one place m_targetNode is established. Saved/restored rather than simply
    assigned so that a caller who (legitimately) injects onto node B from inside
    a callback triggered by an injection onto node A cannot corrupt A's
    in-progress recursion.

    m_valueChannels is a *per-top-level-call* cache: cleared on the way out of
    the outermost call so no DzFloatProperty* survives into a later call, where a
    scene edit could have deleted it. Cross-call idempotency comes from the live
    DzObject::findModifier() check in injectRecord(), not from this cache.
*/
DzFloatProperty* InjectorCore::injectMorph( DzNode* targetNode, int64_t morphId )
{
    m_lastError.clear();

    if ( !m_index )
    {
        logFailure( QString( "morph index is not open; cannot inject morph_id %1" ).arg( morphId ) );
        return 0;
    }
    if ( !targetNode )
    {
        logFailure( QString( "no target node; cannot inject morph_id %1" ).arg( morphId ) );
        return 0;
    }

    DzNode* const savedNode = m_targetNode;
    m_targetNode = targetNode;

    // retargetToOwnFigure = false (the default): at the top level the caller has
    // named a node explicitly -- "inject this morph HERE" -- and that is
    // authoritative. Only the injections this one *induces* re-derive their node
    // from their own target_figure (daz-content-browser-e9y).
    DzFloatProperty* channel = injectById( morphId );

    m_targetNode = savedNode;
    if ( m_depth == 0 )
    {
        m_valueChannels.clear();
        m_inFlight.clear();
    }

    return channel;
}

DzNumericProperty* InjectorCore::ensureInjectedAndGetValueChannel( int64_t morphId )
{
    if ( !m_index || !m_targetNode )
    {
        // Called outside an injection: there is no node to attach to. Not an
        // error worth logging loudly -- PropertySourceAdapter treats a null
        // return as "not an indexed morph" and falls through to a live lookup.
        return 0;
    }

    // DzFloatProperty derives from DzNumericProperty (dzfloatproperty.h line 42),
    // so this widening is implicit and safe.
    //
    // retargetToOwnFigure = true: a formula operand can name a morph belonging to
    // a different figure than the one currently being injected onto (confirmed
    // live: FICairo_head_cbs_EyesLSideOut on Genesis9Eyes references
    // FICairo_head_bs_head, whose target_figure is Genesis9), and this path is
    // not gated by the precomputed dependency table at all
    // (daz-content-browser-e9y).
    return injectById( morphId, true );
}

DzFloatProperty* InjectorCore::injectById( int64_t morphId, bool retargetToOwnFigure )
{
    // Re-entrancy guard, part 2: already injected during this top-level call.
    // Checked first, because a morph published here is the *desired* answer for
    // a diamond dependency, not a failure.
    const std::map<int64_t, DzFloatProperty*>::const_iterator cached =
        m_valueChannels.find( morphId );
    if ( cached != m_valueChannels.end() )
    {
        return cached->second;
    }

    // Re-entrancy guard, part 1: re-entered before its value channel exists.
    // This is a true cycle through the delta-loading stage; break it.
    if ( m_inFlight.find( morphId ) != m_inFlight.end() )
    {
        logInfo( QString( "dependency cycle: morph_id %1 was re-entered before its value "
                          "channel existed; breaking the cycle (this operand will not resolve)" )
                     .arg( morphId ) );
        return 0;
    }

    std::optional<injector_core::MorphRecord> record;
    try
    {
        record = m_index->find_by_id( morphId );
    }
    catch ( const std::exception& e )
    {
        logFailure( QString( "morph index query failed for morph_id %1: %2" )
                        .arg( morphId )
                        .arg( QString::fromUtf8( e.what() ) ) );
        return 0;
    }

    if ( !record )
    {
        logFailure( QString( "no morph_index.db row for morph_id %1; skipping" ).arg( morphId ) );
        return 0;
    }

    // --- Node re-derivation (daz-content-browser-e9y) -------------------------
    // A morph's deltas are authored against ONE figure's mesh topology, so the
    // node it goes onto is a property of the record, not of whoever asked for
    // it. Same-figure records (nearly all of them) come back with m_targetNode
    // unchanged after two string compares -- see resolveNodeForFigure.
    DzNode* const savedNode = m_targetNode;
    if ( retargetToOwnFigure )
    {
        const QString figure = toQString( record->target_figure );
        DzNode* const owner = resolveNodeForFigure( figure );
        if ( !owner )
        {
            // Log and skip, never guess: injecting onto m_targetNode here is
            // exactly the bug -- the referenced figure's deltas would be loaded
            // onto a mesh they were not authored for.
            logInfo( QString( "morph_id %1 ('%2') belongs to figure '%3', which is not in the "
                              "scene; skipping it rather than injecting it onto '%4'" )
                         .arg( morphId )
                         .arg( toQString( record->name ) )
                         .arg( figure )
                         .arg( m_targetNode ? m_targetNode->getName() : QString( "<none>" ) ) );
            return 0;
        }
        if ( owner != m_targetNode )
        {
            logInfo( QString( "morph_id %1 ('%2') belongs to figure '%3', not to the node "
                              "currently being injected ('%4'); injecting it onto '%5' instead" )
                         .arg( morphId )
                         .arg( toQString( record->name ) )
                         .arg( figure )
                         .arg( m_targetNode ? m_targetNode->getName() : QString( "<none>" ) )
                         .arg( owner->getName() ) );
            m_targetNode = owner;
        }
    }

    ++m_depth;
    DzFloatProperty* channel = injectRecord( *record );
    --m_depth;

    m_targetNode = savedNode;
    return channel;
}

/*
    Populates/updates m_registry -- see the member comment (InjectorCore.h) for
    why this is session-lived rather than per-top-level-call like
    m_valueChannels. m_targetNode is used rather than record.target_figure's
    resolved node re-derived from scratch: by the time either injectRecord()
    caller reaches this point, m_targetNode already IS that resolved node
    (daz-content-browser-e9y retargeting happens in injectById() before
    injectRecord() is ever called).
*/
void InjectorCore::registerInjectedMorph( const injector_core::MorphRecord& record,
                                          DzFloatProperty* channel )
{
    for ( size_t i = 0; i < m_registry.size(); ++i )
    {
        if ( m_registry[i].morphId == record.morph_id && m_registry[i].node == m_targetNode )
        {
            m_registry[i].channel = channel;
            return;
        }
    }

    InjectedMorphRecord entry;
    entry.morphId = record.morph_id;
    entry.guid = toQString( record.guid );
    entry.targetFigure = toQString( record.target_figure );
    entry.node = m_targetNode;
    entry.channel = channel;
    m_registry.push_back( entry );
}

DzFloatProperty* InjectorCore::injectRecord( const injector_core::MorphRecord& record )
{
    const QString name = toQString( record.name );

    DzObject* object = m_targetNode->getObject();
    if ( !object )
    {
        logFailure( QString( "node '%1' has no DzObject; cannot inject '%2'" )
                        .arg( m_targetNode->getName() )
                        .arg( name ) );
        return 0;
    }

    // --- Step 1: duplicate-modifier check (design section 3.2 #1) ------------
    // Reads the live scene, so it is also what makes injection idempotent
    // across separate calls (and across a scene that already shipped the morph).
    if ( DzModifier* existing = object->findModifier( name ) )
    {
        DzMorph* existingMorph = qobject_cast<DzMorph*>( existing );
        if ( !existingMorph )
        {
            logFailure( QString( "node '%1' already has a non-morph modifier named '%2'; "
                                 "refusing to inject over it" )
                            .arg( m_targetNode->getName() )
                            .arg( name ) );
            return 0;
        }

        DzFloatProperty* channel = existingMorph->getValueChannel();
        if ( channel )
        {
            m_valueChannels[record.morph_id] = channel;
            registerInjectedMorph( record, channel );
        }
        return channel;
    }

    // --- Steps 3-4: deltas, DzMorph, addModifier -----------------------------
    m_inFlight.insert( record.morph_id );
    DzFloatProperty* channel = createAndAttachMorph( record );
    m_inFlight.erase( record.morph_id );

    if ( !channel )
    {
        return 0;
    }

    // Published *before* dependencies and formulas are wired: that is what makes
    // a re-entrant call for this same morph terminate with a usable property.
    m_valueChannels[record.morph_id] = channel;
    registerInjectedMorph( record, channel );

    // --- Step 5: dependencies first (design section 3.2 #5) ------------------
    // Depth-first and before step 6, so that every operand this morph's formula
    // references already has a live property by the time the formula is built.
    std::vector<int64_t> dependencies;
    try
    {
        dependencies = m_index->dependencies_of( record.morph_id );
    }
    catch ( const std::exception& e )
    {
        logFailure( QString( "dependency lookup failed for '%1': %2" )
                        .arg( name )
                        .arg( QString::fromUtf8( e.what() ) ) );
    }

    for ( size_t i = 0; i < dependencies.size(); ++i )
    {
        // retargetToOwnFigure = true: an edge in the dependency table is not
        // guaranteed same-figure (229 of 529,056 in the production index are
        // cross-figure), and a cross-figure dependency belongs on its own node
        // (daz-content-browser-e9y).
        if ( !injectById( dependencies[i], true ) )
        {
            // Not fatal for this morph: its formula may still resolve the operand
            // against something already live in the scene, and if it doesn't,
            // FormulaControllerBuilder skips just that formula.
            logInfo( QString( "dependency morph_id %1 of '%2' could not be injected; "
                              "its formula operands may fail to resolve" )
                         .arg( dependencies[i] )
                         .arg( name ) );
        }
    }

    // --- Step 6: formulas ----------------------------------------------------
    attachFormulas( record, channel );

    return channel;
}

/*
    Steps 3 and 4 of design section 3.2.

    The delta arrays are built first and the SDK objects only afterwards, so a
    bad/missing/truncated .tmb aborts before anything Daz-managed has been
    allocated -- no cleanup path, no chance of leaving a half-built modifier
    dangling on the object.
*/
DzFloatProperty* InjectorCore::createAndAttachMorph( const injector_core::MorphRecord& record )
{
    const QString name = toQString( record.name );
    const QString tmbPath = resolveTmbPath( record.tmb_path );
    if ( tmbPath.isEmpty() )
    {
        logFailure( QString( "morph '%1' has an empty tmb_path; skipping" ).arg( name ) );
        return 0;
    }

    // DzMorphDeltas::addDeltas is a *batch* API taking parallel arrays
    // (dzmorphdeltas.h:
    //   int addDeltas( const DzIntArray &indexes, const DzTArray<DzVec3> &deltas,
    //                  bool checkForDuplicates = true );
    // ), so the whole .tmb is converted in one pass rather than per-delta.
    DzIntArray indexes;
    DzTArray<DzVec3> values;
    int32_t vertexCount = 0;

    try
    {
        // TmbReader mmaps the file; the pointer/count view below points straight
        // into the mapped pages, which stay valid for the reader's lifetime --
        // hence everything that reads it happens inside this scope.
        const injector_core::TmbReader reader( toStdString( tmbPath ) );

        vertexCount = reader.vertexCount();
        const injector_core::TmbDelta* deltas = reader.deltaData();
        const size_t count = reader.deltaCount();

        indexes.preSize( static_cast<int>( count ) );
        values.preSize( static_cast<int>( count ) );
        for ( size_t i = 0; i < count; ++i )
        {
            indexes.append( static_cast<int>( deltas[i].vertex_index ) );
            values.append( DzVec3( deltas[i].dx, deltas[i].dy, deltas[i].dz ) );
        }
    }
    catch ( const std::exception& e )
    {
        // Covers both TmbFormatError (bad magic / truncation) and the plain
        // runtime_error the reader raises for OS-level open/map failures.
        logFailure( QString( "could not read deltas for '%1' from '%2': %3 -- skipping this morph" )
                        .arg( name )
                        .arg( tmbPath )
                        .arg( QString::fromUtf8( e.what() ) ) );
        return 0;
    }

    DzMorphDeltas* deltas = new DzMorphDeltas();
    // -1 is DSON's documented "unspecified" sentinel (see TmbReader.h); only a
    // real count is worth handing to the SDK.
    if ( vertexCount > 0 )
    {
        deltas->setVertCount( vertexCount );
    }
    // checkForDuplicates = false: a .tmb is transpiled from a single DSON morph
    // target, whose vertex indices are already unique, and the duplicate scan is
    // the expensive part of addDeltas for a six-figure delta count.
    deltas->addDeltas( indexes, values, false );

    // DzMorph( DzMorphDeltas* ) (dzmorph.h) takes the deltas and creates its own
    // value-channel property internally (private createProperties()); there is
    // deliberately no manual DzFloatProperty construction here.
    DzMorph* morph = new DzMorph( deltas );
    morph->setName( name );
    if ( !record.label.empty() )
    {
        morph->setLabel( toQString( record.label ) );
    }

    // DzError DzObject::addModifier( DzModifier*, int index = -1 ) (dzobject.h).
    const DzError error = m_targetNode->getObject()->addModifier( morph );
    if ( error != DZ_NO_ERROR )
    {
        logFailure( QString( "addModifier failed for '%1': %2" )
                        .arg( name )
                        .arg( getErrorMessage( error ) ) );
        // Never accepted by the object, so it is not Daz-managed and this is the
        // one and only place deleting it is correct (design section 7). The
        // deltas go with it -- DzMorph holds them via DzMorphDeltasPtr, a
        // DzTSharedPointer (dzmorphdeltas.h).
        delete morph;
        return 0;
    }

    // From here on `morph` is owned by the scene: never delete it.
    DzFloatProperty* channel = morph->getValueChannel();
    if ( !channel )
    {
        logFailure( QString( "morph '%1' was attached but exposes no value channel" ).arg( name ) );
        return 0;
    }

    // Range metadata from the index, so the injected channel behaves like the
    // one Daz Studio would have created from the .dsf itself
    // (dzfloatproperty.h: setMinMax( float, float ) / setIsClamped( bool )).
    channel->setMinMax( static_cast<float>( record.min_value ),
                        static_cast<float>( record.max_value ) );
    channel->setIsClamped( record.is_clamped );

    return channel;
}

/*
    Step 6 of design section 3.2.

    formulas_json is a JSON *array* of formula objects, each with "output",
    "operations", and an optional "stage" (see cpp/tests/fixtures/*.json).

    "output" NAMES THE PROPERTY THE ENTRY DRIVES, and is not always this morph's
    own value channel (daz-content-browser-2kv). Corrective JCMs -- the primary
    use case -- are self-output, but 38% of the formula-bearing morphs in the
    production index carry at least one entry pointing somewhere else entirely:
    "character/prop control dial" morphs (e.g. FICairo_head_bs_head,
    M113AntennaMorph) drive bone center_point/end_point channels and other
    morphs' values, and typically have ZERO self-output entries. Attaching those
    to this morph's own channel is silent misattachment -- a bone-origin offset
    folded into a dial's value.

    PHASE 1 (this code) is a defensive guard, not full support: each entry is
    classified via injector_core::isSelfOutput() and foreign ones are dropped
    INDIVIDUALLY, so a mixed morph still gets its self-output entries combined
    normally. An all-foreign morph degrades to exactly the formulas_json IS NULL
    path -- deltas injected, plain uncontrolled DzFloatProperty -- with one
    summary log line. PHASE 2 (resolving and attaching foreign outputs to their
    real targets) is deferred; such content is normally loaded natively by Daz
    Studio with its parent asset rather than injected standalone by this tool.

    The surviving entries are compiled and attached as ONE SET, not entry by entry
    (beads-w56). Multiple entries routinely drive the same output and combine
    via "stage" ("mult" = product, absent = implicit sum); handling them
    independently made each entry clobber the last, so a spline JCM gated by a
    "mult" corrective switch emitted only the gate and discarded its
    deformation. FormulaControllerBuilder chains them in array order, which is
    the order Daz evaluates them in.

    ResolutionContext carries record.target_figure -- NOT the node's name and not
    an empty string. Pathless operands ("Genesis8Female:#pJCMCloakBend_m90?value")
    are figure-local, and PropertySourceAdapter scopes its morph-index lookup by
    (target_figure, name) exactly the way Subsystem A's rebuild_dependencies()
    does. Leaving it empty makes every pathless morph operand silently fall
    through to a live-scene lookup instead of injecting the referenced morph.
*/
void InjectorCore::attachFormulas( const injector_core::MorphRecord& record,
                                   DzFloatProperty* channel )
{
    if ( !record.formulas_json.has_value() || !channel || !m_adapter )
    {
        return;
    }

    const QString name = toQString( record.name );

    nlohmann::json parsed;
    try
    {
        parsed = nlohmann::json::parse( *record.formulas_json );
    }
    catch ( const std::exception& e )
    {
        logFailure( QString( "formulas_json for '%1' is not valid JSON: %2 -- morph injected "
                             "without its formulas" )
                        .arg( name )
                        .arg( QString::fromUtf8( e.what() ) ) );
        return;
    }

    if ( !parsed.is_array() )
    {
        logFailure( QString( "formulas_json for '%1' is not a JSON array -- morph injected "
                             "without its formulas" )
                        .arg( name ) );
        return;
    }

    PropertySourceAdapter* adapter = m_adapter.get();
    const ResolutionContext context( m_targetNode, toQString( record.target_figure ) );

    // Captured by value: resolution can re-enter this class (adapter ->
    // ensureInjectedAndGetValueChannel -> injectById -> attachFormulas for
    // another morph), and each nesting level needs its *own* context.
    const OperandResolver resolver =
        [adapter, context]( const std::string& operand ) -> DzNumericProperty*
        {
            return adapter->resolve( operand, context );
        };

    std::vector<injector_core::CompiledFormula> compiled;
    try
    {
        compiled = injector_core::compileFormulaSet( parsed );
    }
    catch ( const std::exception& e )
    {
        // Compilation is atomic across the array: a partially-compiled set would
        // silently change the morph's arithmetic (e.g. a dropped "mult" gate
        // leaves the morph firing at full strength), so the morph is injected
        // with no formulas at all rather than with wrong ones.
        logFailure( QString( "could not compile the formulas on '%1': %2 -- morph injected "
                             "without its formulas" )
                        .arg( name )
                        .arg( QString::fromUtf8( e.what() ) ) );
        return;
    }

    // --- Output classification (daz-content-browser-2kv) ---------------------
    // Done AFTER compilation, not before, so that compileFormulaSet's atomicity
    // still covers the whole array: an entry with a broken "operations" shape is
    // a red flag about the row as a whole and must still abandon the morph's
    // formulas, even if that entry would have been skipped as foreign anyway.
    std::vector<injector_core::CompiledFormula> selfOutput;
    selfOutput.reserve( compiled.size() );
    QString firstForeignTarget;
    for ( size_t i = 0; i < compiled.size(); ++i )
    {
        if ( injector_core::isSelfOutput( compiled[i].output, record.name ) )
        {
            selfOutput.push_back( compiled[i] );
            continue;
        }
        if ( firstForeignTarget.isEmpty() )
        {
            // Kept for the summary line below: one representative target beats
            // either N log lines (these morphs carry up to hundreds of entries)
            // or a bare count with nothing to grep the content back from.
            firstForeignTarget = compiled[i].output.raw.empty()
                                     ? QString( "<no \"output\" field>" )
                                     : toQString( compiled[i].output.raw );
        }
    }

    const size_t skipped = compiled.size() - selfOutput.size();
    if ( skipped > 0 )
    {
        logInfo( QString( "'%1': skipping %2 of %3 formulas_json entries whose \"output\" "
                          "targets a different property than this morph's own value channel "
                          "(first such target: '%4') -- driving foreign outputs is out of "
                          "scope (daz-content-browser-2kv phase 2); %5" )
                     .arg( name )
                     .arg( static_cast<qulonglong>( skipped ) )
                     .arg( static_cast<qulonglong>( compiled.size() ) )
                     .arg( firstForeignTarget )
                     .arg( selfOutput.empty()
                               ? QString( "the morph is injected with its deltas but no "
                                          "formulas, as an uncontrolled float" )
                               : QString( "the remaining %1 self-output entries are still "
                                          "attached" )
                                     .arg( static_cast<qulonglong>( selfOutput.size() ) ) ) );
    }

    if ( selfOutput.empty() )
    {
        // Nothing to attach. Deliberately NOT routed through attachFormulaSet
        // (which no-ops on an empty set anyway) so the "injected but static"
        // failure log below can't fire on what is an expected, benign outcome.
        return;
    }

    if ( !m_builder.attachFormulaSet( selfOutput, channel, resolver ) )
    {
        // FormulaControllerBuilder already logged the specific cause, and left
        // the channel untouched.
        logInfo( QString( "the formulas on '%1' could not be attached; the morph is "
                          "injected but static" )
                     .arg( name ) );
    }
}

}  // namespace daz_plugin
