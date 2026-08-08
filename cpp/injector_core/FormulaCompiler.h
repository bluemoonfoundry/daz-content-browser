// FormulaCompiler.h -- parses a single formula object from a morph's `formulas_json`
// (one element of the top-level JSON array; an object with "output" and "operations"
// fields) into the SDK-independent FormulaIR. Pure function from JSON to IR: no Daz
// Studio SDK types, no evaluation, no I/O.

#pragma once

#include <stdexcept>
#include <variant>

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
std::variant<AlgebraFormula, SplineFormula> compileFormula(const nlohmann::json& formula);

}  // namespace injector_core
