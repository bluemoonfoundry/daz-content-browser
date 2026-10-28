/**********************************************************************
    FormulaControllerBuilder -- translates a morph's compiled formula SET
    (injector_core::CompiledFormula list, produced by
    FormulaCompiler::compileFormulaSet from the morph's whole formulas_json
    array) into native Daz Studio SDK controllers, and chains them onto the
    target DzNumericProperty via DzNumericProperty::insertController()
    (dznumericproperty.h).

    ---------------------------------------------------------------------
    WHY A *SET* AND NOT ONE FORMULA AT A TIME  (beads-w56)
    ---------------------------------------------------------------------
    formulas_json is an ARRAY, and multiple entries routinely drive the SAME
    output property, combined by each entry's "stage" ("mult" = Product,
    absent = implicit Sum). The previous API took one entry at a time and
    built a fresh, independent controller for each at a hardcoded StageSum.
    Entries therefore never combined; live testing in Daz Studio showed the
    output tracking only the last entry. 59% of the 23,596 formula-bearing
    morphs in the production morph_index.db are multi-entry, and the
    spline + "mult" gate pattern is the *typical* shape of a real
    bone-driven JCM -- so essentially every spline JCM was emitting only its
    corrective-enable gate and discarding its deformation entirely.

    Attachment is therefore SET-AT-A-TIME and ORDERED.

    ---------------------------------------------------------------------
    CONTROLLER CHOICE: DzERCLink FIRST, DzFormulaController AS FALLBACK
    ---------------------------------------------------------------------
    Determined by live introspection of a running Daz Studio 4.5 instance
    (daz-script-server /execute) rather than from headers alone:

      * Daz's own DSON loader represents ALL of these formulas as DzERCLink.
        Scanning every controlled morph on a loaded Genesis 9 figure found
        ~4,900 morphs carrying DzERCLinks and ZERO DzFormulaControllers.
        The native encoding of the spline + gate JCM pattern is exactly two
        chained links:
            ctrl[0] DzERCLink ERCKeyed    <- bone rotation, spline keys
            ctrl[1] DzERCLink ERCMultiply <- body_basejointcorrectives
      * DzFormulaController cannot be constructed from DzScript, so its
        Stage-combination arithmetic (in particular whether a Product-only
        controller scales or replaces the incoming chain value) is NOT
        verifiable and is undocumented in the SDK headers.
      * DzERCLink chain arithmetic IS verifiable, and was verified live:
            ERCKeyed(spline) then ERCMultiply(gate)  ->  spline(drv) * gate
            ERCDeltaAdd(scalar=k) then ERCMultiply   ->  (drv * k) * gate
      * Every one of the 968,725 formula entries in the production
        morph_index.db falls into exactly five shapes, ALL of which are
        exactly representable as a DzERCLink:
            98.73%  sum,  push:url push:val mult  -> ERCDeltaAdd scalar=val
             1.07%  mult, push:url                -> ERCMultiply
             0.12%  sum,  push:url                -> ERCDeltaAdd scalar=1
             0.08%  sum,  SPLINE                  -> ERCKeyed
             0.003% sum,  push:url neg            -> ERCDeltaAdd scalar=-1

    So the builder maps each entry to a DzERCLink whenever its shape allows,
    which reproduces Daz's own native representation bit-for-bit and has
    verified arithmetic. Shapes outside that vocabulary (none occur in the
    production index, but arbitrary RPN is expressible in the IR) fall back
    to a SINGLE shared DzFormulaController carrying one DzFormula per such
    entry, added with the entry's Stage via addFormula(f, StageSum/
    StageProduct) -- the mechanism dzformula.h is designed for, and the one
    beads-w56 originally prescribed.

    ---------------------------------------------------------------------
    CHAIN ORDER
    ---------------------------------------------------------------------
    Controllers are inserted in formulas_json array order using
    insertController(c) with the default idx = -1, which APPENDS (verified
    live: inserting L1 then L2 leaves getController(0)==L1, (1)==L2). Daz
    evaluates a property's controller list front-to-back, each controller's
    apply(val, ...) transforming the running value produced by its
    predecessor -- so array order == chain order == evaluation order, and a
    Product entry correctly scales the Sum entries that precede it. This
    matches the native layout observed on Genesis 9's own JCMs.

    ---------------------------------------------------------------------
    ATOMICITY
    ---------------------------------------------------------------------
    Attachment is all-or-nothing. Every controller is built and every
    operand resolved BEFORE anything is inserted; if any operand fails to
    resolve, the already-built controllers are deleted and the property is
    left untouched. A half-attached chain is worse than an unattached one --
    dropping a "mult" gate would leave a corrective morph firing at full
    strength unconditionally.

    Operand resolution (turning a FormulaIR operand string into a live
    DzNumericProperty*) is not this component's job -- that's
    PropertySourceAdapter. It is taken as an injected dependency
    (OperandResolver) so this component has no compile-time or link-time
    dependency on that concrete implementation.
**********************************************************************/
#pragma once

#include <functional>
#include <string>
#include <vector>

#include "FormulaIR.h"

class DzNumericController;
class DzNumericProperty;

namespace daz_plugin {

// Resolves a FormulaIR operand string (a PushOperand::operand or a
// SplineFormula::driving_operand, both opaque strings from formulas_json as
// far as this component is concerned) to a live DzNumericProperty* in the
// current scene. Returns nullptr if the operand cannot be resolved --
// callers should treat that as a per-morph injection failure (see design
// doc S7: log via dzApp->log() and skip, never crash the host process).
using OperandResolver = std::function<DzNumericProperty*(const std::string& operand)>;

class FormulaControllerBuilder {
public:
    // Builds and chains the native SDK controllers for ONE morph's entire
    // formulas_json array onto its output property.
    //
    // - formulas: compileFormulaSet() output, in formulas_json array order.
    //   An empty set is a no-op that returns true.
    // - targetProperty: the slave/output property the chain drives (the
    //   morph's own DzFloatProperty from DzMorph::getValueChannel()).
    // - resolveOperand: resolves every operand string the set references.
    //
    // Returns true if the whole chain was attached; false if targetProperty
    // is null, if any operand failed to resolve, or if an entry's shape is
    // not attachable at all -- in which case NOTHING is attached and no
    // partial chain is left on the property. Never throws.
    bool attachFormulaSet(const std::vector<injector_core::CompiledFormula>& formulas,
                           DzNumericProperty* targetProperty,
                           const OperandResolver& resolveOperand);

private:
    // Builds the controller chain without touching targetProperty. On failure
    // returns false having deleted everything it built; `out` is then empty.
    bool buildChain(const std::vector<injector_core::CompiledFormula>& formulas,
                     const OperandResolver& resolveOperand,
                     std::vector<DzNumericController*>& out);
};

}  // namespace daz_plugin
