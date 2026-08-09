/**********************************************************************
    Daz Morph Injector -- plugin entry point.

    This file contains *only* the Daz Studio SDK plugin registration
    boilerplate; all behaviour lives in the classes it registers.

    The macro usage below mirrors the SDK's own sample at
    samples/interface/AFirstPlugin/pluginmain.cpp.
**********************************************************************/

#include "dzplugin.h"
#include "dzapp.h"

#include "MorphInjectorSmokeTestAction.h"
#include "SceneManifestData.h"
#include "SceneManifestDataIO.h"
#include "version.h"

/*****************************
   Plugin Definition
*****************************/

/**
    Creates the plugin definition object plus the getSDKVersion() /
    getPluginDefinition() exports (and DllMain on Windows) that Daz Studio
    needs in order to load this DLL. See dzmorphinjector.def for why those two
    exports are also listed in a module-definition file.

    Plain DZ_PLUGIN_DEFINITION rather than DZ_CUSTOM_PLUGIN_DEFINITION: the
    DZ_PLUGIN_REGISTER_SCENEDATA_EXTRA_OBJECT_IO macro below (dzplugin.h)
    expands to code that references the file-scope `s_pluginDef` global this
    macro declares by name; DZ_CUSTOM_PLUGIN_DEFINITION instead hides its
    plugin instance inside a function-local static, which that macro cannot
    see. A previous version of this file used the custom form to defer
    SceneManifestWriter's dzScene connection past plugin-load time, but
    connecting from DzMorphInjectorSmokeTestAction's own constructor (see
    MorphInjectorSmokeTestAction.cpp) turned out to work fine and avoids this
    conflict entirely.
**/
DZ_PLUGIN_DEFINITION( "Daz Morph Injector" );

DZ_PLUGIN_AUTHOR( "BlueMoonFoundry" );

DZ_PLUGIN_VERSION( PLUGIN_MAJOR, PLUGIN_MINOR, PLUGIN_REV, PLUGIN_BUILD );

DZ_PLUGIN_DESCRIPTION( QString(
                           "Just-in-time morph injector for %1. "
                           "<br><br>"
                           "Loads morphs on demand from a prebuilt binary morph "
                           "index rather than requiring every morph to be "
                           "resident at scene-load time, wiring each injected "
                           "morph's ERC formulas up as native %1 controllers."
                           "<br><br>"
                           "This build exposes a single scaffolding action, "
                           "<i>Morph Injector &gt; Inject Test Morph</i>, used to "
                           "verify the injection pipeline against a live scene. "
                           "The real search/injection UI is a later subsystem."
                       ).arg( dzApp->getAppName() ) );

/*****************************
   Exported classes
*****************************/

/**
    Registers the smoke-test action (Subsystem B Task 8, beads-2mw.8) so that
    InjectorCore is reachable from a running Daz Studio session at all -- Task 9's
    manual end-to-end verification has nothing to invoke otherwise.

    The GUID below was freshly generated for this class and is never to be
    reused; every additional exported class needs its own (see the 'ClassIDs'
    page in the Daz Studio SDK documentation).
**/
DZ_PLUGIN_CLASS_GUID( DzMorphInjectorSmokeTestAction, B0E93757-286C-4147-9895-4019E6A837EB );

/**
    Registers SceneManifestData (Subsystem C Task 1, daz-content-browser-jhq.1) so
    Daz Studio's file loader can reconstruct it by class GUID when a saved .duf's
    DzElementData chunk is read back (the same DzTClassFactory mechanism the SDK
    uses for every persistent DzBase subclass), and so it is newable from DazScript
    for manual verification.

    Invoked inside daz_plugin's own namespace (rather than passing a
    "daz_plugin::"-qualified typeName to the macro) because DZ_PLUGIN_CLASS_GUID
    token-pastes typeName directly onto GUID/Factory suffixes and then defines
    those pasted names as new (not out-of-line) class/variable declarations --
    which requires typeName to be a single unqualified identifier in scope.
**/
namespace daz_plugin {
DZ_PLUGIN_CLASS_GUID( SceneManifestData, 5A8E01CB-E7D4-40F7-ADFA-9C3760F023E1 );

/**
    Registers SceneManifestDataIO (Subsystem C Task 3, daz-content-browser-jhq.3)
    as the DSON (.duf) read/write bridge for SceneManifestData -- see
    SceneManifestDataIO.h for why this, and not SceneManifestData's own
    loadSection()/save(), is what actually gets the manifest into a saved .duf.
    Registers under DSON type "studio/scene_data/scene_manifest"
    (DZ_PLUGIN_REGISTER_SCENEDATA_EXTRA_OBJECT_IO prefixes the tag with
    "studio/scene_data/", dzplugin.h).
**/
DZ_PLUGIN_CLASS_GUID( SceneManifestDataIO, 8F3C2A11-6B4D-4E2A-9F7E-2D5B8C9A1F04 );
DZ_PLUGIN_REGISTER_SCENEDATA_EXTRA_OBJECT_IO( "scene_manifest", SceneManifestDataIO, SceneManifestData );
}  // namespace daz_plugin
