#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

// Forward-declare the sqlite3 C handle so this header stays free of the
// sqlite3.h include (and its FetchContent-vendored amalgamation) for
// consumers that only need the type, not the C API.
struct sqlite3;
struct sqlite3_stmt;

namespace injector_core {

// Mirrors the columns of the `morphs` table in morph_index.db. Schema is
// Python-authoritative: see src/managers/morph_index_manager.py's _SCHEMA.
struct MorphRecord {
    int64_t morph_id = 0;
    std::string guid;
    std::string label;
    std::string name;
    std::string target_figure;
    std::string group_path;
    std::string source_dsf_path;
    std::string tmb_path;
    int64_t vertex_count = 0;
    int64_t delta_count = 0;
    double min_value = 0.0;
    double max_value = 1.0;
    bool is_clamped = true;
    std::optional<std::string> formulas_json;
    std::string content_hash;
    std::string indexed_at;
};

// Read-only sqlite3 C-API wrapper over morph_index.db. Never writes or
// migrates the schema -- opens the database read-only and only queries.
//
// Not copyable (owns a raw sqlite3* handle); movable.
class MorphIndexReader {
public:
    // Opens `db_path` read-only. Throws std::runtime_error if the database
    // cannot be opened (missing file, not a valid sqlite db, etc).
    explicit MorphIndexReader(const std::string& db_path);
    ~MorphIndexReader();

    MorphIndexReader(const MorphIndexReader&) = delete;
    MorphIndexReader& operator=(const MorphIndexReader&) = delete;
    MorphIndexReader(MorphIndexReader&& other) noexcept;
    MorphIndexReader& operator=(MorphIndexReader&& other) noexcept;

    // Looks up a single morph by its (globally unique) guid. Returns
    // std::nullopt if no row matches.
    std::optional<MorphRecord> find_by_guid(const std::string& guid) const;

    // Looks up a single morph scoped by (target_figure, name) -- mirrors the
    // same scoping morph_index_manager.py's rebuild_dependencies() uses,
    // since pathless "name:X" formula operands are figure-local, not
    // globally unique. Returns std::nullopt if no row matches, including
    // when the figure doesn't match (name found under a different figure).
    std::optional<MorphRecord> find_by_name(const std::string& target_figure,
                                             const std::string& name) const;

    // Reads precomputed morph_dependencies edges for `dependent_morph_id`,
    // returning the referenced_morph_id of each edge. Empty vector if there
    // are none.
    std::vector<int64_t> dependencies_of(int64_t dependent_morph_id) const;

private:
    sqlite3* db_ = nullptr;

    std::optional<MorphRecord> find_one(sqlite3_stmt* stmt) const;
};

}  // namespace injector_core
