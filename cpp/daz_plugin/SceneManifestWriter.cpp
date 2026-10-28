#include "SceneManifestWriter.h"

#include <vector>

#include <QtCore/QByteArray>

#include "dzapp.h"
#include "dzerrorcodes.h"
#include "dzfloatproperty.h"
#include "dzscene.h"

#include "InjectorCore.h"
#include "SceneManifestData.h"

namespace daz_plugin {

namespace {

std::string toStdString( const QString& s )
{
    const QByteArray utf8 = s.toUtf8();
    return std::string( utf8.constData(), static_cast<size_t>( utf8.size() ) );
}

void log( const QString& message )
{
    if ( dzApp )
    {
        dzApp->log( QString( "[daz_plugin] SceneManifestWriter: %1" ).arg( message ) );
    }
}

/*
    InjectorCore::injectedMorphs() -> injector_core::SceneManifest.

    Skips any record whose channel is null: InjectorCore.h documents the
    registry as "deliberately not kept fresh against scene edits", so a node or
    modifier deleted after injection can leave a stale entry here. There is
    nothing meaningful to persist for one (no live value to read), so it is
    logged and left out of the saved manifest rather than written with a
    fabricated value.
*/
injector_core::SceneManifest buildManifest( const InjectorCore& injector )
{
    injector_core::SceneManifest manifest;

    const std::vector<InjectorCore::InjectedMorphRecord>& registry = injector.injectedMorphs();
    manifest.injected_morphs.reserve( registry.size() );

    for ( size_t i = 0; i < registry.size(); ++i )
    {
        const InjectorCore::InjectedMorphRecord& record = registry[i];
        if ( !record.channel )
        {
            log( QString( "skipping stale registry entry for guid '%1' (no live value channel)" )
                     .arg( record.guid ) );
            continue;
        }

        injector_core::InjectedMorphEntry entry;
        entry.guid = toStdString( record.guid );
        entry.target_figure = toStdString( record.targetFigure );
        entry.val = record.channel->getValue();
        manifest.injected_morphs.push_back( entry );
    }

    return manifest;
}

}  // namespace

void SceneManifestWriter::refresh()
{
    if ( !dzScene )
    {
        log( "dzScene is null; manifest refresh skipped" );
        return;
    }

    InjectorCore& injector = InjectorCore::sharedInstance();
    const injector_core::SceneManifest manifest = buildManifest( injector );

    SceneManifestData* existing =
        qobject_cast<SceneManifestData*>( dzScene->findDataItem( SceneManifestData::dataName() ) );
    if ( existing )
    {
        existing->setManifest( manifest );
        return;
    }

    SceneManifestData* item = new SceneManifestData();
    item->setManifest( manifest );

    const DzError err = dzScene->addDataItem( item );
    if ( err != DZ_NO_ERROR )
    {
        log( QString( "addDataItem() failed with error %1; manifest was NOT attached" ).arg( err ) );
        // Never successfully handed over to the scene -- ours to delete, per
        // InjectorCore.h's MEMORY posture (same rule, same reasoning, applied
        // here to a DzSceneData instead of a DzMorph/DzMorphDeltas).
        delete item;
    }
}

}  // namespace daz_plugin
