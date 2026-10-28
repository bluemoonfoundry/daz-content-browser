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

// How one formulas_json array entry combines with the other entries targeting the
// same output property.
//
// A morph's formulas_json is an ARRAY, and multiple entries routinely drive the same
// output. Each entry carries an optional top-level "stage" key selecting how its
// result folds into the running value:
//
//   absent (or any unrecognized value) -> Sum     -- the implicit default; the entry's
//                                                    result is ADDED to the running value
//   "mult"                             -> Product -- the entry's result MULTIPLIES the
//                                                    running value
//
// Dropping this distinction is what made every multi-entry morph evaluate to only its
// last entry (see beads-w56): a spline-driven JCM gated by a `stage:"mult"` corrective
// switch would discard the spline entirely and emit just the gate value.
enum class FormulaStage {
    Sum,
    Product,
};

// The parsed pieces of a formulas_json entry's top-level "output" -- the property
// the entry actually DRIVES.
//
// "output" uses the same URL grammar as an operand ({"op":"push","url":...}):
//
//     [<label>':'] [<path>] '#' <element> ['?' <property>]
//
// e.g. "Genesis9:#body_cbs_forearm_y135n_l?value"                       (pathless)
//      "neck2:/data/.../Genesis9.dsf#neck2?center_point/x"              (path-form)
//
// daz_plugin's PropertySourceAdapter::parseOperand implements this same grammar for
// operand strings, but it is a Qt/QString API living in the SDK-dependent half of the
// project. injector_core must stay Qt- and SDK-free (it builds and tests in ordinary
// CI with no Daz Studio install), so the grammar is re-expressed here over std::string.
// The two must stay in sync; they are deliberately identical, field for field.
//
// `parsed` is false when the string carried no '#' at all -- the one shape neither
// half of the project attempts to interpret. Such an entry is treated as foreign
// (unclassifiable => not provably self => skipped), which is the safe direction.
struct OutputRef {
    std::string raw;       // the "output" string verbatim, for logging
    std::string label;     // before the first ':' (may be empty)
    std::string path;      // between ':' and '#' -- empty for the pathless form
    std::string element;   // between '#' and '?' -- a node name or a property name
    std::string property;  // after '?' -- "value", "center_point/x", ... (may be empty)
    bool parsed = false;
};

// One fully-compiled formulas_json array entry: its formula body plus the stage that
// says how the body combines with its siblings. `stage_explicit` records whether the
// JSON actually carried a "stage" key, purely so callers can log/diagnose the
// difference between an implicit and an explicit sum; it never affects arithmetic.
struct CompiledFormula {
    std::variant<AlgebraFormula, SplineFormula> body;
    FormulaStage stage = FormulaStage::Sum;
    bool stage_explicit = false;

    // Which property this entry drives. NOT necessarily the owning morph's own value
    // channel: 38% of the formula-bearing morphs in the production index carry at
    // least one entry whose output targets something else entirely -- a bone's
    // center_point/end_point, another morph's value (see daz-content-browser-2kv).
    // Callers must classify with isSelfOutput() before attaching anything.
    OutputRef output;
};

}  // namespace injector_core
