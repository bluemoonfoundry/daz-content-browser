cmake_minimum_required(VERSION 3.15)
project(daz_morph_injector CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(MSVC)
    # The Daz Studio SDK ships release-only dzcore/Qt import libs (its own
    # top-level CMakeLists.txt globally applies "/MD" "/U_DEBUG" for this
    # reason). Anything that ends up inside the .dsx/.dll -- including
    # injector_core -- must therefore use the release DLL CRT even in Debug
    # builds, otherwise the plugin picks up conflicting CRTs and a mismatched
    # _ITERATOR_DEBUG_LEVEL. Set project-wide so injector_core, daz_plugin and
    # the test binaries all agree.
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")
endif()

# Points at a local Daz Studio SDK install, e.g.
# y:/working/BlueMoonFoundry/daz-studio-sdks/daz-studio-45-sdk-win32 (the design's
# investigation baseline). The SDK is proprietary and never vendored into this repo.
set(DAZ_SDK_DIR "" CACHE PATH "Path to a local Daz Studio SDK install")
if(NOT DAZ_SDK_DIR)
    message(FATAL_ERROR "DAZ_SDK_DIR must be set to a local Daz Studio SDK install path.")
endif()
if(NOT EXISTS "${DAZ_SDK_DIR}/include")
    message(FATAL_ERROR "DAZ_SDK_DIR (${DAZ_SDK_DIR}) does not look like a Daz Studio SDK install (missing include/).")
endif()

enable_testing()

# SDK-independent static lib -- no Daz SDK/Qt dependency, unit-testable in ordinary CI.
add_subdirectory(injector_core)

# Links injector_core + Qt + Daz SDK -> .dsx. Only buildable with a real SDK/Qt toolchain.
add_subdirectory(daz_plugin)

# Links only injector_core; no SDK/Qt dependency, runs in ordinary CI.
add_subdirectory(tests)
