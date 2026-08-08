#include "MorphInjectorSmokeTestAction.h"

#include <QtCore/QString>
#include <QtGui/QMessageBox>

#include "dzapp.h"
#include "dzfloatproperty.h"
#include "dznode.h"
#include "dzscene.h"

#include "InjectorCore.h"

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

void report( const QString& message, bool ok )
{
    if ( dzApp )
    {
        dzApp->log( QString( "[daz_plugin] smoke test: %1" ).arg( message ) );
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
    const QString dbPath = daz_plugin::InjectorCore::defaultMorphIndexDbPath();
    const QString cacheRoot = daz_plugin::InjectorCore::defaultMorphCacheRoot();

    // Constructed per invocation: the smoke test is a one-shot, and a fresh
    // read-only sqlite handle costs nothing next to the injection itself.
    // Subsystem D will want a session-lived instance instead.
    daz_plugin::InjectorCore injector( dbPath, cacheRoot );
    if ( !injector.isOpen() )
    {
        report( tr( "Could not open the morph index at '%1': %2" )
                    .arg( dbPath )
                    .arg( injector.lastError() ),
                false );
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
    // verifier can be shown.
    channel->setValue( channel->getMax() );

    report( tr( "Injected '%1' onto '%2' as morph channel '%3' and set it to %4." )
                .arg( guid )
                .arg( node->getName() )
                .arg( channel->getName() )
                .arg( channel->getValue() ),
            true );
}
