# SDK-independent static lib: TmbReader, MorphIndexReader, FormulaCompiler (added by
# beads-2mw.2/.3/.4). No Daz Studio SDK or Qt dependency -- must build and test in
# ordinary CI without a Daz Studio install.

include(FetchContent)

# MorphIndexReader needs a C sqlite3 dependency to read morph_index.db. Per the
# Subsystem B design doc (docs/superpowers/specs/2026-08-07-morph-injector-core-design.md
# S5), injector_core links only the C++ standard library plus a statically-linked
# sqlite3 amalgamation -- no system sqlite3, no ORM. Pulled via FetchContent from a
# prebuilt-amalgamation mirror (two files: sqlite3.c/sqlite3.h, no build step of its
# own required) rather than the canonical sqlite/sqlite repo, which needs a separate
# amalgamation-generation step we don't want at configure time.
#
# That mirror ships its own CMakeLists.txt (defines a "SQLite3" target), but its
# target_include_directories only exposes $<INSTALL_INTERFACE:...> -- fine for an
# `install`ed consumer, not for us building straight out of the FetchContent source
# dir. So we compile sqlite3.c ourselves into a distinctly-named target
# (sqlite3_amalgamation) with an explicit BUILD_INTERFACE include path, and just
# ignore the fetched project's own (unused) "SQLite3"/shell_app targets.
set(BUILD_SHELL OFF CACHE BOOL "" FORCE)  # don't build the sqlite3 CLI shell too
FetchContent_Declare(
    sqlite_amalgamation
    GIT_REPOSITORY https://github.com/azadkuh/sqlite-amalgamation.git
    GIT_TAG master
)
FetchContent_MakeAvailable(sqlite_amalgamation)

add_library(sqlite3_amalgamation STATIC
    ${sqlite_amalgamation_SOURCE_DIR}/sqlite3.c
)
target_include_directories(sqlite3_amalgamation PUBLIC ${sqlite_amalgamation_SOURCE_DIR})
if(MSVC)
    # Pin the CRT explicitly rather than relying on the project-wide
    # CMAKE_MSVC_RUNTIME_LIBRARY (cpp/CMakeLists.txt): this target is created in
    # the same scope as the FetchContent'd sqlite project, and in practice it
    # comes out of generation with an *empty* RuntimeLibrary, i.e. cl.exe's
    # default of /MT. Discovered in beads-2mw.6, the first task whose code
    # actually references MorphIndexReader and so drags sqlite3.obj into the
    # plugin DLL -- which then linked with `LNK4098: defaultlib 'LIBCMT'
    # conflicts`, i.e. a static CRT mixed into a /MD DLL (two heaps; sqlite3
    # allocations freed against the wrong one). Belt-and-braces: the property is
    # the modern spelling, /MD is the guaranteed-effective flag.
    set_property(TARGET sqlite3_amalgamation PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")
    target_compile_options(sqlite3_amalgamation PRIVATE "/MD")
endif()
# Minimal, single-threaded-per-connection use from injector_core; keep the
# amalgamation lean and avoid pulling in extension-loading/shared-cache surface.
target_compile_definitions(sqlite3_amalgamation PUBLIC
    SQLITE_THREADSAFE=1
    SQLITE_OMIT_LOAD_EXTENSION
)

FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
)
FetchContent_MakeAvailable(nlohmann_json)

add_library(injector_core STATIC
    TmbReader.cpp
    MorphIndexReader.cpp
    FormulaCompiler.cpp
    SceneManifest.cpp
)

target_include_directories(injector_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(injector_core PUBLIC sqlite3_amalgamation nlohmann_json::nlohmann_json)

# std::span (TmbReader's zero-copy delta view) requires C++20. PRIVATE, not
# PUBLIC: a PUBLIC requirement here would propagate to daz_plugin (it links
# injector_core), forcibly bumping its effective standard past the project's
# C++17 default -- which breaks the vendored Qt4 SDK headers (`register` as a
# storage-class specifier is a hard error under MSVC's strict C++20 mode).
# daz_plugin doesn't include TmbReader.h yet, so it doesn't need this; the
# tests target does, and declares its own cxx_std_20 requirement below in
# cpp/tests/CMakeLists.txt instead of inheriting it from here.
target_compile_features(injector_core PRIVATE cxx_std_20)
