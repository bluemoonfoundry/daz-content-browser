/**********************************************************************
    SceneManifestWriter -- Subsystem C Task 3 (daz-content-browser-jhq.3).
    Snapshots InjectorCore::sharedInstance()'s registry (Task 2, InjectorCore.h)
    into a fresh injector_core::SceneManifest and attaches/updates it on the
    live DzScene as a SceneManifestData (Task 1), so it is already in place by
    the time ANY save happens to run.

    CORRECTION (found via live testing against a real Daz Studio 4.24 session,
    daz-content-browser-jhq.3): this was originally designed around a
    DzScene::sceneSaveStarting( const QString& ) signal (dzscene.h) -- the
    parent bd issue's own title. Live testing showed that signal (and its
    sceneSaved()/assetModified() siblings) is never actually emitted by a real
    Ctrl+S / File > Save in this Daz Studio version, even though the
    connection itself succeeds (confirmed cross-DLL signal/slot delivery works
    correctly via DzScene::nodeSelectionListChanged(), which fires reliably).
    Only IDzSceneAsset::assetWasSaved() (also implemented by DzScene) was
    observed to fire -- and only AFTER the file is already written, too late
    to affect that save. So instead of hooking any signal, refresh() is called
    directly, synchronously, right after each successful injection
    (MorphInjectorSmokeTestAction.cpp) -- the manifest is then already correct
    on the DzScene by construction, independent of which save path (or
    Daz Studio version's signal wiring) eventually serializes it.
**********************************************************************/

#pragma once

namespace daz_plugin {

class SceneManifestWriter
{
public:
    /**
        Rebuilds the manifest from InjectorCore::sharedInstance()'s registry
        and attaches/updates it on dzScene as a SceneManifestData
        (SceneManifestData.h), creating one via DzScene::addDataItem() if this
        session hasn't attached one yet, or updating the existing instance in
        place otherwise (SceneManifestData has no duplicate() to worry about --
        see its header -- so setManifest() is a plain in-place assignment).

        A no-op if dzScene (dzscene.h) is not constructed yet -- not expected
        in practice, since this is only ever called from within an already-live
        session (after a user-triggered injection), but degrades safely rather
        than crashing if it is ever called earlier.
    **/
    static void refresh();
};

}  // namespace daz_plugin
