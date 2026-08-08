#include "MorphIndexReader.h"

#include <algorithm>
#include <gtest/gtest.h>

using injector_core::MorphIndexReader;
using injector_core::MorphRecord;

namespace {

// Populated by cpp/tests/fixtures/build_fixture_db.py from the real
// repo-root morph_index.db. Contains exactly 3 morphs:
//   3027 - "BaseFeminine_body_cbs_thigh_x115n_l" (has formulas_json, 2 deps)
//   3074 - "body_cbs_thigh_x115n_l" (formulas_json NULL, referenced by 3027)
//   3026 - "BaseFeminine_body_bs_Body" (formulas_json NULL, referenced by 3027)
// All three share target_figure "AJC Energy Sportswear Leggings_244823".
constexpr const char* kFixtureDbPath = FIXTURE_DB_PATH;

constexpr const char* kFigure = "AJC Energy Sportswear Leggings_244823";

constexpr int64_t kDependentMorphId = 3027;
constexpr const char* kDependentGuid =
    "/data/adeilsonjc/AJC%20Energy%20Sportswear%20Outfit/AJC%20Energy%20Sportswear%20Leggings/"
    "Morphs/adeilsonjc/Base/BaseFeminine_body_cbs_thigh_x115n_l.dsf";
constexpr const char* kDependentName = "BaseFeminine_body_cbs_thigh_x115n_l";

}  // namespace

TEST(MorphIndexReader, FindByGuidReturnsCorrectFields) {
    MorphIndexReader reader(kFixtureDbPath);

    auto record = reader.find_by_guid(kDependentGuid);

    ASSERT_TRUE(record.has_value());
    EXPECT_EQ(record->morph_id, kDependentMorphId);
    EXPECT_EQ(record->guid, kDependentGuid);
    EXPECT_EQ(record->label, kDependentName);
    EXPECT_EQ(record->name, kDependentName);
    EXPECT_EQ(record->target_figure, kFigure);
    EXPECT_EQ(record->group_path, "/Hidden/Base Correctives/Legs");
    EXPECT_EQ(record->vertex_count, 244823);
    EXPECT_EQ(record->delta_count, 6179);
    EXPECT_DOUBLE_EQ(record->min_value, 0.0);
    EXPECT_DOUBLE_EQ(record->max_value, 1.0);
    EXPECT_TRUE(record->is_clamped);
    ASSERT_TRUE(record->formulas_json.has_value());
    EXPECT_NE(record->formulas_json->find("BaseFeminine_body_cbs_thigh_x115n_l"),
              std::string::npos);
    EXPECT_FALSE(record->content_hash.empty());
    EXPECT_FALSE(record->indexed_at.empty());
}

TEST(MorphIndexReader, FindByGuidReturnsNulloptForUnknownGuid) {
    MorphIndexReader reader(kFixtureDbPath);

    auto record = reader.find_by_guid("/data/does/not/exist.dsf");

    EXPECT_FALSE(record.has_value());
}

TEST(MorphIndexReader, FindByGuidHandlesNullFormulasJson) {
    MorphIndexReader reader(kFixtureDbPath);

    // morph 3074, "body_cbs_thigh_x115n_l", has formulas_json = NULL.
    auto record = reader.find_by_name(kFigure, "body_cbs_thigh_x115n_l");

    ASSERT_TRUE(record.has_value());
    EXPECT_FALSE(record->formulas_json.has_value());
}

TEST(MorphIndexReader, FindByNameResolvesScopedByFigure) {
    MorphIndexReader reader(kFixtureDbPath);

    auto record = reader.find_by_name(kFigure, kDependentName);

    ASSERT_TRUE(record.has_value());
    EXPECT_EQ(record->morph_id, kDependentMorphId);
    EXPECT_EQ(record->name, kDependentName);
    EXPECT_EQ(record->target_figure, kFigure);
}

TEST(MorphIndexReader, FindByNameReturnsNulloptForWrongFigure) {
    MorphIndexReader reader(kFixtureDbPath);

    // Same name, but scoped to a figure that doesn't own this morph.
    auto record = reader.find_by_name("SomeOtherFigure_999999", kDependentName);

    EXPECT_FALSE(record.has_value());
}

TEST(MorphIndexReader, FindByNameReturnsNulloptForUnknownName) {
    MorphIndexReader reader(kFixtureDbPath);

    auto record = reader.find_by_name(kFigure, "NameThatDoesNotExist");

    EXPECT_FALSE(record.has_value());
}

TEST(MorphIndexReader, DependenciesOfReturnsCorrectEdgeSet) {
    MorphIndexReader reader(kFixtureDbPath);

    auto deps = reader.dependencies_of(kDependentMorphId);

    std::sort(deps.begin(), deps.end());
    std::vector<int64_t> expected = {3026, 3074};
    EXPECT_EQ(deps, expected);
}

TEST(MorphIndexReader, DependenciesOfReturnsEmptyForMorphWithNoDeps) {
    MorphIndexReader reader(kFixtureDbPath);

    // 3074 is a leaf in this fixture -- nothing depends-on-edges *from* it.
    auto deps = reader.dependencies_of(3074);

    EXPECT_TRUE(deps.empty());
}

TEST(MorphIndexReader, DependenciesOfReturnsEmptyForUnknownMorphId) {
    MorphIndexReader reader(kFixtureDbPath);

    auto deps = reader.dependencies_of(999999);

    EXPECT_TRUE(deps.empty());
}
