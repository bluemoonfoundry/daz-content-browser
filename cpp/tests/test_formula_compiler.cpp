// Tests for FormulaCompiler, driven by real formulas_json fixtures pulled from
// morph_index.db (see cpp/tests/fixtures/). No Daz Studio SDK involved.

#include "FormulaCompiler.h"

#include <fstream>
#include <sstream>

#include <gtest/gtest.h>
#include <nlohmann/json.hpp>

namespace {

nlohmann::json loadFixture(const std::string& name) {
    std::ifstream in(std::string(FIXTURES_DIR) + "/" + name);
    if (!in) {
        throw std::runtime_error("could not open fixture: " + name);
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    return nlohmann::json::parse(ss.str());
}

}  // namespace

// pJCMCloakBend_m90: push:url, push:val(-0.01111111), mult -- a plain algebra formula.
TEST(FormulaCompiler, CompilesAlgebraFixture) {
    nlohmann::json formula = loadFixture("algebra_pJCMCloakBend_m90.json");

    auto ir = injector_core::compileFormula(formula);

    ASSERT_TRUE(std::holds_alternative<injector_core::AlgebraFormula>(ir));
    const auto& algebra = std::get<injector_core::AlgebraFormula>(ir);

    ASSERT_EQ(algebra.ops.size(), 3u);

    ASSERT_TRUE(std::holds_alternative<injector_core::PushOperand>(algebra.ops[0]));
    EXPECT_EQ(std::get<injector_core::PushOperand>(algebra.ops[0]).operand,
              "Cloak:/data/%21Daz%20Original/G3HoodedCloak/Hooded%20Cloak/"
              "GnHdCloak_G3_23369.dsf#Cloak?rotation/x");

    ASSERT_TRUE(std::holds_alternative<injector_core::PushConst>(algebra.ops[1]));
    EXPECT_DOUBLE_EQ(std::get<injector_core::PushConst>(algebra.ops[1]).value, -0.01111111);

    ASSERT_TRUE(std::holds_alternative<injector_core::AlgebraOp>(algebra.ops[2]));
    EXPECT_EQ(std::get<injector_core::AlgebraOp>(algebra.ops[2]).kind,
              injector_core::AlgebraOp::Mult);
}

// body_cbs_foot_Back_l: push:url, push:[27.6,0,0,0,0], push:[65,1,0,0,0], push:2,
// spline_tcb -- a keyed TCB spline formula with two control points.
TEST(FormulaCompiler, CompilesSplineTcbFixture) {
    nlohmann::json formula = loadFixture("spline_body_cbs_foot_Back_l.json");

    auto ir = injector_core::compileFormula(formula);

    ASSERT_TRUE(std::holds_alternative<injector_core::SplineFormula>(ir));
    const auto& spline = std::get<injector_core::SplineFormula>(ir);

    EXPECT_EQ(spline.driving_operand,
              "Genesis9/l_foot:/data/Daz%203D/Genesis%209/Base/Genesis9.dsf#l_foot?rotation/x");
    EXPECT_TRUE(spline.tcb_interpolation);

    ASSERT_EQ(spline.keys.size(), 2u);

    EXPECT_TRUE(spline.keys[0].has_tcb);
    EXPECT_DOUBLE_EQ(spline.keys[0].key, 27.6);
    EXPECT_DOUBLE_EQ(spline.keys[0].value, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[0].t, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[0].c, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[0].b, 0.0);

    EXPECT_TRUE(spline.keys[1].has_tcb);
    EXPECT_DOUBLE_EQ(spline.keys[1].key, 65.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].value, 1.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].t, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].c, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].b, 0.0);
}

// pJCMFlexCollarUpperFront_26_R: push:url, push:[0,0], push:[26,1], push:2,
// spline_linear -- a keyed linear spline formula (no tangents), inlined here rather
// than as a checked-in fixture since it's only needed to exercise the has_tcb=false
// path in addition to the two required fixtures above.
TEST(FormulaCompiler, CompilesSplineLinearInline) {
    nlohmann::json formula = nlohmann::json::parse(R"({
        "output": "BW%20PKAO%20Shoulder%20Protector:#pJCMFlexCollarUpperFront_26_R?value",
        "operations": [
            { "op": "push", "url": "Genesis8_1Male/rCollar:/data/Daz%203D/Genesis%208/Male%208_1/Genesis8_1Male.dsf#rCollar?rotation/y" },
            { "op": "push", "val": [0, 0] },
            { "op": "push", "val": [26, 1] },
            { "op": "push", "val": 2 },
            { "op": "spline_linear" }
        ]
    })");

    auto ir = injector_core::compileFormula(formula);

    ASSERT_TRUE(std::holds_alternative<injector_core::SplineFormula>(ir));
    const auto& spline = std::get<injector_core::SplineFormula>(ir);

    EXPECT_EQ(spline.driving_operand,
              "Genesis8_1Male/rCollar:/data/Daz%203D/Genesis%208/Male%208_1/"
              "Genesis8_1Male.dsf#rCollar?rotation/y");
    EXPECT_FALSE(spline.tcb_interpolation);

    ASSERT_EQ(spline.keys.size(), 2u);
    EXPECT_FALSE(spline.keys[0].has_tcb);
    EXPECT_DOUBLE_EQ(spline.keys[0].key, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[0].value, 0.0);
    EXPECT_FALSE(spline.keys[1].has_tcb);
    EXPECT_DOUBLE_EQ(spline.keys[1].key, 26.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].value, 1.0);
}

TEST(FormulaCompiler, ThrowsOnUnknownOp) {
    nlohmann::json formula = nlohmann::json::parse(R"({
        "output": "x",
        "operations": [
            { "op": "push", "val": 1 },
            { "op": "clamp" }
        ]
    })");

    EXPECT_THROW(injector_core::compileFormula(formula), injector_core::FormulaCompileError);
}
