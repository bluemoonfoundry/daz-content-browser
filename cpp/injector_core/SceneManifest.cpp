#include "SceneManifest.h"

namespace injector_core {

nlohmann::json toJson(const SceneManifest& manifest) {
    nlohmann::json entries = nlohmann::json::array();
    for (const auto& entry : manifest.injected_morphs) {
        entries.push_back({
            {"guid", entry.guid},
            {"target_figure", entry.target_figure},
            {"val", entry.val},
        });
    }

    return {
        {"jit_loader_manifest",
         {
             {"version", manifest.version},
             {"injected_morphs", entries},
         }},
    };
}

SceneManifest fromJson(const nlohmann::json& j) {
    if (!j.contains("jit_loader_manifest")) {
        throw SceneManifestParseError("missing top-level \"jit_loader_manifest\" key");
    }
    const nlohmann::json& payload = j.at("jit_loader_manifest");

    SceneManifest manifest;
    manifest.version = payload.at("version").get<std::string>();

    for (const auto& entryJson : payload.at("injected_morphs")) {
        if (!entryJson.contains("guid") || !entryJson.contains("target_figure") ||
            !entryJson.contains("val")) {
            throw SceneManifestParseError(
                "injected_morphs entry missing required field (guid/target_figure/val)");
        }
        InjectedMorphEntry entry;
        entry.guid = entryJson.at("guid").get<std::string>();
        entry.target_figure = entryJson.at("target_figure").get<std::string>();
        entry.val = entryJson.at("val").get<double>();
        manifest.injected_morphs.push_back(std::move(entry));
    }

    return manifest;
}

}  // namespace injector_core
