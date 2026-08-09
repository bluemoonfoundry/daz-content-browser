#include "MorphInjectorSmokeTestAction.h"

#include <vector>

#include <QtCore/QString>
#include <QtGui/QMessageBox>

#include "dzapp.h"
#include "dzfloatproperty.h"
#include "dznode.h"
#include "dzscene.h"

#include "InjectorCore.h"
#include "SceneManifestLoader.h"
#include "SceneManifestWriter.h"

namespace {

/*
    The morph this action injects when DAZ_MORPH_SMOKE_TEST_GUID is unset.

    Chosen from cpp/tests/fixtures/morph_index.db (morph_id 3027), which
    build_fixture_db.py extracted from the real repo-root morph_index.db. It is
    the single best default for a *first* smoke test because it exercises the
    entire design section 3.2 sequence in one click rather than just the
    delta-loading half:
      - it has a non-null formulas_json  -> steps 6 (compile + attach) run
      - it has two dependency edges (-> morph_ids 3074 and 3026) -> step 5's
        recursion runs, and both dependencies are themselves real indexed morphs
        with .tmb files, so the recursive path is genuinely taken
      - 6179 deltas over a 244823-vertex mesh -> a realistic batch addDeltas load

    Its target_figure is "AJC Energy Sportswear Leggings_244823", i.e. it belongs
    to a *clothing* item, not a base figure. The verifier therefore has to have
    the AJC Energy Sportswear Leggings loaded and selected for this default to do
    anything; if that outfit is not available, set DAZ_MORPH_SMOKE_TEST_GUID to
    any guid from the local morph_index.db that matches a figure actually in the
    scene. A base-figure morph would be a better long-term default -- picking one
    needs a look at the full local index, which is a manual-verification-time
    call, not a build-time one (see beads-2mw.8 notes).
*/
const char* const kDefaultSmokeTestGuid =
    "/data/adeilsonjc/AJC%20Energy%20Sportswear%20Outfit/AJC%20Energy%20Sportswear%20Leggings/"
    "Morphs/adeilsonjc/Base/BaseFeminine_body_cbs_thigh_x115n_l.dsf";

QString smokeTestGuid()
{
    const QString fromEnv = QString::fromLocal8Bit( qgetenv( "DAZ_MORPH_SMOKE_TEST_GUID" ) );
    return fromEnv.isEmpty() ? QString::fromLatin1( kDefaultSmokeTestGuid ) : fromEnv;
}

//! One line per registered morph, for the manual-verification report below.
QString describeRegistry( const daz_plugin::InjectorCore& injector )
{
    const std::vector<daz_plugin::InjectorCore::InjectedMorphRecord>& registry =
        injector.injectedMorphs();
    if ( registry.empty() )
    {
        return QObject::tr( "(registry is empty)" );
    }

    QString text;
    for ( size_t i = 0; i < registry.size(); ++i )
    {
        const daz_plugin::InjectorCore::InjectedMorphRecord& entry = registry[i];
        text += QString( "  morph_id %1 on '%2': val=%3\n" )
                    .arg( entry.morphId )
                    .arg( entry.node ? entry.node->getName() : QString( "<null>" ) )
                    .arg( entry.channel ? entry.channel->getValue() : 0.0 );
    }
    return text;
}

/*
    DAZ_MORPH_SMOKE_TEST_SILENT=1 skips the QMessageBox below and logs only.

    Exists so this action can be triggered from a script (e.g. daz-script-server,
    for scripted verification of Tasks 3/4's save/reload round trip) without
    popping a modal dialog: QMessageBox::exec() runs its own nested event loop
    on the GUI thread, and a script-server request that triggers this action is
    itself blocking that same GUI thread waiting for the trigger() call to
    return -- nothing can click "OK" on its behalf, so the whole app hangs until
    a human physically dismisses it. Interactive menu use is unaffected: this
    only matters for callers who both (a) invoke the action off-thread from
    Daz Studio's own input handling and (b) cannot dismiss a dialog themselves.
*/
bool isSilentMode()
{
    return QString::fromLocal8Bit( qgetenv( "DAZ_MORPH_SMOKE_TEST_SILENT" ) ) == "1";
}

void report( const QString& message, bool ok )
{
    if ( dzApp )
    {
        dzApp->log( QString( "[daz_plugin] smoke test: %1" ).arg( message ) );
    }

    if ( isSilentMode() )
    {
        return;
    }

    QWidget* parent = dzApp ? dzApp->getDialogParent() : 0;
    if ( ok )
    {
        QMessageBox::information( parent, QObject::tr( "Morph Injector" ), message,
                                  QMessageBox::Ok );
    }
    else
    {
        QMessageBox::warning( parent, QObject::tr( "Morph Injector" ), message, QMessageBox::Ok );
    }
}

}  // namespace

DzMorphInjectorSmokeTestAction::DzMorphInjectorSmokeTestAction()
    : DzAction( tr( "Inject Test Morph" ),
                tr( "Injects one hardcoded morph onto the selected node, to smoke-test the "
                    "just-in-time morph injector." ) )
{
    // Registers the action with Daz Studio's help / interactive-lesson systems,
    // exactly as the SDK's own sample does
    // (samples/interface/AFirstPlugin/afirstpluginaction.cpp).
    setObjectName( DzMorphInjectorSmokeTestAction::metaObject()->className() );

    // Subsystem C Task 4 (daz-content-browser-jhq.4): re-inject a saved
    // scene's manifest as soon as it finishes loading. Registered DzAction
    // subclasses are constructed once, at plugin load, by which point dzScene
    // already exists -- so this connection is made unconditionally rather than
    // guarded on dzScene being non-null (SceneManifestWriter.cpp's SDK-global
    // dzScene pointer is always valid by the time any DzAction is constructed).
    connect( dzScene, SIGNAL( sceneLoaded() ), this, SLOT( onSceneLoaded() ) );
}

void DzMorphInjectorSmokeTestAction::onSceneLoaded()
{
    daz_plugin::SceneManifestLoader::restore();
}

void DzMorphInjectorSmokeTestAction::executeAction()
{
    // DzScene::getPrimarySelection() (dzscene.h) -- the node the user has
    // selected in the Scene pane.
    DzNode* node = dzScene ? dzScene->getPrimarySelection() : 0;
    if ( !node )
    {
        report( tr( "No node is selected. Select a figure or clothing item in the Scene pane "
                    "and run this again." ),
                false );
        return;
    }

    const QString guid = smokeTestGuid();

    // Shared across clicks (InjectorCore::sharedInstance()) rather than
    // constructed per call: beads-jhq.2's registry is session-lived by design,
    // and Task 3's SceneManifestWriter::refresh() (SceneManifestWriter.cpp)
    // needs to see the same registry this action injects into, not a copy
    // local to this action.
    daz_plugin::InjectorCore& injector = daz_plugin::InjectorCore::sharedInstance();
    if ( !injector.isOpen() )
    {
        report( tr( "Could not open the morph index: %1" ).arg( injector.lastError() ), false );
        return;
    }

    DzFloatProperty* channel = injector.injectMorphByGuid( node, guid );
    if ( !channel )
    {
        report( tr( "Injection of '%1' onto '%2' failed: %3 (see the Daz Studio log for the "
                    "full trace)" )
                    .arg( guid )
                    .arg( node->getName() )
                    .arg( injector.lastError() ),
                false );
        return;
    }

    // Drive the channel to its maximum so the vertex deltas are visibly applied
    // straight away -- an injected morph sitting at 0 looks identical to no
    // injection at all, which is the single most confusing thing a manual
    // verifier can be shown. ONLY when there's no attached formula controller,
    // though: a formula-driven channel's live value is computed by threading
    // this raw setValue() as the seed through its controller chain (confirmed
    // live during beads-w56's re-verification -- setting the seed to 1 here
    // silently added a spurious "+1 * (last multiply-stage operand)" to every
    // subsequent chain evaluation, e.g. turning a correct A*B result into
    // (1+A)*B). For a formula-driven channel, leaving the seed at its default
    // (0) is what makes the chain's output actually reflect the formula.
    if ( channel->getNumControllers() == 0 )
    {
        channel->setValue( channel->getMax() );
    }

    // Task 3 (daz-content-browser-jhq.3): keep the saved-scene manifest
    // current the moment the registry changes, rather than trying to catch it
    // at save time -- see SceneManifestWriter.h for why no DzScene save signal
    // turned out to be a reliable hook for this in a real Daz Studio session.
    daz_plugin::SceneManifestWriter::refresh();

    report( tr( "Injected '%1' onto '%2' as morph channel '%3' and set it to %4.\n\n"
                "InjectorCore registry now has %5 entr%6:\n%7" )
                .arg( guid )
                .arg( node->getName() )
                .arg( channel->getName() )
                .arg( channel->getValue() )
                .arg( injector.injectedMorphs().size() )
                .arg( injector.injectedMorphs().size() == 1 ? tr( "y" ) : tr( "ies" ) )
                .arg( describeRegistry( injector ) ),
            true );
}
