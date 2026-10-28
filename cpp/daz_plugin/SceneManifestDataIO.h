/**********************************************************************
    SceneManifestDataIO -- the DSON (.duf, JSON) bridge for SceneManifestData
    (SceneManifestData.h).

    CORRECTION (found via live testing against a real Daz Studio 4.5 session,
    daz-content-browser-jhq.3): SceneManifestData's own loadSection()/save()
    pair (DzInFile/DzOutFile, dzinfile.h/dzoutfile.h) is NEVER invoked when
    saving/loading a modern .duf scene. Saving a scene with a
    SceneManifestData attached via DzScene::addDataItem() and inspecting the
    resulting .duf (gunzip + JSON parse) showed no trace of it: DzOutFile's
    binary section format is only consulted for the legacy binary .daz scene
    format. The real DSON extension point -- confirmed by the SDK's own
    samples/saving/customscenedata sample -- is a separate DzExtraSceneDataIO
    (dzassetextraobjectio.h) registered via
    DZ_PLUGIN_REGISTER_SCENEDATA_EXTRA_OBJECT_IO (dzplugin.h), which bridges
    through IDzJsonIO (idzjsonio.h) instead. This class is that bridge.
    SceneManifestData's loadSection()/save() are left in place (harmless,
    possibly useful if this plugin ever also supports the legacy format) but
    are no longer load-bearing for the acceptance criterion.

    The whole manifest is written/read as a single JSON-string member
    ("manifest_json", itself injector_core::toJson()'s output re-dumped to a
    string) rather than as native nested JSON members: IDzJsonIO's addMember
    overloads only cover scalar leaf types (dzassetjsonitem.h has no
    "start a raw nested JSON value" primitive), so round-tripping the
    manifest's actual array-of-objects shape through it directly would mean
    reimplementing injector_core::toJson()/fromJson() a second time against
    IDzJsonIO's API. A single string member preserves the existing,
    already-tested (de)serialization exactly.

    Registered together with SceneManifestData under the tag "scene_manifest"
    (-> DSON type "studio/scene_data/scene_manifest") in PluginMain.cpp.
**********************************************************************/

#pragma once

#include "dzassetextraobjectio.h"

class DzAssetJsonItem;
class DzAssetJsonObject;
class DzFileIOSettings;
class DzSceneData;
class IDzJsonIO;

namespace daz_plugin {

struct SceneManifestReadContext;

class SceneManifestDataIO : public DzExtraSceneDataIO
{
    Q_OBJECT
public:
    SceneManifestDataIO();
    virtual ~SceneManifestDataIO();

    //! DzScene's single SceneManifestData instance, creating (and attaching via
    //! DzScene::addDataItem()) one if none exists yet -- the read path always
    //! hits this case, since scene loading clears/rebuilds the scene before this
    //! extra data is applied; the write path never does, since
    //! SceneManifestWriter (SceneManifestWriter.h) always creates/attaches the
    //! instance itself before this is called.
    virtual DzSceneData* createDataItem( const DzFileIOSettings* opts ) const;

    //! Skips writing the "scene_manifest" extra entirely for a manifest with no
    //! injected morphs, rather than saving an empty payload every time.
    virtual bool shouldWrite( QObject* object, const DzFileIOSettings* opts ) const;

    virtual DzError writeExtraInstance( QObject* object, IDzJsonIO* io,
                                        const DzFileIOSettings* opts ) const;

    virtual DzAssetJsonObject* startInstanceRead( DzAssetJsonItem* parentItem );
    virtual DzError applyInstanceToObject( QObject* object, const DzFileIOSettings* opts ) const;

private:
    //! Owned; set by startInstanceRead(), consumed by applyInstanceToObject(),
    //! freed at destruction -- same lifecycle the SDK's customscenedata sample
    //! uses for its own read context.
    SceneManifestReadContext* m_context;
};

}  // namespace daz_plugin
