# DazStudioSDK.cmake
#
# Locates a local Daz Studio SDK install (DAZ_SDK_DIR) and exposes it to this
# project using *exactly the same* target/variable names the SDK's own top-level
# CMakeLists.txt uses, so that our plugin target can be written the same way the
# SDK's own sample plugins are written.
#
# Why we re-derive this instead of add_subdirectory()'ing the SDK: the SDK's
# top-level CMakeLists.txt declares `cmake_minimum_required(VERSION 3.4.0)` and
# unconditionally `add_subdirectory(samples)`. CMake >= 4.0 hard-errors on a
# <3.5 minimum, and we do not want to build the SDK's samples. So this file is a
# faithful transcription of the parts of
#   <DAZ_SDK_DIR>/CMakeLists.txt
# that define the consumable interface.
#
# Defines (mirroring the SDK's own names 1:1):
#   DZ_LIB_SUFFIX / DZ_BIN_SUFFIX / DZ_LIB_PREFIX / UTIL_EXT
#   DZ_PLATFORM / DZ_MIXED_PLATFORM / DZ_OS_PLATFORM
#   DAZ_SDK_CORE_RELATIVE_PATH
#   DAZ_SDK_INCLUDE_DIR / DAZ_SDK_DZCORE_LIB / DAZ_SDK_DPC_EXE
#   imported targets: dzcore (SHARED IMPORTED), dpc (IMPORTED executable)
#   DZSDK_QT_CORE_TARGET / DZSDK_QT_GUI_TARGET / DZSDK_QT_SCRIPT_TARGET /
#   DZSDK_QT_OPENGL_TARGET / DZSDK_QT_NETWORK_TARGET / DZSDK_QT_SQL_TARGET /
#   DZSDK_QT_XML_TARGET
#   DZSDK_MOC_EXECUTABLE
#
# Qt note: Qt 4.8 is *vendored inside the SDK* (lib/<plat>/Qt*4.lib,
# bin/<plat>/Qt*4.dll). There is no system Qt dependency. CMake's bundled
# FindQt4 module is deprecated and drives qmake/mkspecs probing that the
# vendored, mkspec-less Qt tree cannot satisfy, so we declare the Qt4::* imported
# targets directly from the vendored import libs. The target *names* are the same
# ones FindQt4 would have produced (Qt4::QtCore, ...), which is what the SDK's
# DZSDK_QT_*_TARGET variables point at -- so consumers are unchanged.

include_guard(GLOBAL)

if(NOT DAZ_SDK_DIR)
    message(FATAL_ERROR "DazStudioSDK.cmake: DAZ_SDK_DIR is not set.")
endif()

# --- Platform / architecture (verbatim from the SDK's CMakeLists.txt) ---------
if(WIN32)
    set(DZ_LIB_SUFFIX ".lib")
    set(DZ_BIN_SUFFIX ".dll")
    set(DZ_LIB_PREFIX "")
    set(UTIL_EXT ".exe")
    if(CMAKE_SIZEOF_VOID_P EQUAL 4)
        set(DZ_PLATFORM x86)
        set(DZ_MIXED_PLATFORM Win32)
        set(DZ_OS_PLATFORM Win32)
    elseif(CMAKE_SIZEOF_VOID_P EQUAL 8)
        set(DZ_PLATFORM x64)
        set(DZ_MIXED_PLATFORM x64)
        set(DZ_OS_PLATFORM Win64)
    else()
        message(FATAL_ERROR "Unknown architecture")
    endif()
elseif(APPLE)
    set(DZ_LIB_SUFFIX ".dylib")
    set(DZ_BIN_SUFFIX ".dylib")
    set(DZ_LIB_PREFIX "lib")
    set(UTIL_EXT "")
    if(CMAKE_SIZEOF_VOID_P EQUAL 4)
        set(DZ_PLATFORM x86)
        set(DZ_MIXED_PLATFORM Mac32)
        set(DZ_OS_PLATFORM Mac32)
    elseif(CMAKE_SIZEOF_VOID_P EQUAL 8)
        set(DZ_PLATFORM x64)
        set(DZ_MIXED_PLATFORM Mac64)
        set(DZ_OS_PLATFORM Mac64)
    else()
        message(FATAL_ERROR "Unknown architecture")
    endif()
    set(CMAKE_MACOSX_RPATH TRUE)
    set(CMAKE_BUILD_WITH_INSTALL_RPATH TRUE)
else()
    message(FATAL_ERROR "Unsupported platform for the Daz Studio SDK")
endif()

set(DAZ_SDK_CORE_RELATIVE_PATH "lib/${DZ_MIXED_PLATFORM}/${DZ_LIB_PREFIX}dzcore${DZ_LIB_SUFFIX}")

# --- dzcore imported target ---------------------------------------------------
set(DAZ_SDK_INCLUDE_DIR "${DAZ_SDK_DIR}/include" CACHE PATH "Path to DAZ Studio SDK includes")
set(DAZ_SDK_DZCORE_LIB "${DAZ_SDK_DIR}/${DAZ_SDK_CORE_RELATIVE_PATH}" CACHE FILEPATH "Path to dzcore library")
if(NOT EXISTS "${DAZ_SDK_DZCORE_LIB}")
    message(FATAL_ERROR
        "The library dzcore could not be located at ${DAZ_SDK_DZCORE_LIB}.\n"
        "Check DAZ_SDK_DIR, and that the SDK ships libs for this architecture "
        "(${DZ_MIXED_PLATFORM}).")
endif()

if(NOT TARGET dzcore)
    add_library(dzcore SHARED IMPORTED GLOBAL)
    if(WIN32)
        set_property(TARGET dzcore APPEND PROPERTY IMPORTED_IMPLIB "${DAZ_SDK_DZCORE_LIB}")
    else()
        set_property(TARGET dzcore APPEND PROPERTY IMPORTED_LOCATION "${DAZ_SDK_DZCORE_LIB}")
    endif()
    set_property(TARGET dzcore APPEND PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${DAZ_SDK_INCLUDE_DIR}")
endif()

# --- dpc (Daz Plugin Compiler) imported executable ---------------------------
# Not used by the current skeleton, but declared so later tasks that need to
# compile embedded scripts/images can just COMMAND dpc, like the SDK samples do.
set(DAZ_SDK_DPC_EXE "${DAZ_SDK_DIR}/bin/${DZ_MIXED_PLATFORM}/dpc${UTIL_EXT}" CACHE FILEPATH "Path to DAZ Studio SDK dpc executable")
if(EXISTS "${DAZ_SDK_DPC_EXE}" AND NOT TARGET dpc)
    add_executable(dpc IMPORTED GLOBAL)
    set_property(TARGET dpc APPEND PROPERTY IMPORTED_LOCATION "${DAZ_SDK_DPC_EXE}")
endif()

# --- Qt 4.8, vendored inside the SDK ----------------------------------------
set(DAZ_SDK_QT_LIB_DIR "${DAZ_SDK_DIR}/lib/${DZ_MIXED_PLATFORM}")
set(DAZ_SDK_QT_BIN_DIR "${DAZ_SDK_DIR}/bin/${DZ_MIXED_PLATFORM}")

# Qt4 header layout inside the SDK's include/ dir. The SDK ships the modules
# flattened under include/<Module>/ with no include/ parent per module.
set(_dzsdk_qt_include_dirs "${DAZ_SDK_INCLUDE_DIR}")

# module-name -> (imported target, import lib basename, include subdir)
set(_dzsdk_qt_modules QtCore QtGui QtScript QtOpenGL QtNetwork QtSql QtXml Qt3Support)

foreach(_mod IN LISTS _dzsdk_qt_modules)
    if(WIN32)
        set(_lib "${DAZ_SDK_QT_LIB_DIR}/${_mod}4${DZ_LIB_SUFFIX}")
    else()
        set(_lib "${DAZ_SDK_QT_LIB_DIR}/${_mod}.framework")
    endif()
    if(NOT EXISTS "${_lib}")
        message(FATAL_ERROR "Vendored Qt module ${_mod} not found at ${_lib} (DAZ_SDK_DIR=${DAZ_SDK_DIR})")
    endif()
    if(NOT TARGET Qt4::${_mod})
        add_library(Qt4::${_mod} SHARED IMPORTED GLOBAL)
        if(WIN32)
            set_property(TARGET Qt4::${_mod} APPEND PROPERTY IMPORTED_IMPLIB "${_lib}")
            # No debug Qt import libs are shipped; point every config at the
            # release import lib so Debug builds of our plugin still link.
            set_property(TARGET Qt4::${_mod} PROPERTY MAP_IMPORTED_CONFIG_DEBUG "")
            set_property(TARGET Qt4::${_mod} PROPERTY MAP_IMPORTED_CONFIG_RELWITHDEBINFO "")
            set_property(TARGET Qt4::${_mod} PROPERTY MAP_IMPORTED_CONFIG_MINSIZEREL "")
        else()
            set_property(TARGET Qt4::${_mod} APPEND PROPERTY IMPORTED_LOCATION "${_lib}")
        endif()
        set_property(TARGET Qt4::${_mod} APPEND PROPERTY
            INTERFACE_INCLUDE_DIRECTORIES "${DAZ_SDK_INCLUDE_DIR}" "${DAZ_SDK_INCLUDE_DIR}/${_mod}")
        # QT_DLL/QT_SHARED are what qmake/FindQt4 define for a shared Qt build;
        # Qt headers key their Q_CORE_EXPORT dllimport decoration off them.
        set_property(TARGET Qt4::${_mod} APPEND PROPERTY
            INTERFACE_COMPILE_DEFINITIONS QT_DLL QT_SHARED "QT_${_mod}_LIB")
    endif()
endforeach()

# dzcore's public headers include Qt headers, so make Qt part of dzcore's
# interface (the SDK's own samples get this by always listing Qt alongside it).
set_property(TARGET dzcore APPEND PROPERTY INTERFACE_LINK_LIBRARIES Qt4::QtCore)

# Same variable names the SDK's top-level CMakeLists.txt sets, so plugin
# CMakeLists files here read identically to the SDK's samples.
set(DZSDK_QT_CORE_TARGET Qt4::QtCore)
set(DZSDK_QT_GUI_TARGET Qt4::QtGui)
set(DZSDK_QT_SCRIPT_TARGET Qt4::QtScript)
set(DZSDK_QT_OPENGL_TARGET Qt4::QtOpenGL)
set(DZSDK_QT_NETWORK_TARGET Qt4::QtNetwork)
set(DZSDK_QT_SQL_TARGET Qt4::QtSql)
set(DZSDK_QT_XML_TARGET Qt4::QtXml)
set(DZSDK_QT_3SUPPORT_TARGET Qt4::Qt3Support)

# The SDK vendors Qt4's moc; CMAKE_AUTOMOC must use *this* moc, never a system
# Qt5/6 moc, or generated code will not match the Qt4 headers being compiled.
set(DZSDK_MOC_EXECUTABLE "${DAZ_SDK_QT_BIN_DIR}/moc${UTIL_EXT}")
if(NOT EXISTS "${DZSDK_MOC_EXECUTABLE}")
    message(FATAL_ERROR "Vendored moc not found at ${DZSDK_MOC_EXECUTABLE}")
endif()
if(NOT TARGET Qt4::moc)
    add_executable(Qt4::moc IMPORTED GLOBAL)
    set_property(TARGET Qt4::moc PROPERTY IMPORTED_LOCATION "${DZSDK_MOC_EXECUTABLE}")
endif()
# CMake's AUTOMOC for the Qt4 flavour keys off QT_MOC_EXECUTABLE / Qt4::moc.
set(QT_MOC_EXECUTABLE "${DZSDK_MOC_EXECUTABLE}" CACHE FILEPATH "Path to the SDK-vendored Qt4 moc" FORCE)
set(QT_VERSION_MAJOR 4)
set(QT_VERSION_MINOR 8)

message(STATUS "Daz Studio SDK: ${DAZ_SDK_DIR}")
message(STATUS "Daz Studio SDK arch: ${DZ_MIXED_PLATFORM} (${DZ_PLATFORM}, ${CMAKE_SIZEOF_VOID_P}-byte pointers)")
message(STATUS "Daz Studio SDK dzcore: ${DAZ_SDK_DZCORE_LIB}")
message(STATUS "Daz Studio SDK Qt libs: ${DAZ_SDK_QT_LIB_DIR}")
