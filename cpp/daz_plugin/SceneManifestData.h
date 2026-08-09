/**********************************************************************
    SceneManifestData -- DzSceneData subclass that persists a
    injector_core::SceneManifest (SceneManifest.h) on the live DzScene, so a
    saved .duf carries which morphs InjectorCore has injected without its
    (never-referenced) .dsf source files (parent spec section 4.3.1; see the
    daz-content-browser-jhq epic notes).

    CORRECTION (found via live testing against the running dzScene through
    daz-script-server, daz-content-browser-jhq.1): the epic notes' claim that
    "DzScene inherits DzElement's data-item mechanism" does not hold. Checked
    against the real header: DzScene derives DzBase + DzSceneAsset (dzscene.h)
    -- NOT DzElement. DzScene has its OWN addDataItem(DzSceneData*)/
    findDataItem() pair (dzscene.h), entirely separate from
    DzElement::addDataItem(DzElementData*)/findDataItem() (dzelement.h), which
    only applies to real DzElement subclasses (DzNode, DzModifier, ...). A
    first attempt subclassing DzElementData compiled fine and
    Scene.addDataItem(item) even returned DZ_NO_ERROR, but the item was
    silently never stored (wrong overload/type) and Scene.findDataItem()
    never saw it -- caught by manually round-tripping through a live Daz
    Studio instance, not by the header read alone.

    Subclasses DzSceneData directly rather than DzSimpleSceneData:
    DzSimpleSceneData layers its own DzSettings-based key/value persistence on
    top, which this class has no use for -- the whole manifest already
    serializes losslessly to one JSON string via injector_core::toJson, so a
    single writeStringSection/readString pair is sufficient. loadSection()/
    save() are virtual on DzBase (dzbase.h) already, inherited down through
    DzCustomData/DzSceneData, so overriding them here needs no additional
    plumbing. Unlike DzElementData, DzSceneData has no duplicate() virtual --
    scene-level data isn't per-element, so there is nothing to duplicate.

    Attach via DzScene::addDataItem() / retrieve via
    DzScene::findDataItem(SceneManifestData::dataName()) (dzscene.h). C3/C4
    (sceneSaveStarting()/sceneLoaded() hooks) are the intended callers.
**********************************************************************/

#pragma once

#include <QtCore/QString>

#include "dzcustomdata.h"

#include "SceneManifest.h"

class DzInFile;
class DzOutFile;

namespace daz_plugin {

class SceneManifestData : public DzSceneData
{
    Q_OBJECT
public:
    SceneManifestData();
    virtual ~SceneManifestData();

    //! The name passed to addDataItem()/findDataItem() to attach/retrieve this data.
    static QString dataName() { return "JitLoaderManifest"; }

    const injector_core::SceneManifest& manifest() const { return m_manifest; }
    void setManifest( const injector_core::SceneManifest& manifest ) { m_manifest = manifest; }

    //! DzBase (dzbase.h), via DzCustomData/DzSceneData.
    virtual void loadSection( DzInFile* file, short sectionID );
    virtual void save( DzOutFile* file ) const;

private:
    // The one section this class writes/reads; anything else is passed to the base
    // class per the standard loadSection dispatch pattern (see e.g. the SDK's
    // DzBlackHoleMod::loadSection sample).
    static const short kManifestSectionID = 0x0100;

    injector_core::SceneManifest m_manifest;
};

}  // namespace daz_plugin
