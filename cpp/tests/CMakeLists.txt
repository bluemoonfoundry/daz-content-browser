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
    test_placeholder.cpp
)

target_link_libraries(injector_core_tests PRIVATE
    injector_core
    GTest::gtest_main
)

include(GoogleTest)
gtest_discover_tests(injector_core_tests)
