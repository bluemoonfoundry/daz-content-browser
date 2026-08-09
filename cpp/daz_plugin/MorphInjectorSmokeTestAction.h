/**********************************************************************
    DzMorphInjectorSmokeTestAction -- the minimum viable way to reach
    InjectorCore::injectMorph() from inside a running Daz Studio session.

    This exists so beads-2mw.9 (end-to-end manual smoke test) has something to
    click: InjectorCore is the top-level integration point and cannot be
    exercised at all without a live scene, and until Subsystem D builds the real
    search panel there is no other UI that calls it.

    It is scaffolding, deliberately: no dialog, no morph picker, no undo
    integration. It injects one hardcoded morph guid onto the primary scene
    selection and reports the outcome. Everything it needs is overridable from
    the environment so the manual verification pass can retarget it at a better
    real-scene morph without a rebuild:

      DAZ_MORPH_INDEX_DB   (or MORPH_INDEX_DB_PATH)  -- path to morph_index.db
      DAZ_MORPH_CACHE_DIR  (or MORPH_CACHE_PATH)     -- root of the .tmb cache
      DAZ_MORPH_SMOKE_TEST_GUID                      -- morph guid to inject

    Appears in Daz Studio under the "Morph Injector" action group / menu; see
    getActionGroup()/getDefaultMenuPath() below and dzaction.h for how
    DzActionMgr consumes them.

    THREADING: DzAction::executeAction() is invoked on the main GUI thread,
    which is exactly what InjectorCore requires (design section 7).
**********************************************************************/

#pragma once

#include "dzaction.h"

/**
    A single menu item that runs one hardcoded injection against the currently
    selected node.
**/
class DzMorphInjectorSmokeTestAction : public DzAction
{
    Q_OBJECT

public:
    DzMorphInjectorSmokeTestAction();

    virtual QString getActionGroup() const { return tr( "Morph Injector" ); }
    virtual QString getDefaultMenuPath() const { return tr( "&Morph Injector" ); }

protected:
    virtual void executeAction();

private slots:
    /**
        Subsystem C Task 4 (daz-content-browser-jhq.4): connected to
        DzScene::sceneLoaded() (dzscene.h) from this class's constructor --
        the connection point PluginMain.cpp's DZ_PLUGIN_DEFINITION comment
        documents. Delegates to SceneManifestLoader::restore(); kept here
        rather than in a dedicated QObject so the plugin has exactly one
        Q_OBJECT class already known to construct once at plugin load, instead
        of a second one built solely to own a slot.
    **/
    void onSceneLoaded();
};
