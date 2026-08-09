/**********************************************************************
    SceneManifestLoader -- Subsystem C Task 4 (daz-content-browser-jhq.4).
    The reload-time counterpart to SceneManifestWriter: reads the
    SceneManifestData (Task 1) a just-loaded DzScene carries, if any, and
    re-injects each entry via InjectorCore::sharedInstance() so a saved scene's
    injected morphs come back after a full close/reopen without their (never
    referenced) .dsf source files on disk.

    Driven by DzScene::sceneLoaded() (dzscene.h) -- unlike sceneSaveStarting()
    (see SceneManifestWriter.h's correction note), this signal was confirmed to
    fire on a real scene load in the target Daz Studio version, so a direct
    signal connection is used here rather than a synchronous call site.
**********************************************************************/

#pragma once

namespace daz_plugin {

class SceneManifestLoader
{
public:
    /**
        Reads dzScene's SceneManifestData (if attached) and re-injects each
        entry: resolves the entry's target_figure to a live node
        (InjectorCore::resolveFigureNode(), the same lookup
        InjectorCore::resolveNodeForFigure() uses), calls
        InjectorCore::sharedInstance().injectMorphByGuid() on it, and restores
        the dialed value with DzFloatProperty::setValue(). InjectorCore's own
        registerInjectedMorph() repopulates the session registry as a side
        effect of injectMorphByGuid(), so Task 2's registry is accurate again
        by the time this returns -- there is no separate registry-population
        step here.

        Never aborts the scene load: an entry whose target_figure node is not
        in the scene, or whose guid no longer resolves in morph_index.db, is
        logged via dzApp->log() and skipped (Subsystem B's established
        graceful-failure posture -- see InjectorCore.h's FAILURE POSTURE note).

        A no-op if dzScene is null, no manifest is attached, or the manifest
        is empty.
    **/
    static void restore();
};

}  // namespace daz_plugin
