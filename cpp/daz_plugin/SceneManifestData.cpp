#include "SceneManifestData.h"

#include "dzinfile.h"
#include "dzoutfile.h"

namespace daz_plugin {

SceneManifestData::SceneManifestData()
    : DzSceneData( dataName(), /*persistent=*/true )
{
}

SceneManifestData::~SceneManifestData()
{
}

void SceneManifestData::loadSection( DzInFile* file, short sectionID )
{
    if ( sectionID == kManifestSectionID )
    {
        QString json;
        file->readString( json );
        m_manifest = injector_core::fromJson( nlohmann::json::parse( json.toUtf8().constData() ) );
    }
    else
    {
        DzSceneData::loadSection( file, sectionID );
    }
}

void SceneManifestData::save( DzOutFile* file ) const
{
    DzSceneData::save( file );

    const std::string json = injector_core::toJson( m_manifest ).dump();
    file->writeStringSection( kManifestSectionID, QString::fromUtf8( json.c_str() ) );
}

}  // namespace daz_plugin
