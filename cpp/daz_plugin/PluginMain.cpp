/**********************************************************************
    Daz Morph Injector -- plugin entry point.

    This file contains *only* the Daz Studio SDK plugin registration
    boilerplate. It deliberately implements no injection behaviour: its whole
    job is to prove the build + link + load path (dzcore, the SDK's vendored
    Qt 4.8, and the injector_core static lib) before Subsystem B Tasks 6-8 add
    real functionality.

    The macro usage below mirrors the SDK's own sample at
    samples/interface/AFirstPlugin/pluginmain.cpp.
**********************************************************************/

#include "dzplugin.h"
#include "dzapp.h"

#include "version.h"

/*****************************
   Plugin Definition
*****************************/

/**
    Creates the plugin definition object plus the getSDKVersion() /
    getPluginDefinition() exports (and DllMain on Windows) that Daz Studio
    needs in order to load this DLL. See dzmorphinjector.def for why those two
    exports are also listed in a module-definition file.
**/
DZ_PLUGIN_DEFINITION( "Daz Morph Injector" );

DZ_PLUGIN_AUTHOR( "BlueMoonFoundry" );

DZ_PLUGIN_VERSION( PLUGIN_MAJOR, PLUGIN_MINOR, PLUGIN_REV, PLUGIN_BUILD );

DZ_PLUGIN_DESCRIPTION( QString(
                           "Just-in-time morph injector for %1. "
                           "<br><br>"
                           "This build is a registration-only skeleton: it "
                           "exports no classes and performs no injection. It "
                           "exists to verify that the plugin builds against the "
                           "Daz Studio SDK and is accepted by %1's plugin "
                           "loader."
                       ).arg( dzApp->getAppName() ) );

/*****************************
   Exported classes
*****************************/

/**
    No DZ_PLUGIN_CLASS_GUID registrations yet -- a plugin exporting zero classes
    is valid and still shows up in Help > About Installed Plugins, which is what
    the manual load check looks for.

    Subsystem B Tasks 6-8 add the real exported classes here (each needs its own
    freshly generated GUID -- never reuse one from an SDK sample), e.g.:

        DZ_PLUGIN_CLASS_GUID( DzMorphInjectorAction, <fresh-guid> );
**/
