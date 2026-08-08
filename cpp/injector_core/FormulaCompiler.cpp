#include "FormulaCompiler.h"

namespace injector_core {

namespace {

// A single push's operand, as pulled out of `{"op":"push", "url":...}` or
// `{"op":"push", "val":...}`. `val` may itself be a JSON array (control-point tuple)
// rather than a scalar; array pushes are only valid inside the fixed spline shape and
// are handled separately by tryCompileSpline.
struct ParsedPush {
    enum class Form { Url, ScalarVal, ArrayVal } form;
    std::string url;
    double scalar = 0.0;
    const nlohmann::json* array = nullptr;  // points into the owning formula json
};

ParsedPush parsePush(const nlohmann::json& op) {
    if (op.contains("url")) {
        if (!op.at("url").is_string()) {
            throw FormulaCompileError("push op's \"url\" field is not a string");
        }
        return ParsedPush{ParsedPush::Form::Url, op.at("url").get<std::string>(), 0.0, nullptr};
    }
    if (op.contains("val")) {
        const nlohmann::json& val = op.at("val");
        if (val.is_array()) {
            return ParsedPush{ParsedPush::Form::ArrayVal, "", 0.0, &val};
        }
        if (val.is_number()) {
            return ParsedPush{ParsedPush::Form::ScalarVal, "", val.get<double>(), nullptr};
        }
        throw FormulaCompileError("push op's \"val\" field is neither a number nor an array");
    }
    throw FormulaCompileError("push op has neither \"url\" nor \"val\"");
}

SplineKey parseControlPoint(const nlohmann::json& arr) {
    if (arr.size() == 5) {
        SplineKey k;
        k.key = arr.at(0).get<double>();
        k.value = arr.at(1).get<double>();
        k.t = arr.at(2).get<double>();
        k.c = arr.at(3).get<double>();
        k.b = arr.at(4).get<double>();
        k.has_tcb = true;
        return k;
    }
    if (arr.size() == 2) {
        SplineKey k;
        k.key = arr.at(0).get<double>();
        k.value = arr.at(1).get<double>();
        k.has_tcb = false;
        return k;
    }
    throw FormulaCompileError("spline control-point push array has unexpected size " +
                               std::to_string(arr.size()) + " (expected 2 or 5)");
}

// Recognizes the fixed spline shape:
//   push(driving url), push(control-point)*N, push(key count N), spline_tcb|spline_linear
// Returns true and fills `out` if `ops` matches; returns false (without throwing) if
// the terminal op isn't a spline op at all, so the caller can fall back to algebra
// parsing. Throws FormulaCompileError if the terminal op IS a spline op but the
// preceding ops don't match the required shape.
bool tryCompileSpline(const nlohmann::json& ops, SplineFormula& out) {
    if (ops.empty()) {
        return false;
    }
    const nlohmann::json& terminal = ops.back();
    const std::string terminalOp = terminal.value("op", "");
    if (terminalOp != "spline_tcb" && terminalOp != "spline_linear") {
        return false;
    }
    out.tcb_interpolation = (terminalOp == "spline_tcb");

    // Need at least: driving push, >=1 control-point push, key-count push, spline op.
    if (ops.size() < 4) {
        throw FormulaCompileError("spline formula too short: expected push(url), "
                                   ">=1 push(control point), push(count), " +
                                   terminalOp);
    }

    // ops[0] must be the driving push:url.
    const nlohmann::json& first = ops.front();
    if (first.value("op", "") != "push") {
        throw FormulaCompileError("spline formula's first op is not \"push\"");
    }
    ParsedPush driving = parsePush(first);
    if (driving.form != ParsedPush::Form::Url) {
        throw FormulaCompileError("spline formula's first push is not a url push "
                                   "(driving operand)");
    }
    out.driving_operand = driving.url;

    // ops[size-2] must be push(N) -- the key count, as a scalar.
    const nlohmann::json& countOp = ops[ops.size() - 2];
    if (countOp.value("op", "") != "push") {
        throw FormulaCompileError("spline formula's second-to-last op is not \"push\" "
                                   "(expected key count)");
    }
    ParsedPush countPush = parsePush(countOp);
    if (countPush.form != ParsedPush::Form::ScalarVal) {
        throw FormulaCompileError("spline formula's key-count push is not a scalar");
    }
    const double count = countPush.scalar;

    // ops[1 .. size-3] are the control-point pushes.
    out.keys.clear();
    for (size_t i = 1; i + 2 < ops.size(); ++i) {
        const nlohmann::json& cp = ops[i];
        if (cp.value("op", "") != "push") {
            throw FormulaCompileError("spline formula's control-point op at index " +
                                       std::to_string(i) + " is not \"push\"");
        }
        ParsedPush parsed = parsePush(cp);
        if (parsed.form != ParsedPush::Form::ArrayVal) {
            throw FormulaCompileError("spline formula's control-point push at index " +
                                       std::to_string(i) + " is not an array value");
        }
        out.keys.push_back(parseControlPoint(*parsed.array));
    }

    if (static_cast<double>(out.keys.size()) != count) {
        throw FormulaCompileError("spline formula's key count push (" +
                                   std::to_string(count) + ") does not match the number "
                                   "of control-point pushes (" +
                                   std::to_string(out.keys.size()) + ")");
    }

    return true;
}

AlgebraFormula compileAlgebra(const nlohmann::json& ops) {
    AlgebraFormula formula;
    formula.ops.reserve(ops.size());

    for (const auto& op : ops) {
        const std::string kind = op.value("op", "");
        if (kind == "push") {
            ParsedPush parsed = parsePush(op);
            switch (parsed.form) {
                case ParsedPush::Form::Url:
                    formula.ops.emplace_back(PushOperand{parsed.url});
                    break;
                case ParsedPush::Form::ScalarVal:
                    formula.ops.emplace_back(PushConst{parsed.scalar});
                    break;
                case ParsedPush::Form::ArrayVal:
                    throw FormulaCompileError(
                        "push of an array value outside the spline control-point shape");
            }
        } else if (kind == "mult") {
            formula.ops.emplace_back(AlgebraOp{AlgebraOp::Mult});
        } else if (kind == "neg") {
            formula.ops.emplace_back(AlgebraOp{AlgebraOp::Neg});
        } else if (kind == "spline_tcb" || kind == "spline_linear") {
            // Should have been consumed by tryCompileSpline already; if we get here
            // the formula mixed a spline op into a non-spline-shaped stack.
            throw FormulaCompileError("\"" + kind +
                                       "\" op found outside the fixed spline formula shape");
        } else {
            throw FormulaCompileError("unknown formula op \"" + kind + "\"");
        }
    }

    return formula;
}

}  // namespace

std::variant<AlgebraFormula, SplineFormula> compileFormula(const nlohmann::json& formula) {
    if (!formula.contains("operations") || !formula.at("operations").is_array()) {
        throw FormulaCompileError("formula object has no \"operations\" array");
    }
    const nlohmann::json& ops = formula.at("operations");

    SplineFormula spline;
    if (tryCompileSpline(ops, spline)) {
        return spline;
    }

    return compileAlgebra(ops);
}

}  // namespace injector_core
