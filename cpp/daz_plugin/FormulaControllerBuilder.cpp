#include "FormulaControllerBuilder.h"

#include "dzapp.h"
#include "dzerclink.h"
#include "dzformula.h"
#include "dznumericproperty.h"

namespace daz_plugin {

namespace {

// Logs and returns nullptr's-worth-of-failure for an operand that didn't
// resolve. Per design doc S7, a per-morph/per-formula injection failure
// logs and is skipped rather than crashing the host process -- the caller
// (attachAlgebraFormula/attachSplineFormula) is responsible for aborting
// just this formula's attach, not the whole plugin.
void logUnresolvedOperand(const std::string& operand) {
    if (dzApp) {
        dzApp->log(QString("[daz_plugin] FormulaControllerBuilder: failed to resolve "
                            "formula operand \"%1\" -- skipping this formula")
                        .arg(QString::fromStdString(operand)));
    }
}

}  // namespace

bool FormulaControllerBuilder::attachFormula(
    const std::variant<injector_core::AlgebraFormula, injector_core::SplineFormula>& ir,
    DzNumericProperty* targetProperty,
    const OperandResolver& resolveOperand) {
    if (!targetProperty) {
        return false;
    }

    if (const auto* algebra = std::get_if<injector_core::AlgebraFormula>(&ir)) {
        return attachAlgebraFormula(*algebra, targetProperty, resolveOperand);
    }
    const auto& spline = std::get<injector_core::SplineFormula>(ir);
    return attachSplineFormula(spline, targetProperty, resolveOperand);
}

// AlgebraFormula -> DzFormula (dzformula.h), walked left-to-right (the IR's
// ops vector is already RPN in source order):
//   - PushConst{value}   -> DzFormula::addOpPush(float)
//   - PushOperand{operand} -> resolveOperand(operand), then
//                             DzFormula::addOpPush(DzNumericProperty*)
//   - AlgebraOp{Mult}    -> DzFormula::addOp(DzFormula::OpMultiply)
//   - AlgebraOp{Neg}     -> DzFormula::addOp(DzFormula::OpNegate)
// The built DzFormula is added to a fresh DzFormulaController via
// addFormula() (default Stage::StageSum -- FormulaCompiler's IR has no
// concept of stages, and StageSum is the natural default for a single
// formula owning its own controller). The *controller* -- not the
// DzFormula, which is a plain DzBase, not a DzNumericController -- is what
// gets attached via DzNumericProperty::insertController().
//
// On an unresolved operand, the partially-built DzFormula is deleted (it
// was never handed to a DzFormulaController yet, so this is a plain
// non-owning `delete`) and nothing is attached to targetProperty. This
// matches the ticket's stated posture (log + skip, not crash).
bool FormulaControllerBuilder::attachAlgebraFormula(const injector_core::AlgebraFormula& formula,
                                                      DzNumericProperty* targetProperty,
                                                      const OperandResolver& resolveOperand) {
    DzFormula* dzFormula = new DzFormula();

    for (const auto& op : formula.ops) {
        if (const auto* constOp = std::get_if<injector_core::PushConst>(&op)) {
            dzFormula->addOpPush(static_cast<float>(constOp->value));
        } else if (const auto* operandOp = std::get_if<injector_core::PushOperand>(&op)) {
            DzNumericProperty* resolved = resolveOperand ? resolveOperand(operandOp->operand) : nullptr;
            if (!resolved) {
                logUnresolvedOperand(operandOp->operand);
                delete dzFormula;
                return false;
            }
            dzFormula->addOpPush(resolved);
        } else {
            const auto& algebraOp = std::get<injector_core::AlgebraOp>(op);
            switch (algebraOp.kind) {
                case injector_core::AlgebraOp::Mult:
                    dzFormula->addOp(DzFormula::OpMultiply);
                    break;
                case injector_core::AlgebraOp::Neg:
                    dzFormula->addOp(DzFormula::OpNegate);
                    break;
            }
        }
    }

    DzFormulaController* controller = new DzFormulaController();
    controller->addFormula(dzFormula, DzFormulaController::StageSum);
    targetProperty->insertController(controller);
    return true;
}

// SplineFormula -> DzERCLink (dzerclink.h), keyed:
//   - ERCType::ERCKeyed (setType)
//   - ERCKeyInterpolation::TCB_INTERP / LINEAR_INTERP (setKeyInterpolation),
//     chosen from tcb_interpolation
//   - per SplineKey: addKeyValue(key, value, t, c, b) (5-arg TCB overload)
//     when has_tcb, else addKeyValue(key, value) (2-arg linear overload)
//   - setProperty() binds the link's driving/control property, resolved
//     from driving_operand via the same resolver used for algebra operands
// DzERCLink derives directly from DzNumericController (unlike DzFormula, no
// separate controller wrapper is needed), so the link itself is what's
// passed to DzNumericProperty::insertController() on the target/slave
// property.
bool FormulaControllerBuilder::attachSplineFormula(const injector_core::SplineFormula& formula,
                                                     DzNumericProperty* targetProperty,
                                                     const OperandResolver& resolveOperand) {
    DzNumericProperty* drivingProperty =
        resolveOperand ? resolveOperand(formula.driving_operand) : nullptr;
    if (!drivingProperty) {
        logUnresolvedOperand(formula.driving_operand);
        return false;
    }

    DzERCLink* link = new DzERCLink();
    link->setType(DzERCLink::ERCKeyed);
    link->setKeyInterpolation(formula.tcb_interpolation ? DzERCLink::TCB_INTERP
                                                          : DzERCLink::LINEAR_INTERP);
    link->setProperty(drivingProperty);

    for (const auto& key : formula.keys) {
        if (key.has_tcb) {
            link->addKeyValue(key.key, key.value, key.t, key.c, key.b);
        } else {
            link->addKeyValue(key.key, key.value);
        }
    }

    targetProperty->insertController(link);
    return true;
}

}  // namespace daz_plugin
