#include "FormulaControllerBuilder.h"

#include <variant>

#include "dzapp.h"
#include "dzerclink.h"
#include "dzformula.h"
#include "dznumericproperty.h"

namespace daz_plugin {

namespace {

void logMessage(const QString& message) {
    if (dzApp) {
        dzApp->log("[daz_plugin] FormulaControllerBuilder: " + message);
    }
}

void logUnresolvedOperand(const std::string& operand) {
    logMessage(QString("failed to resolve formula operand \"%1\" -- abandoning this "
                        "morph's whole formula chain")
                   .arg(QString::fromStdString(operand)));
}

// ---------------------------------------------------------------------------
// ERC shape matching
// ---------------------------------------------------------------------------
// An ERC link computes, per dzerclink.h's ERCType, a fixed transform of the
// running value using one driving property, one scalar and one addend. Only a
// few algebra shapes fit -- but per the header's corpus analysis those few
// shapes are 100% of the real production data, and unlike DzFormulaController
// their combination arithmetic is live-verified.
struct ErcShape {
    DzERCLink::ERCType type;
    std::string operand;
    double scalar = 1.0;
};

// Matches an AlgebraFormula against the ERC-expressible vocabulary:
//
//   Sum     + [ PushOperand ]                   -> ERCDeltaAdd, scalar  1
//   Sum     + [ PushOperand, PushConst k, Mult ]-> ERCDeltaAdd, scalar  k
//   Sum     + [ PushOperand, Neg ]              -> ERCDeltaAdd, scalar -1
//   Product + [ PushOperand ]                   -> ERCMultiply
//
// ERCDeltaAdd computes `val + prop*scalar + addend`, so a Sum entry that is a
// driving property times a constant is exactly a delta-add with that constant
// as its scalar -- which is precisely how Daz's own loader encodes the
// 98.73%-of-corpus `push:url push:val mult` shape (verified live: native
// Genesis 9 JCMs carry ERCDeltaAdd/ERCMultiply link pairs, never formulas).
//
// A Product entry scaled by a constant (`stage:"mult"` with a trailing
// `push:val mult`) is deliberately NOT matched here: ERCMultiply's scalar is
// not verified to participate the way ERCDeltaAdd's does, so such an entry is
// routed to the DzFormulaController fallback rather than guessed at. No such
// entry exists in the production index.
bool matchErcShape(const injector_core::AlgebraFormula& algebra,
                    injector_core::FormulaStage stage,
                    ErcShape& out) {
    using namespace injector_core;

    const auto& ops = algebra.ops;
    if (ops.empty()) {
        return false;
    }

    const auto* leadOperand = std::get_if<PushOperand>(&ops[0]);
    if (!leadOperand) {
        return false;
    }
    out.operand = leadOperand->operand;

    if (stage == FormulaStage::Product) {
        if (ops.size() != 1) {
            return false;
        }
        out.type = DzERCLink::ERCMultiply;
        out.scalar = 1.0;
        return true;
    }

    out.type = DzERCLink::ERCDeltaAdd;

    if (ops.size() == 1) {
        out.scalar = 1.0;
        return true;
    }

    if (ops.size() == 2) {
        const auto* neg = std::get_if<AlgebraOp>(&ops[1]);
        if (!neg || neg->kind != AlgebraOp::Neg) {
            return false;
        }
        out.scalar = -1.0;
        return true;
    }

    if (ops.size() == 3) {
        const auto* constant = std::get_if<PushConst>(&ops[1]);
        const auto* mult = std::get_if<AlgebraOp>(&ops[2]);
        if (!constant || !mult || mult->kind != AlgebraOp::Mult) {
            return false;
        }
        out.scalar = constant->value;
        return true;
    }

    return false;
}

// SplineFormula -> DzERCLink, keyed. This is byte-for-byte what Daz's native
// loader produces for a bone-rotation-driven JCM (verified live on Genesis 9:
// ERCKeyed link, TCB interpolation, driving property = the bone's rotation
// channel, one key per spline control point).
DzERCLink* buildSplineLink(const injector_core::SplineFormula& spline,
                            DzNumericProperty* drivingProperty) {
    DzERCLink* link = new DzERCLink();
    link->setType(DzERCLink::ERCKeyed);
    link->setKeyInterpolation(spline.tcb_interpolation ? DzERCLink::TCB_INTERP
                                                        : DzERCLink::LINEAR_INTERP);
    link->setProperty(drivingProperty);

    for (const auto& key : spline.keys) {
        if (key.has_tcb) {
            link->addKeyValue(key.key, key.value, key.t, key.c, key.b);
        } else {
            link->addKeyValue(key.key, key.value);
        }
    }
    return link;
}

// AlgebraFormula -> DzFormula, walked left-to-right (the IR's ops vector is
// already RPN in source order). Returns nullptr (having deleted the partial
// formula) if any operand fails to resolve.
DzFormula* buildDzFormula(const injector_core::AlgebraFormula& algebra,
                           const OperandResolver& resolveOperand) {
    DzFormula* formula = new DzFormula();

    for (const auto& op : algebra.ops) {
        if (const auto* constOp = std::get_if<injector_core::PushConst>(&op)) {
            formula->addOpPush(static_cast<float>(constOp->value));
        } else if (const auto* operandOp = std::get_if<injector_core::PushOperand>(&op)) {
            DzNumericProperty* resolved =
                resolveOperand ? resolveOperand(operandOp->operand) : nullptr;
            if (!resolved) {
                logUnresolvedOperand(operandOp->operand);
                delete formula;
                return nullptr;
            }
            formula->addOpPush(resolved);
        } else {
            const auto& algebraOp = std::get<injector_core::AlgebraOp>(op);
            switch (algebraOp.kind) {
                case injector_core::AlgebraOp::Mult:
                    formula->addOp(DzFormula::OpMultiply);
                    break;
                case injector_core::AlgebraOp::Neg:
                    formula->addOp(DzFormula::OpNegate);
                    break;
            }
        }
    }
    return formula;
}

}  // namespace

bool FormulaControllerBuilder::buildChain(
    const std::vector<injector_core::CompiledFormula>& formulas,
    const OperandResolver& resolveOperand,
    std::vector<DzNumericController*>& out) {
    using namespace injector_core;

    out.clear();

    // Lazily created, and shared by every entry that isn't ERC-expressible, so
    // those entries combine through addFormula()'s Stage rather than through
    // separate clobbering controllers. It occupies the chain slot of the FIRST
    // such entry.
    DzFormulaController* fallbackController = nullptr;

    // Set false the moment any entry cannot be built; the loop then stops and
    // everything built so far is destroyed, leaving the property untouched.
    bool ok = true;

    for (const auto& compiled : formulas) {
        if (!ok) {
            break;
        }
        if (const auto* spline = std::get_if<SplineFormula>(&compiled.body)) {
            if (compiled.stage == FormulaStage::Product) {
                // A keyed ERC link multiplies nothing, and DzFormula has no
                // spline opcode at all (dzformula.h's Operation enum), so a
                // Product-staged spline is not representable by either
                // mechanism. Refuse rather than silently mis-evaluate. No such
                // entry exists in the production morph index.
                logMessage("a spline formula carries stage \"mult\", which no SDK "
                            "controller can express -- abandoning this morph's formula chain");
                ok = false;
                break;
            }

            DzNumericProperty* driving =
                resolveOperand ? resolveOperand(spline->driving_operand) : nullptr;
            if (!driving) {
                logUnresolvedOperand(spline->driving_operand);
                ok = false;
                break;
            }
            out.push_back(buildSplineLink(*spline, driving));
            continue;
        }

        const auto& algebra = std::get<AlgebraFormula>(compiled.body);

        ErcShape shape;
        if (matchErcShape(algebra, compiled.stage, shape)) {
            DzNumericProperty* driving =
                resolveOperand ? resolveOperand(shape.operand) : nullptr;
            if (!driving) {
                logUnresolvedOperand(shape.operand);
                ok = false;
                break;
            }
            DzERCLink* link = new DzERCLink();
            link->setType(shape.type);
            link->setScalar(shape.scalar);
            link->setAddend(0.0);
            link->setProperty(driving);
            out.push_back(link);
            continue;
        }

        // Not ERC-expressible: fold into the shared DzFormulaController with
        // this entry's Stage. addFormula() is exactly the SDK's mechanism for
        // combining several formulas on one controller (dzformula.h).
        DzFormula* formula = buildDzFormula(algebra, resolveOperand);
        if (!formula) {
            ok = false;  // buildDzFormula already logged the operand
            break;
        }
        if (!fallbackController) {
            fallbackController = new DzFormulaController();
            out.push_back(fallbackController);
        }
        fallbackController->addFormula(formula,
                                        compiled.stage == FormulaStage::Product
                                            ? DzFormulaController::StageProduct
                                            : DzFormulaController::StageSum);
    }

    if (!ok) {
        // Nothing has touched targetProperty yet, so these controllers are
        // still unowned and safe to delete. Deleting a DzFormulaController
        // also destroys the DzFormulas it took ownership of via addFormula().
        for (DzNumericController* controller : out) {
            delete controller;
        }
        out.clear();
        return false;
    }

    return true;
}

bool FormulaControllerBuilder::attachFormulaSet(
    const std::vector<injector_core::CompiledFormula>& formulas,
    DzNumericProperty* targetProperty,
    const OperandResolver& resolveOperand) {
    if (!targetProperty) {
        return false;
    }
    if (formulas.empty()) {
        return true;
    }

    std::vector<DzNumericController*> chain;
    if (!buildChain(formulas, resolveOperand, chain) || chain.empty()) {
        return false;
    }

    // Insert in formulas_json array order. insertController()'s default
    // idx = -1 APPENDS (verified live), and Daz applies a property's
    // controllers front-to-back with each transforming the running value, so
    // array order == evaluation order.
    for (DzNumericController* controller : chain) {
        targetProperty->insertController(controller);
    }
    return true;
}

}  // namespace daz_plugin
