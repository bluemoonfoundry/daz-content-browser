# Links only injector_core -- no Daz Studio SDK/Qt dependency, runs in ordinary CI.
# Framework: GoogleTest, fetched via FetchContent (see beads-2mw.1 notes for rationale).

include(FetchContent)
FetchContent_Declare(
    googletest
    GIT_REPOSITORY https://github.com/google/googletest.git
    GIT_TAG v1.15.2
)
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

add_executable(injector_core_tests
    test_tmb_reader.cpp
    test_morph_index_reader.cpp
    test_formula_compiler.cpp
    test_scene_manifest.cpp
)

target_link_libraries(injector_core_tests PRIVATE
    injector_core
    GTest::gtest_main
)

# test_tmb_reader.cpp includes TmbReader.h, which uses std::span (C++20).
# injector_core only requires cxx_std_20 PRIVATE (see its CMakeLists.txt) so
# that requirement doesn't leak into daz_plugin -- so this target declares
# its own requirement instead of inheriting one.
target_compile_features(injector_core_tests PRIVATE cxx_std_20)

target_compile_definitions(injector_core_tests PRIVATE
    TMB_TEST_FIXTURES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures"
    # Fixture DB for MorphIndexReader tests: a handful of rows extracted from the
    # real repo-root morph_index.db via cpp/tests/fixtures/build_fixture_db.py.
    FIXTURE_DB_PATH="${CMAKE_CURRENT_SOURCE_DIR}/fixtures/morph_index.db"
    FIXTURES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures"
)

include(GoogleTest)
gtest_discover_tests(injector_core_tests)
