# SDK-dependent target -- links injector_core + Qt + Daz SDK, produces the .dsx.
# The real DAZ_SDK_DIR/Qt linking and PluginMain.cpp registration boilerplate are
# added by beads-2mw.5. For now this is an empty-but-buildable placeholder that
# proves the build graph resolves; it depends only on injector_core, so it does
# not require a real Daz Studio SDK or Qt to configure or build.

add_library(daz_plugin STATIC
    placeholder.cpp
)

target_link_libraries(daz_plugin PRIVATE injector_core)
