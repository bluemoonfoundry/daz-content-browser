#include "MorphIndexReader.h"

#include <sqlite3.h>

#include <stdexcept>
#include <utility>

namespace injector_core {

namespace {

constexpr const char* kFindByGuidSql =
    "SELECT morph_id, guid, label, name, target_figure, group_path, "
    "source_dsf_path, tmb_path, vertex_count, delta_count, min_value, "
    "max_value, is_clamped, formulas_json, content_hash, indexed_at "
    "FROM morphs WHERE guid = ?;";

constexpr const char* kFindByIdSql =
    "SELECT morph_id, guid, label, name, target_figure, group_path, "
    "source_dsf_path, tmb_path, vertex_count, delta_count, min_value, "
    "max_value, is_clamped, formulas_json, content_hash, indexed_at "
    "FROM morphs WHERE morph_id = ?;";

constexpr const char* kFindByNameSql =
    "SELECT morph_id, guid, label, name, target_figure, group_path, "
    "source_dsf_path, tmb_path, vertex_count, delta_count, min_value, "
    "max_value, is_clamped, formulas_json, content_hash, indexed_at "
    "FROM morphs WHERE target_figure = ? AND name = ?;";

constexpr const char* kDependenciesOfSql =
    "SELECT referenced_morph_id FROM morph_dependencies WHERE dependent_morph_id = ?;";

std::string column_text(sqlite3_stmt* stmt, int col) {
    const unsigned char* text = sqlite3_column_text(stmt, col);
    if (text == nullptr) {
        return std::string();
    }
    return std::string(reinterpret_cast<const char*>(text));
}

std::optional<std::string> column_text_nullable(sqlite3_stmt* stmt, int col) {
    if (sqlite3_column_type(stmt, col) == SQLITE_NULL) {
        return std::nullopt;
    }
    return column_text(stmt, col);
}

MorphRecord row_to_record(sqlite3_stmt* stmt) {
    MorphRecord record;
    record.morph_id = sqlite3_column_int64(stmt, 0);
    record.guid = column_text(stmt, 1);
    record.label = column_text(stmt, 2);
    record.name = column_text(stmt, 3);
    record.target_figure = column_text(stmt, 4);
    record.group_path = column_text(stmt, 5);
    record.source_dsf_path = column_text(stmt, 6);
    record.tmb_path = column_text(stmt, 7);
    record.vertex_count = sqlite3_column_int64(stmt, 8);
    record.delta_count = sqlite3_column_int64(stmt, 9);
    record.min_value = sqlite3_column_double(stmt, 10);
    record.max_value = sqlite3_column_double(stmt, 11);
    record.is_clamped = sqlite3_column_int(stmt, 12) != 0;
    record.formulas_json = column_text_nullable(stmt, 13);
    record.content_hash = column_text(stmt, 14);
    record.indexed_at = column_text(stmt, 15);
    return record;
}

}  // namespace

MorphIndexReader::MorphIndexReader(const std::string& db_path) {
    // Read-only, no implicit database creation: SQLITE_OPEN_READONLY. The
    // schema is Python-authoritative -- this reader never writes/migrates.
    int rc = sqlite3_open_v2(db_path.c_str(), &db_, SQLITE_OPEN_READONLY, nullptr);
    if (rc != SQLITE_OK) {
        std::string message = "MorphIndexReader: failed to open '" + db_path +
                               "': " + (db_ != nullptr ? sqlite3_errmsg(db_) : "unknown error");
        if (db_ != nullptr) {
            sqlite3_close(db_);
            db_ = nullptr;
        }
        throw std::runtime_error(message);
    }
}

MorphIndexReader::~MorphIndexReader() {
    if (db_ != nullptr) {
        sqlite3_close(db_);
        db_ = nullptr;
    }
}

MorphIndexReader::MorphIndexReader(MorphIndexReader&& other) noexcept : db_(other.db_) {
    other.db_ = nullptr;
}

MorphIndexReader& MorphIndexReader::operator=(MorphIndexReader&& other) noexcept {
    if (this != &other) {
        if (db_ != nullptr) {
            sqlite3_close(db_);
        }
        db_ = other.db_;
        other.db_ = nullptr;
    }
    return *this;
}

std::optional<MorphRecord> MorphIndexReader::find_one(sqlite3_stmt* stmt) const {
    std::optional<MorphRecord> result;
    int rc = sqlite3_step(stmt);
    if (rc == SQLITE_ROW) {
        result = row_to_record(stmt);
    } else if (rc != SQLITE_DONE) {
        sqlite3_finalize(stmt);
        throw std::runtime_error(std::string("MorphIndexReader: query failed: ") +
                                  sqlite3_errmsg(db_));
    }
    sqlite3_finalize(stmt);
    return result;
}

std::optional<MorphRecord> MorphIndexReader::find_by_guid(const std::string& guid) const {
    sqlite3_stmt* stmt = nullptr;
    int rc = sqlite3_prepare_v2(db_, kFindByGuidSql, -1, &stmt, nullptr);
    if (rc != SQLITE_OK) {
        throw std::runtime_error(std::string("MorphIndexReader: failed to prepare find_by_guid: ") +
                                  sqlite3_errmsg(db_));
    }
    sqlite3_bind_text(stmt, 1, guid.c_str(), -1, SQLITE_TRANSIENT);
    return find_one(stmt);
}

std::optional<MorphRecord> MorphIndexReader::find_by_id(int64_t morph_id) const {
    sqlite3_stmt* stmt = nullptr;
    int rc = sqlite3_prepare_v2(db_, kFindByIdSql, -1, &stmt, nullptr);
    if (rc != SQLITE_OK) {
        throw std::runtime_error(std::string("MorphIndexReader: failed to prepare find_by_id: ") +
                                  sqlite3_errmsg(db_));
    }
    sqlite3_bind_int64(stmt, 1, morph_id);
    return find_one(stmt);
}

std::optional<MorphRecord> MorphIndexReader::find_by_name(const std::string& target_figure,
                                                            const std::string& name) const {
    sqlite3_stmt* stmt = nullptr;
    int rc = sqlite3_prepare_v2(db_, kFindByNameSql, -1, &stmt, nullptr);
    if (rc != SQLITE_OK) {
        throw std::runtime_error(std::string("MorphIndexReader: failed to prepare find_by_name: ") +
                                  sqlite3_errmsg(db_));
    }
    sqlite3_bind_text(stmt, 1, target_figure.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(stmt, 2, name.c_str(), -1, SQLITE_TRANSIENT);
    return find_one(stmt);
}

std::vector<int64_t> MorphIndexReader::dependencies_of(int64_t dependent_morph_id) const {
    sqlite3_stmt* stmt = nullptr;
    int rc = sqlite3_prepare_v2(db_, kDependenciesOfSql, -1, &stmt, nullptr);
    if (rc != SQLITE_OK) {
        throw std::runtime_error(
            std::string("MorphIndexReader: failed to prepare dependencies_of: ") +
            sqlite3_errmsg(db_));
    }
    sqlite3_bind_int64(stmt, 1, dependent_morph_id);

    std::vector<int64_t> result;
    while (true) {
        rc = sqlite3_step(stmt);
        if (rc == SQLITE_ROW) {
            result.push_back(sqlite3_column_int64(stmt, 0));
        } else if (rc == SQLITE_DONE) {
            break;
        } else {
            sqlite3_finalize(stmt);
            throw std::runtime_error(
                std::string("MorphIndexReader: query failed: ") + sqlite3_errmsg(db_));
        }
    }
    sqlite3_finalize(stmt);
    return result;
}

}  // namespace injector_core
