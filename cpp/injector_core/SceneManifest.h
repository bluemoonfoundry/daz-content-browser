// SceneManifest.h -- SDK-independent data model + JSON (de)serialization for the JIT
// morph loader's per-scene persistence payload (parent spec section 4.3.1). Records
// which morphs InjectorCore has injected into the scene so a saved .duf can restore
// them on reload without native Daz Studio missing-file dialogs, since the morphs'
// .dsf source files are never actually referenced by the saved scene (see the
// daz-content-browser-jhq epic notes). No Daz Studio SDK types anywhere in this file;
// the DzElementData wiring that attaches this payload to the live DzScene lives in
// daz_plugin instead.

#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace injector_core {

// One injected morph's persisted identity + current value. `target_figure` is stored
// alongside `guid` (not just guid) so re-injection on scene load can resolve the
// correct node via the same InjectorCore::resolveNodeForFigure mechanism beads-e9y
// already built, rather than inventing new node-identity tracking -- this mirrors
// MorphRecord::guid + MorphRecord::target_figure (MorphIndexReader.h).
struct InjectedMorphEntry {
    std::string guid;
    std::string target_figure;
    double val = 0.0;
};

// The full manifest payload, keyed under "jit_loader_manifest" in the serialized JSON:
//   {"jit_loader_manifest": {"version": "1.0", "injected_morphs": [...]}}
struct SceneManifest {
    std::string version = "1.0";
    std::vector<InjectedMorphEntry> injected_morphs;
};

// Serializes a manifest to its full wrapped JSON form (including the top-level
// "jit_loader_manifest" key).
nlohmann::json toJson(const SceneManifest& manifest);

// Thrown by fromJson when the top-level "jit_loader_manifest" key is missing, or an
// injected_morphs entry is missing a required field.
class SceneManifestParseError : public std::runtime_error {
public:
    explicit SceneManifestParseError(const std::string& message) : std::runtime_error(message) {}
};

// Parses a manifest from its full wrapped JSON form, as produced by toJson().
SceneManifest fromJson(const nlohmann::json& j);

}  // namespace injector_core
