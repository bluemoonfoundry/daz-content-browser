#include "TmbReader.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstring>
#include <utility>

namespace injector_core {

namespace {

// Must byte-match src/tmb_format.py's `_HEADER = struct.Struct("<4siI4x")`:
// magic(4) + vertex_count:int32(4) + delta_count:uint32(4) + reserved(4) = 16 bytes.
#pragma pack(push, 1)
struct TmbHeader {
    char magic[4];
    int32_t vertex_count;
    uint32_t delta_count;
    uint32_t reserved;
};
#pragma pack(pop)

static_assert(sizeof(TmbHeader) == 16, "TmbHeader must be exactly 16 bytes to match the .tmb on-disk layout");

constexpr char kMagic[4] = {'T', 'M', 'B', '1'};

std::string LastErrorMessage() {
    DWORD error = GetLastError();
    LPSTR buffer = nullptr;
    DWORD size = FormatMessageA(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, error, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), reinterpret_cast<LPSTR>(&buffer), 0, nullptr);
    std::string message = (size > 0 && buffer != nullptr) ? std::string(buffer, size) : std::string("unknown error");
    if (buffer != nullptr) {
        LocalFree(buffer);
    }
    return message;
}

} // namespace

TmbReader::TmbReader(const std::string& path) {
    HANDLE file = CreateFileA(
        path.c_str(),
        GENERIC_READ,
        FILE_SHARE_READ,
        nullptr,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        throw std::runtime_error("TmbReader: failed to open '" + path + "': " + LastErrorMessage());
    }
    file_handle_ = file;

    LARGE_INTEGER file_size{};
    if (!GetFileSizeEx(file, &file_size)) {
        std::string error = LastErrorMessage();
        close();
        throw std::runtime_error("TmbReader: failed to get file size for '" + path + "': " + error);
    }

    if (static_cast<unsigned long long>(file_size.QuadPart) < sizeof(TmbHeader)) {
        close();
        throw TmbFormatError("TmbReader: truncated .tmb file '" + path + "': header too short");
    }

    // Zero-length mapping is invalid on Windows; a header-only file (0 deltas)
    // still has a non-zero size (>= sizeof(TmbHeader)), so this is safe.
    HANDLE mapping = CreateFileMappingA(file, nullptr, PAGE_READONLY, 0, 0, nullptr);
    if (mapping == nullptr) {
        std::string error = LastErrorMessage();
        close();
        throw std::runtime_error("TmbReader: failed to create file mapping for '" + path + "': " + error);
    }
    mapping_handle_ = mapping;

    void* base = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0);
    if (base == nullptr) {
        std::string error = LastErrorMessage();
        close();
        throw std::runtime_error("TmbReader: failed to map view of '" + path + "': " + error);
    }
    mapped_base_ = base;

    const auto* header = reinterpret_cast<const TmbHeader*>(base);
    if (std::memcmp(header->magic, kMagic, sizeof(kMagic)) != 0) {
        close();
        throw TmbFormatError("TmbReader: bad magic bytes in '" + path + "' (not a TMB file)");
    }

    // Capture everything the error message (or the happy path below) needs
    // from the mapped header BEFORE calling close(), which unmaps the view
    // and invalidates `header`/`base`.
    const int32_t header_vertex_count = header->vertex_count;
    const uint32_t header_delta_count = header->delta_count;

    const uint64_t file_bytes = static_cast<uint64_t>(file_size.QuadPart);
    const uint64_t data_bytes = static_cast<uint64_t>(header_delta_count) * sizeof(TmbDelta);
    const uint64_t required_bytes = sizeof(TmbHeader) + data_bytes;
    if (file_bytes < required_bytes) {
        close();
        throw TmbFormatError(
            "TmbReader: truncated .tmb file '" + path + "': expected " + std::to_string(header_delta_count) +
            " deltas (" + std::to_string(data_bytes) + " bytes) but file only has " +
            std::to_string(file_bytes - sizeof(TmbHeader)) + " bytes of data");
    }

    vertex_count_ = header_vertex_count;

    const auto* data_start = reinterpret_cast<const TmbDelta*>(reinterpret_cast<const uint8_t*>(base) + sizeof(TmbHeader));
    deltas_ = std::span<const TmbDelta>(data_start, header->delta_count);
}

TmbReader::~TmbReader() {
    close();
}

TmbReader::TmbReader(TmbReader&& other) noexcept
    : file_handle_(other.file_handle_),
      mapping_handle_(other.mapping_handle_),
      mapped_base_(other.mapped_base_),
      vertex_count_(other.vertex_count_),
      deltas_(other.deltas_) {
    other.file_handle_ = nullptr;
    other.mapping_handle_ = nullptr;
    other.mapped_base_ = nullptr;
    other.vertex_count_ = 0;
    other.deltas_ = {};
}

TmbReader& TmbReader::operator=(TmbReader&& other) noexcept {
    if (this != &other) {
        close();
        file_handle_ = other.file_handle_;
        mapping_handle_ = other.mapping_handle_;
        mapped_base_ = other.mapped_base_;
        vertex_count_ = other.vertex_count_;
        deltas_ = other.deltas_;

        other.file_handle_ = nullptr;
        other.mapping_handle_ = nullptr;
        other.mapped_base_ = nullptr;
        other.vertex_count_ = 0;
        other.deltas_ = {};
    }
    return *this;
}

void TmbReader::close() noexcept {
    deltas_ = {};
    vertex_count_ = 0;

    if (mapped_base_ != nullptr) {
        UnmapViewOfFile(mapped_base_);
        mapped_base_ = nullptr;
    }
    if (mapping_handle_ != nullptr) {
        CloseHandle(static_cast<HANDLE>(mapping_handle_));
        mapping_handle_ = nullptr;
    }
    if (file_handle_ != nullptr) {
        CloseHandle(static_cast<HANDLE>(file_handle_));
        file_handle_ = nullptr;
    }
}

} // namespace injector_core
