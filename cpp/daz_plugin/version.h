#pragma once

// Version stamp for the Daz Morph Injector plugin, consumed by
// DZ_PLUGIN_VERSION in PluginMain.cpp. Mirrors the SDK samples' version.h.

#include "dzversion.h"

#define PLUGIN_MAJOR 0
#define PLUGIN_MINOR 1
#define PLUGIN_REV   0
#define PLUGIN_BUILD 0

#define PLUGIN_VERSION DZ_MAKE_VERSION( PLUGIN_MAJOR, PLUGIN_MINOR, PLUGIN_REV, PLUGIN_BUILD )
