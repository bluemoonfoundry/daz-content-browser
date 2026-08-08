/**********************************************************************
    FormulaControllerBuilder -- translates injector_core::FormulaIR
    (AlgebraFormula / SplineFormula, produced by FormulaCompiler from a
    morph's formulas_json) into native Daz Studio SDK controller objects and
    attaches them to a target DzNumericProperty via
    DzNumericProperty::insertController() (dznumericproperty.h).

    Two mechanical cases (design doc
    docs/superpowers/specs/2026-08-07-morph-injector-core-design.md, S3.2/S4,
    confirmed against the real SDK headers dzformula.h, dzerclink.h,
    dznumericcontroller.h, dznumericproperty.h):

      - AlgebraFormula -> a DzFormula (dzformula.h) built op-by-op via
        addOp()/addOpPush(), added to a DzFormulaController
        (also dzformula.h) via addFormula(). The controller -- not the
        DzFormula itself -- is what gets insertController()'d, since
        DzFormula derives from DzBase, not DzNumericController.

      - SplineFormula -> a DzERCLink (dzerclink.h) with
        ERCType::ERCKeyed and ERCKeyInterpolation::TCB_INTERP /
        LINEAR_INTERP, populated via addKeyValue() and setProperty().
        DzERCLink itself derives from DzNumericController, so it is
        inserted directly.

    Operand resolution (turning a FormulaIR operand string into a live
    DzNumericProperty*) is not this component's job -- that's
    PropertySourceAdapter (Subsystem B Task 6, built in parallel). This
    header takes operand resolution as an injected dependency
    (OperandResolver) so it has no compile-time or link-time dependency on
    PropertySourceAdapter's concrete implementation; Task 8's InjectorCore
    wires in the real resolver when it composes these pieces together.
**********************************************************************/
#pragma once

#include <functional>
#include <string>
#include <variant>

#include "FormulaIR.h"

class DzNumericProperty;

namespace daz_plugin {

// Resolves a FormulaIR operand string (a PushOperand::operand or a
// SplineFormula::driving_operand, both opaque strings from formulas_json as
// far as this component is concerned) to a live DzNumericProperty* in the
// current scene. Returns nullptr if the operand cannot be resolved --
// callers should treat that as a per-morph injection failure (see design
// doc S7: log via dzApp->log() and skip, never crash the host process).
//
// Task 7 (this component) only calls this synchronously and does not cache,
// retry, or interpret failures itself -- that policy belongs to the
// resolver's real implementation (PropertySourceAdapter, Task 6) and to
// InjectorCore's orchestration (Task 8).
using OperandResolver = std::function<DzNumericProperty*(const std::string& operand)>;

// Builds and attaches the native SDK controller(s) for one compiled formula.
//
// - ir: the FormulaCompiler output for a single formulas_json entry --
//   either an injector_core::AlgebraFormula or injector_core::SplineFormula.
// - targetProperty: the slave/output property the controller drives (e.g.
//   the morph's own DzFloatProperty from DzMorph::getValueChannel(),
//   dzmorph.h). Must be non-null.
// - resolveOperand: resolves every operand string the IR references (each
//   PushOperand::operand for AlgebraFormula; driving_operand for
//   SplineFormula) to a live DzNumericProperty*.
//
// Returns true if the controller was built and attached; false if any
// operand failed to resolve (nullptr from resolveOperand) or targetProperty
// is null, in which case nothing is attached and no partial controller is
// left dangling on the property. Never throws.
class FormulaControllerBuilder {
public:
    bool attachFormula(const std::variant<injector_core::AlgebraFormula, injector_core::SplineFormula>& ir,
                        DzNumericProperty* targetProperty,
                        const OperandResolver& resolveOperand);

private:
    bool attachAlgebraFormula(const injector_core::AlgebraFormula& formula,
                               DzNumericProperty* targetProperty,
                               const OperandResolver& resolveOperand);

    bool attachSplineFormula(const injector_core::SplineFormula& formula,
                              DzNumericProperty* targetProperty,
                              const OperandResolver& resolveOperand);
};

}  // namespace daz_plugin
