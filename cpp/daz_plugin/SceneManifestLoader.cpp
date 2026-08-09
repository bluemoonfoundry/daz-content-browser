#include "SceneManifestLoader.h"

#include <QtCore/QByteArray>
#include <QtCore/QString>

#include "dzapp.h"
#include "dzfloatproperty.h"
#include "dznode.h"
#include "dzscene.h"

#include "InjectorCore.h"
#include "SceneManifestData.h"

namespace daz_plugin {

namespace {

//! std::string (UTF-8, as stored by SceneManifest) -> QString.
QString toQString( const std::string& s )
{
    return QString::fromUtf8( s.c_str(), static_cast<int>( s.size() ) );
}

void log( const QString& message )
{
    if ( dzApp )
    {
        dzApp->log( QString( "[daz_plugin] SceneManifestLoader: %1" ).arg( message ) );
    }
}

/*
    One manifest entry -> a live, re-injected morph at its saved value.

    Mirrors InjectorCore::injectById's own logging posture: a bad entry is
    logged and skipped, never thrown or aborted, so one stale/renamed morph
    can't take the rest of the manifest -- or the scene load itself -- down
    with it.
*/
void restoreEntry( InjectorCore& injector, const injector_core::InjectedMorphEntry& entry )
{
    const QString guid = toQString( entry.guid );
    const QString targetFigure = toQString( entry.target_figure );

    // Same lookup InjectorCore::resolveNodeForFigure() falls back to once its
    // in-injection fast path doesn't apply -- there is no enclosing injection
    // at scene-load time, so that fast path never applies here to begin with.
    DzNode* node = injector.resolveFigureNode( targetFigure );
    if ( !node )
    {
        log( QString( "manifest entry for guid '%1' targets figure '%2', which is not in the "
                      "scene; skipping it" )
                 .arg( guid )
                 .arg( targetFigure ) );
        return;
    }

    DzFloatProperty* channel = injector.injectMorphByGuid( node, guid );
    if ( !channel )
    {
        // injectMorphByGuid() already logged the specific cause via
        // InjectorCore::logFailure().
        log( QString( "could not re-inject guid '%1' onto '%2'; skipping it" )
                 .arg( guid )
                 .arg( node->getName() ) );
        return;
    }

    channel->setValue( static_cast<float>( entry.val ) );
}

}  // namespace

void SceneManifestLoader::restore()
{
    if ( !dzScene )
    {
        log( "dzScene is null; manifest restore skipped" );
        return;
    }

    SceneManifestData* data =
        qobject_cast<SceneManifestData*>( dzScene->findDataItem( SceneManifestData::dataName() ) );
    if ( !data )
    {
        // No manifest on this scene -- nothing was ever injected into it, or it
        // predates this feature. Not an error.
        return;
    }

    const injector_core::SceneManifest& manifest = data->manifest();
    if ( manifest.injected_morphs.empty() )
    {
        return;
    }

    InjectorCore& injector = InjectorCore::sharedInstance();
    if ( !injector.isOpen() )
    {
        log( QString( "morph index is not open; cannot restore %1 manifest entr%2: %3" )
                 .arg( static_cast<qulonglong>( manifest.injected_morphs.size() ) )
                 .arg( manifest.injected_morphs.size() == 1 ? "y" : "ies" )
                 .arg( injector.lastError() ) );
        return;
    }

    for ( size_t i = 0; i < manifest.injected_morphs.size(); ++i )
    {
        restoreEntry( injector, manifest.injected_morphs[i] );
    }
}

}  // namespace daz_plugin
