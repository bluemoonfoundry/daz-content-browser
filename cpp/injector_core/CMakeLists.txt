# SDK-independent static lib: TmbReader, MorphIndexReader, FormulaCompiler (added by
# beads-2mw.2/.3/.4). No Daz Studio SDK or Qt dependency -- must build and test in
# ordinary CI without a Daz Studio install.

add_library(injector_core STATIC
    placeholder.cpp
)

target_include_directories(injector_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
