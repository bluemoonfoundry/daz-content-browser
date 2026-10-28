#include "SceneManifestDataIO.h"

#include <exception>

#include <QtCore/QByteArray>

#include "dzapp.h"
#include "dzassetjsonitem.h"
#include "dzerrorcodes.h"
#include "dzscene.h"
#include "idzjsonio.h"

#include "SceneManifestData.h"

namespace daz_plugin {

namespace {

void log( const QString& message )
{
    if ( dzApp )
    {
        dzApp->log( QString( "[daz_plugin] SceneManifestDataIO: %1" ).arg( message ) );
    }
}

}  // namespace

//! Holds the "manifest_json" string as it is parsed, between startInstanceRead()
//! and applyInstanceToObject() -- same shape as the SDK customscenedata sample's
//! own MyReadContext (myscenemodel.h).
struct SceneManifestReadContext
{
    explicit SceneManifestReadContext( DzAssetFile& file ) : m_file( file ) {}

    DzAssetFile& m_file;
    QString m_manifestJson;
};

namespace {

//! Captures the one "manifest_json" member as the JSON parser walks this
//! extra's instance object.
class ReadSceneManifestData : public DzAssetJsonObject
{
public:
    explicit ReadSceneManifestData( SceneManifestReadContext* context )
        : DzAssetJsonObject( context->m_file ), m_context( context )
    {
    }

    virtual bool addMember( const QString& name, const QString& val )
    {
        if ( name == "manifest_json" )
        {
            m_context->m_manifestJson = val;
            return true;
        }
        return DzAssetJsonObject::addMember( name, val );
    }

    SceneManifestReadContext* m_context;
};

}  // namespace

SceneManifestDataIO::SceneManifestDataIO()
    : m_context( 0 )
{
}

SceneManifestDataIO::~SceneManifestDataIO()
{
    delete m_context;
}

DzSceneData* SceneManifestDataIO::createDataItem( const DzFileIOSettings* opts ) const
{
    Q_UNUSED( opts );

    if ( !dzScene )
    {
        return 0;
    }

    SceneManifestData* item =
        qobject_cast<SceneManifestData*>( dzScene->findDataItem( SceneManifestData::dataName() ) );
    if ( !item )
    {
        item = new SceneManifestData();
        dzScene->addDataItem( item );
        log( "createDataItem(): no existing SceneManifestData, created a new (empty) one" );
    }
    else
    {
        log( QString( "createDataItem(): found existing SceneManifestData with %1 entries" )
                 .arg( item->manifest().injected_morphs.size() ) );
    }
    return item;
}

bool SceneManifestDataIO::shouldWrite( QObject* object, const DzFileIOSettings* opts ) const
{
    Q_UNUSED( opts );

    SceneManifestData* item = qobject_cast<SceneManifestData*>( object );
    const bool result = item && !item->manifest().injected_morphs.empty();
    log( QString( "shouldWrite(): %1" ).arg( result ? "yes" : "no" ) );
    return result;
}

DzError SceneManifestDataIO::writeExtraInstance( QObject* object, IDzJsonIO* io,
                                                  const DzFileIOSettings* opts ) const
{
    Q_UNUSED( opts );

    SceneManifestData* item = qobject_cast<SceneManifestData*>( object );
    if ( !item )
    {
        return DZ_NO_ERROR;
    }

    const std::string json = injector_core::toJson( item->manifest() ).dump();
    io->addMember( "manifest_json", QString::fromUtf8( json.c_str() ) );
    return DZ_NO_ERROR;
}

DzAssetJsonObject* SceneManifestDataIO::startInstanceRead( DzAssetJsonItem* parentItem )
{
    delete m_context;
    m_context = new SceneManifestReadContext( parentItem->getFile() );
    return new ReadSceneManifestData( m_context );
}

DzError SceneManifestDataIO::applyInstanceToObject( QObject* object, const DzFileIOSettings* opts ) const
{
    Q_UNUSED( opts );

    SceneManifestData* item = qobject_cast<SceneManifestData*>( object );
    if ( !item || !m_context || m_context->m_manifestJson.isEmpty() )
    {
        return DZ_NO_ERROR;
    }

    try
    {
        item->setManifest( injector_core::fromJson(
            nlohmann::json::parse( m_context->m_manifestJson.toUtf8().constData() ) ) );
        log( QString( "applyInstanceToObject(): parsed manifest with %1 entries" )
                 .arg( item->manifest().injected_morphs.size() ) );
    }
    catch ( const std::exception& e )
    {
        log( QString( "failed to parse saved manifest_json: %1" ).arg( QString::fromUtf8( e.what() ) ) );
    }

    return DZ_NO_ERROR;
}

}  // namespace daz_plugin
