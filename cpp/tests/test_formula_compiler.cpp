// Tests for FormulaCompiler, driven by real formulas_json fixtures pulled from
// morph_index.db (see cpp/tests/fixtures/). No Daz Studio SDK involved.

#include "FormulaCompiler.h"

#include <fstream>
#include <sstream>
#include <string>
#include <vector>

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

// ---------------------------------------------------------------------------
// Multi-entry formulas_json: "stage" combination (beads-w56)
// ---------------------------------------------------------------------------
// Both fixtures below are verbatim formulas_json values pulled from the real
// production morph_index.db (morph_id 3027 and 70626). They are the two shapes
// that the pre-fix code silently mis-evaluated: it compiled each array entry
// independently, dropped "stage" entirely, and let each entry's controller
// clobber the previous one's.

// morph_id 3027, BaseFeminine_body_cbs_thigh_x115n_l: two algebra entries.
//   [0] (no stage -> implicit Sum)  push:url(thigh_x115n_l), push:1, mult
//   [1] stage:"mult" (Product)      push:url(BaseFeminine_body_bs_Body)
// Intended value = body_cbs_thigh_x115n_l * BaseFeminine_body_bs_Body.
TEST(FormulaCompiler, CompilesMultiEntryAlgebraWithMultStage) {
    nlohmann::json formulas =
        loadFixture("multientry_algebra_mult_BaseFeminine_body_cbs_thigh_x115n_l.json");
    ASSERT_TRUE(formulas.is_array());
    ASSERT_EQ(formulas.size(), 2u);

    std::vector<injector_core::CompiledFormula> set = injector_core::compileFormulaSet(formulas);
    ASSERT_EQ(set.size(), 2u);

    // Entry 0: implicit sum stage, algebra body `drivingA * 1`.
    EXPECT_EQ(set[0].stage, injector_core::FormulaStage::Sum);
    EXPECT_FALSE(set[0].stage_explicit);
    ASSERT_TRUE(std::holds_alternative<injector_core::AlgebraFormula>(set[0].body));
    const auto& first = std::get<injector_core::AlgebraFormula>(set[0].body);
    ASSERT_EQ(first.ops.size(), 3u);
    ASSERT_TRUE(std::holds_alternative<injector_core::PushOperand>(first.ops[0]));
    EXPECT_NE(std::get<injector_core::PushOperand>(first.ops[0]).operand.find(
                  "body_cbs_thigh_x115n_l?value"),
              std::string::npos);
    ASSERT_TRUE(std::holds_alternative<injector_core::PushConst>(first.ops[1]));
    EXPECT_DOUBLE_EQ(std::get<injector_core::PushConst>(first.ops[1]).value, 1.0);
    ASSERT_TRUE(std::holds_alternative<injector_core::AlgebraOp>(first.ops[2]));
    EXPECT_EQ(std::get<injector_core::AlgebraOp>(first.ops[2]).kind,
              injector_core::AlgebraOp::Mult);

    // Entry 1: THE REGRESSION -- explicit stage "mult" must survive to the IR.
    EXPECT_EQ(set[1].stage, injector_core::FormulaStage::Product);
    EXPECT_TRUE(set[1].stage_explicit);
    ASSERT_TRUE(std::holds_alternative<injector_core::AlgebraFormula>(set[1].body));
    const auto& second = std::get<injector_core::AlgebraFormula>(set[1].body);
    ASSERT_EQ(second.ops.size(), 1u);
    ASSERT_TRUE(std::holds_alternative<injector_core::PushOperand>(second.ops[0]));
    EXPECT_NE(std::get<injector_core::PushOperand>(second.ops[0]).operand.find(
                  "BaseFeminine_body_bs_Body?value"),
              std::string::npos);
}

// morph_id 70626, body_cbs_forearm_y135n_l: the near-universal real JCM shape --
// a TCB spline driven by a bone rotation, gated by a stage:"mult" corrective
// switch. Pre-fix, the spline was discarded entirely and the morph emitted only
// the gate value.
TEST(FormulaCompiler, CompilesMultiEntrySplineWithMultGate) {
    nlohmann::json formulas =
        loadFixture("multientry_spline_gate_body_cbs_forearm_y135n_l.json");
    ASSERT_TRUE(formulas.is_array());
    ASSERT_EQ(formulas.size(), 2u);

    std::vector<injector_core::CompiledFormula> set = injector_core::compileFormulaSet(formulas);
    ASSERT_EQ(set.size(), 2u);

    // Entry 0: the spline, implicit sum stage.
    EXPECT_EQ(set[0].stage, injector_core::FormulaStage::Sum);
    EXPECT_FALSE(set[0].stage_explicit);
    ASSERT_TRUE(std::holds_alternative<injector_core::SplineFormula>(set[0].body));
    const auto& spline = std::get<injector_core::SplineFormula>(set[0].body);
    EXPECT_EQ(spline.driving_operand,
              "l_forearm:/data/Daz%203D/Genesis%209/Base/Genesis9.dsf#l_forearm?rotation/y");
    EXPECT_TRUE(spline.tcb_interpolation);
    ASSERT_EQ(spline.keys.size(), 3u);
    EXPECT_DOUBLE_EQ(spline.keys[0].key, -135.0);
    EXPECT_DOUBLE_EQ(spline.keys[0].value, 1.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].key, -75.0);
    EXPECT_DOUBLE_EQ(spline.keys[1].value, 0.0);
    EXPECT_DOUBLE_EQ(spline.keys[2].key, 0.5);
    EXPECT_DOUBLE_EQ(spline.keys[2].value, 0.0);

    // Entry 1: the corrective-enable gate, stage "mult".
    EXPECT_EQ(set[1].stage, injector_core::FormulaStage::Product);
    EXPECT_TRUE(set[1].stage_explicit);
    ASSERT_TRUE(std::holds_alternative<injector_core::AlgebraFormula>(set[1].body));
    const auto& gate = std::get<injector_core::AlgebraFormula>(set[1].body);
    ASSERT_EQ(gate.ops.size(), 1u);
    ASSERT_TRUE(std::holds_alternative<injector_core::PushOperand>(gate.ops[0]));
    EXPECT_NE(std::get<injector_core::PushOperand>(gate.ops[0]).operand.find(
                  "body_basejointcorrectives?value"),
              std::string::npos);
}

// compileFormulaSet must preserve formulas_json's own array order: that order is
// the controller-chain order, so a Product entry ahead of the Sum entry it
// scales would change the arithmetic.
TEST(FormulaCompiler, FormulaSetPreservesArrayOrder) {
    nlohmann::json formulas = nlohmann::json::parse(R"([
        { "output": "x", "stage": "mult", "operations": [ { "op": "push", "url": "a" } ] },
        { "output": "x", "operations": [ { "op": "push", "url": "b" } ] },
        { "output": "x", "stage": "mult", "operations": [ { "op": "push", "url": "c" } ] }
    ])");

    std::vector<injector_core::CompiledFormula> set = injector_core::compileFormulaSet(formulas);
    ASSERT_EQ(set.size(), 3u);

    EXPECT_EQ(set[0].stage, injector_core::FormulaStage::Product);
    EXPECT_EQ(set[1].stage, injector_core::FormulaStage::Sum);
    EXPECT_EQ(set[2].stage, injector_core::FormulaStage::Product);

    const char* expected[] = {"a", "b", "c"};
    for (size_t i = 0; i < 3; ++i) {
        const auto& algebra = std::get<injector_core::AlgebraFormula>(set[i].body);
        ASSERT_EQ(algebra.ops.size(), 1u);
        EXPECT_EQ(std::get<injector_core::PushOperand>(algebra.ops[0]).operand, expected[i]);
    }
}

// An explicit "sum", and any unrecognized stage string, both mean Sum -- only
// "mult" selects Product. An unknown stage must not throw: falling back to Sum
// mis-scales one entry, whereas throwing would drop the morph's whole chain.
TEST(FormulaCompiler, OnlyMultSelectsProductStage) {
    nlohmann::json formulas = nlohmann::json::parse(R"([
        { "output": "x", "stage": "sum",      "operations": [ { "op": "push", "url": "a" } ] },
        { "output": "x", "stage": "multiply", "operations": [ { "op": "push", "url": "b" } ] },
        { "output": "x", "stage": "mult",     "operations": [ { "op": "push", "url": "c" } ] }
    ])");

    std::vector<injector_core::CompiledFormula> set = injector_core::compileFormulaSet(formulas);
    ASSERT_EQ(set.size(), 3u);

    EXPECT_EQ(set[0].stage, injector_core::FormulaStage::Sum);
    EXPECT_TRUE(set[0].stage_explicit);

    EXPECT_EQ(set[1].stage, injector_core::FormulaStage::Sum);  // "multiply" != "mult"
    EXPECT_TRUE(set[1].stage_explicit);

    EXPECT_EQ(set[2].stage, injector_core::FormulaStage::Product);
    EXPECT_TRUE(set[2].stage_explicit);
}

// Compilation is atomic across the array: one bad entry rejects the whole set
// rather than yielding a partial chain (a dropped "mult" gate would leave a
// corrective morph firing at full strength unconditionally).
TEST(FormulaCompiler, FormulaSetIsAtomicOnBadEntry) {
    nlohmann::json formulas = nlohmann::json::parse(R"([
        { "output": "x", "operations": [ { "op": "push", "url": "a" } ] },
        { "output": "x", "operations": [ { "op": "push", "val": 1 }, { "op": "clamp" } ] }
    ])");

    EXPECT_THROW(injector_core::compileFormulaSet(formulas), injector_core::FormulaCompileError);
}

TEST(FormulaCompiler, FormulaSetRejectsNonArray) {
    nlohmann::json formula = nlohmann::json::parse(R"({
        "output": "x", "operations": [ { "op": "push", "url": "a" } ]
    })");

    EXPECT_THROW(injector_core::compileFormulaSet(formula), injector_core::FormulaCompileError);
}

// A single-entry array still works, and still defaults to Sum.
TEST(FormulaCompiler, SingleEntrySetDefaultsToSumStage) {
    nlohmann::json formulas = nlohmann::json::array();
    formulas.push_back(loadFixture("algebra_pJCMCloakBend_m90.json"));

    std::vector<injector_core::CompiledFormula> set = injector_core::compileFormulaSet(formulas);
    ASSERT_EQ(set.size(), 1u);
    EXPECT_EQ(set[0].stage, injector_core::FormulaStage::Sum);
    EXPECT_FALSE(set[0].stage_explicit);
    EXPECT_TRUE(std::holds_alternative<injector_core::AlgebraFormula>(set[0].body));
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
