// FormulaCompiler.h -- parses a single formula object from a morph's `formulas_json`
// (one element of the top-level JSON array; an object with "output" and "operations"
// fields) into the SDK-independent FormulaIR. Pure function from JSON to IR: no Daz
// Studio SDK types, no evaluation, no I/O.

#pragma once

#include <stdexcept>
#include <variant>
#include <vector>

#include <nlohmann/json.hpp>

#include "FormulaIR.h"

namespace injector_core {

// Thrown when a formula's "operations" array doesn't match either the algebra shape
// (push/mult/neg chain) or the fixed spline shape (push-run + terminal spline op).
class FormulaCompileError : public std::runtime_error {
public:
    explicit FormulaCompileError(const std::string& message) : std::runtime_error(message) {}
};

// Compiles one formula object (e.g. `formulas_json[i]`, with an "operations" array)
// into either an AlgebraFormula or a SplineFormula. Throws FormulaCompileError on any
// shape that doesn't match the closed, known operator vocabulary
// (push/mult/neg/spline_tcb/spline_linear).
//
// NOTE: this overload discards the entry's "stage". It is retained for callers that
// genuinely only care about a single entry's body (and for the existing unit tests);
// anything attaching formulas to a live property must use compileFormulaSet instead,
// or multi-entry morphs silently lose their combination semantics (beads-w56).
std::variant<AlgebraFormula, SplineFormula> compileFormula(const nlohmann::json& formula);

// Compiles one formulas_json array entry, preserving its "stage".
CompiledFormula compileFormulaEntry(const nlohmann::json& formula);

// Compiles a whole `formulas_json` array -- every entry for one morph, in array order.
//
// Order is load-bearing: the returned vector's order is the order the resulting
// controllers must be chained onto the target property, because Daz evaluates a
// property's controller list front-to-back, each controller transforming the running
// value produced by the previous one. Reordering a Product entry ahead of the Sum
// entry it is meant to scale changes the result.
//
// ATOMIC: throws FormulaCompileError (naming the offending entry index) if ANY entry
// fails to compile, rather than returning a partial set. A partially-compiled set is
// worse than none -- dropping, say, a `stage:"mult"` corrective gate would leave the
// morph firing at full strength unconditionally instead of being switched off.
// Callers should treat the throw as "inject the morph without its formulas".
//
// Throws FormulaCompileError if `formulas` is not a JSON array.
std::vector<CompiledFormula> compileFormulaSet(const nlohmann::json& formulas);

}  // namespace injector_core
