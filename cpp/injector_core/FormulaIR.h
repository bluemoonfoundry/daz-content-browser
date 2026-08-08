// FormulaIR.h -- SDK-independent intermediate representation for a compiled Daz
// morph formula (see docs/superpowers/specs/2026-08-07-morph-injector-core-design.md
// section 3.1). No Daz Studio SDK types anywhere in this file.

#pragma once

#include <string>
#include <variant>
#include <vector>

namespace injector_core {

// One non-terminal op in an AlgebraFormula's RPN op stack. `push` operations carry
// their operand separately via PushConst/PushOperand; AlgebraOp only covers the
// operators that don't push a value of their own.
struct AlgebraOp {
    enum Kind { Mult, Neg };
    Kind kind;
};

// A `push` of a literal numeric constant (formulas_json's `{"op":"push","val":<number>}`).
struct PushConst {
    double value;
};

// A `push` of an operand reference -- a `formulas_json` `url` string
// (`{"op":"push","url":"..."}`). Resolved later, against the live scene graph, by a
// downstream component (PropertySourceAdapter) that this file has no dependency on.
struct PushOperand {
    std::string operand;
};

// A formula that is a plain RPN algebra chain (push/mult/neg/... with no spline).
struct AlgebraFormula {
    std::vector<std::variant<AlgebraOp, PushConst, PushOperand>> ops;
};

// One control point of a keyed spline formula. For spline_tcb pushes (5-element
// [key, value, t, c, b] arrays) has_tcb is true and t/c/b are populated. For
// spline_linear pushes (2-element [key, value] arrays) has_tcb is false and t/c/b
// are zero-initialized and unused.
struct SplineKey {
    double key = 0.0;
    double value = 0.0;
    double t = 0.0;
    double c = 0.0;
    double b = 0.0;
    bool has_tcb = false;
};

// A formula that is a keyed spline: a single driving push:url, followed by N
// control-point pushes, a push of the key count, and a terminal spline_tcb/spline_linear.
struct SplineFormula {
    std::string driving_operand;  // the single push:url before the key pushes
    std::vector<SplineKey> keys;
    bool tcb_interpolation = false;  // true: spline_tcb, false: spline_linear
};

}  // namespace injector_core
