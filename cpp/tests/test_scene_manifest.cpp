#include "SceneManifest.h"

#include <gtest/gtest.h>

using injector_core::InjectedMorphEntry;
using injector_core::SceneManifest;
using injector_core::SceneManifestParseError;

TEST(SceneManifest, RoundTripPreservesAllFieldsForMultipleEntries) {
    SceneManifest manifest;
    manifest.version = "1.0";
    manifest.injected_morphs = {
        InjectedMorphEntry{"/data/vendor/product/Morphs/a.dsf", "Genesis9_1", 0.75},
        InjectedMorphEntry{"/data/vendor/product/Morphs/b.dsf", "Genesis9_2", -0.5},
        InjectedMorphEntry{"/data/vendor/product/Morphs/c.dsf", "Genesis9_1", 1.0},
    };

    nlohmann::json serialized = injector_core::toJson(manifest);
    SceneManifest roundTripped = injector_core::fromJson(serialized);

    EXPECT_EQ(roundTripped.version, manifest.version);
    ASSERT_EQ(roundTripped.injected_morphs.size(), manifest.injected_morphs.size());
    for (size_t i = 0; i < manifest.injected_morphs.size(); ++i) {
        EXPECT_EQ(roundTripped.injected_morphs[i].guid, manifest.injected_morphs[i].guid);
        EXPECT_EQ(roundTripped.injected_morphs[i].target_figure,
                  manifest.injected_morphs[i].target_figure);
        EXPECT_DOUBLE_EQ(roundTripped.injected_morphs[i].val, manifest.injected_morphs[i].val);
    }
}

TEST(SceneManifest, RoundTripPreservesEmptyInjectedMorphs) {
    SceneManifest manifest;
    manifest.version = "1.0";

    SceneManifest roundTripped = injector_core::fromJson(injector_core::toJson(manifest));

    EXPECT_EQ(roundTripped.version, "1.0");
    EXPECT_TRUE(roundTripped.injected_morphs.empty());
}

TEST(SceneManifest, ToJsonWrapsPayloadUnderTopLevelKey) {
    SceneManifest manifest;
    manifest.injected_morphs = {InjectedMorphEntry{"guid1", "Figure1", 0.5}};

    nlohmann::json serialized = injector_core::toJson(manifest);

    ASSERT_TRUE(serialized.contains("jit_loader_manifest"));
    const auto& payload = serialized["jit_loader_manifest"];
    EXPECT_EQ(payload["version"], "1.0");
    ASSERT_EQ(payload["injected_morphs"].size(), 1u);
    EXPECT_EQ(payload["injected_morphs"][0]["guid"], "guid1");
    EXPECT_EQ(payload["injected_morphs"][0]["target_figure"], "Figure1");
    EXPECT_DOUBLE_EQ(payload["injected_morphs"][0]["val"].get<double>(), 0.5);
}

TEST(SceneManifest, FromJsonThrowsWhenTopLevelKeyMissing) {
    nlohmann::json malformed = nlohmann::json::object({{"not_the_right_key", true}});

    EXPECT_THROW(injector_core::fromJson(malformed), SceneManifestParseError);
}

TEST(SceneManifest, FromJsonThrowsWhenEntryMissingRequiredField) {
    nlohmann::json malformed = nlohmann::json::parse(R"({
        "jit_loader_manifest": {
            "version": "1.0",
            "injected_morphs": [
                {"guid": "guid1", "val": 0.5}
            ]
        }
    })");

    EXPECT_THROW(injector_core::fromJson(malformed), SceneManifestParseError);
}
