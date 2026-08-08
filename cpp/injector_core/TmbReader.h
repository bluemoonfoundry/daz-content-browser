// TmbReader: zero-copy reader for the .tmb (Turbo Morph Binary) format.
//
// Layout (must byte-match src/tmb_format.py exactly):
//   HEADER (16 bytes, little-endian):
//     magic          char[4]   "TMB1"
//     vertex_count   int32_t   SIGNED -- real .dsf files use -1 as a documented
//                               DSON sentinel meaning "unspecified/same as base mesh"
//     delta_count    uint32_t
//     reserved       4 bytes, zero
//   DATA: delta_count x 16-byte records:
//     vertex_index   uint32_t
//     dx, dy, dz     float
//
// TmbReader memory-maps the file (Windows CreateFileMappingA/MapViewOfFile) and
// exposes the delta array as a zero-copy std::span over the mapped bytes -- no
// per-read heap allocation of the delta array, to stay within the <100ms
// injection budget.
//
// This header/implementation must remain free of any Daz Studio SDK or Qt
// dependency -- it is part of injector_core, the SDK-independent layer.

#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>

// This header is included from two targets built at *different* language
// standards: injector_core (C++20, for std::span) and daz_plugin (C++17 -- it
// cannot be raised, because the vendored Qt 4.8 headers use `register` as a
// storage-class specifier, which MSVC rejects outright under /std:c++20; see
// cpp/injector_core/CMakeLists.txt). So the std::span accessor is compiled in
// only where the standard actually provides it, and the always-available
// deltaData()/deltaCount() pair is what daz_plugin (InjectorCore) uses.
//
// The class's *data members* are identical under both standards (a raw pointer
// plus a count -- std::span is deliberately not a member), so there is no ODR /
// layout mismatch between the two translation-unit sets.
#if defined( _MSVC_LANG )
#define INJECTOR_CORE_CPP_LANG _MSVC_LANG
#else
#define INJECTOR_CORE_CPP_LANG __cplusplus
#endif

#if INJECTOR_CORE_CPP_LANG >= 202002L
#define INJECTOR_CORE_HAS_SPAN 1
#include <span>
#endif

namespace injector_core {

// One vertex displacement record, exactly as laid out on disk (16 bytes).
#pragma pack(push, 1)
struct TmbDelta {
    uint32_t vertex_index;
    float dx;
    float dy;
    float dz;
};
#pragma pack(pop)

static_assert(sizeof(TmbDelta) == 16, "TmbDelta must be exactly 16 bytes to match the .tmb on-disk layout");

// Thrown on bad magic, truncated header, or truncated data section.
class TmbFormatError : public std::runtime_error {
public:
    explicit TmbFormatError(const std::string& message) : std::runtime_error(message) {}
};

// Memory-maps a .tmb file for the lifetime of this object and exposes its
// contents as a zero-copy view. Not copyable (owns an OS mapping handle);
// movable.
class TmbReader {
public:
    // Opens and maps `path`. Throws TmbFormatError on bad magic or truncation,
    // and std::runtime_error on OS-level failure to open/map the file.
    explicit TmbReader(const std::string& path);
    ~TmbReader();

    TmbReader(const TmbReader&) = delete;
    TmbReader& operator=(const TmbReader&) = delete;

    TmbReader(TmbReader&& other) noexcept;
    TmbReader& operator=(TmbReader&& other) noexcept;

    // Signed: -1 is a valid, documented "unspecified" sentinel from DSON.
    int32_t vertexCount() const noexcept { return vertex_count_; }

    // Zero-copy pointer/count view over the memory-mapped delta records.
    // Available under every supported standard; valid only for the lifetime of
    // this object (it points straight into the mapped view).
    const TmbDelta* deltaData() const noexcept { return deltas_data_; }
    size_t deltaCount() const noexcept { return deltas_count_; }

#ifdef INJECTOR_CORE_HAS_SPAN
    // Zero-copy view over the memory-mapped delta records.
    std::span<const TmbDelta> deltas() const noexcept {
        return std::span<const TmbDelta>(deltas_data_, deltas_count_);
    }
#endif

private:
    void* file_handle_ = nullptr;    // HANDLE
    void* mapping_handle_ = nullptr; // HANDLE
    void* mapped_base_ = nullptr;    // LPVOID, base of MapViewOfFile

    int32_t vertex_count_ = 0;
    const TmbDelta* deltas_data_ = nullptr;
    size_t deltas_count_ = 0;

    void close() noexcept;
};

} // namespace injector_core
