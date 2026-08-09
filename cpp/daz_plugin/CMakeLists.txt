# SDK-dependent target: links injector_core + the Daz Studio SDK's dzcore and its
# vendored Qt 4.8, and produces the loadable Daz Studio plugin binary.
#
# Requires a real Daz Studio SDK at DAZ_SDK_DIR. Everything the SDK exposes is
# reached through the same target/variable names the SDK's own sample plugins use
# (dzcore, ${DZSDK_QT_*_TARGET}); see cpp/cmake/DazStudioSDK.cmake for how those
# are declared, and samples/interface/AFirstPlugin/CMakeLists.txt in the SDK for
# the pattern being mirrored.

include(${CMAKE_SOURCE_DIR}/cmake/DazStudioSDK.cmake)

set(DZ_PLUGIN_TGT_NAME daz_plugin)
set(DZ_PLUGIN_PROJECT_NAME "Daz Morph Injector")

# Basename of the produced binary. Daz Studio plugin binaries are conventionally
# lowercase and prefixed (cf. the SDK sample's sdk_afirstplugin.dll).
set(DZ_PLUGIN_OUTPUT_NAME "dzmorphinjector")

# On Windows a Daz Studio plugin is an ordinary DLL dropped into Daz Studio's
# plugins/ directory -- the SDK sample ships as plugins/sdk_afirstplugin.dll (see
# samples/interface/AFirstPlugin/docs/.../AFirstPluginREADME.htm, "Content Files:
# PC"). ".dsx" is *not* the plugin binary extension; in Daz's own packaging .dsx
# files are the DSON XML docs/inline-help payloads that ship alongside the DLL.
# So the default here is ${DZ_BIN_SUFFIX} (.dll on Windows, .dylib on macOS),
# exposed as a cache var only so a differently-configured Studio install can
# override it without editing this file.
set(DAZ_PLUGIN_SUFFIX "${DZ_BIN_SUFFIX}" CACHE STRING
    "File extension for the built Daz Studio plugin binary (Windows: .dll)")

if(WIN32)
    # The DZ_PLUGIN_DEFINITION macro emits getSDKVersion()/getPluginDefinition()
    # with __declspec(dllexport) but *without* extern "C", so without a .def file
    # they would be exported C++-mangled and Daz Studio's GetProcAddress lookup
    # would fail. The .def re-exports them undecorated, exactly as the SDK's own
    # samples do (afirstplugin.def).
    set(DZ_PLUGIN_OS_SOURCES dzmorphinjector.def)
else()
    set(DZ_PLUGIN_OS_SOURCES "")
endif()

add_library(${DZ_PLUGIN_TGT_NAME} SHARED
    PluginMain.cpp
    PropertySourceAdapter.cpp
    FormulaControllerBuilder.cpp
    InjectorCore.cpp
    MorphInjectorSmokeTestAction.cpp
    SceneManifestData.cpp
    SceneManifestDataIO.cpp
    SceneManifestLoader.cpp
    SceneManifestWriter.cpp
    ${DZ_PLUGIN_OS_SOURCES}
)

# DzMorphInjectorSmokeTestAction (MorphInjectorSmokeTestAction.h) is the first
# Q_OBJECT-bearing class in this target, so AUTOMOC -- pinned to the Qt4 moc
# vendored in the SDK, never a system Qt5/6 moc -- now does real work rather than
# just proving the toolchain. Note the SDK samples instead end their .cpp with
# `#include "moc_<name>.cpp"`; that pattern is deliberately NOT used here,
# because CMake's AUTOMOC already compiles the generated moc_*.cpp via
# mocs_compilation.cpp.
set_property(TARGET ${DZ_PLUGIN_TGT_NAME} PROPERTY AUTOMOC ON)
set_property(TARGET ${DZ_PLUGIN_TGT_NAME} PROPERTY AUTOMOC_EXECUTABLE "${DZSDK_MOC_EXECUTABLE}")
set_property(TARGET ${DZ_PLUGIN_TGT_NAME} PROPERTY AUTOMOC_COMPILER_PREDEFINES OFF)

target_include_directories(${DZ_PLUGIN_TGT_NAME} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(${DZ_PLUGIN_TGT_NAME}
    PRIVATE
    # Proven-but-unused link boundary: the skeleton calls nothing from
    # injector_core yet, but Tasks 6-8 do, so the link edge is established now.
    injector_core
    dzcore
    ${DZSDK_QT_CORE_TARGET}
    ${DZSDK_QT_GUI_TARGET}
    ${DZSDK_QT_SCRIPT_TARGET}
)

if(MSVC)
    # The SDK ships release-only dzcore/Qt import libs (its own top-level
    # CMakeLists.txt does `add_compile_options("/MD" "/U_DEBUG")` for the same
    # reason). CMAKE_MSVC_RUNTIME_LIBRARY is forced to the release DLL CRT
    # project-wide in cpp/CMakeLists.txt so injector_core matches; /U_DEBUG is a
    # belt-and-braces guard so Qt/dzcore headers never take a debug-only path.
    target_compile_options(${DZ_PLUGIN_TGT_NAME} PRIVATE "/U_DEBUG")
    # windows.h arrives via DZ_PLUGIN_DEFINITION; keep its min/max macros out of
    # the way of Qt and the standard library.
    target_compile_definitions(${DZ_PLUGIN_TGT_NAME} PRIVATE NOMINMAX)
endif()

set_target_properties(${DZ_PLUGIN_TGT_NAME}
    PROPERTIES
    OUTPUT_NAME ${DZ_PLUGIN_OUTPUT_NAME}
    PREFIX ""
    SUFFIX "${DAZ_PLUGIN_SUFFIX}"
    PROJECT_LABEL ${DZ_PLUGIN_PROJECT_NAME}
)
