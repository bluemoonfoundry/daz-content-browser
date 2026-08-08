/**********************************************************************
    PropertySourceAdapter -- resolves a compiled-formula operand string into a
    live DzNumericProperty* in the running Daz Studio scene.

    Design: docs/superpowers/specs/2026-08-07-morph-injector-core-design.md
    sections 2 (operand string forms), 3.2 (this component), 7 (threading).

    THREADING: every method here touches live scene-graph objects and must be
    called on the main GUI thread only (design section 7). Nothing in this class
    mutates the scene -- it only reads it -- but the objects it walks are only
    safe to touch from the GUI thread regardless.
**********************************************************************/

#pragma once

#include <cstdint>
#include <string>

#include <QtCore/QString>

class DzNode;
class DzNumericProperty;

namespace injector_core {
class MorphIndexReader;
}

namespace daz_plugin {

/**
    Callback interface into InjectorCore (Subsystem B Task 8, beads-2mw.8).

    A path-form / name-form operand can name another *indexed but not yet
    injected* morph. Its value channel does not exist in the scene until that
    morph has been injected, so PropertySourceAdapter cannot resolve it alone --
    it has to ask the injector to materialise the morph first. That is a genuine
    cycle (InjectorCore -> FormulaControllerBuilder -> PropertySourceAdapter ->
    InjectorCore), broken here by an abstract interface so this file has no
    compile-time dependency on InjectorCore.

    Task 8 implements this on InjectorCore, where the implementation is:
      injectMorph(morph_id) (idempotent / re-entrancy guarded, so a diamond or
      cyclic dependency graph terminates) -> DzMorph::getValueChannel().

    Contract for the implementer:
      - Returns the live value channel of the named morph, injecting it first if
        needed. Returns nullptr (never throws) if the morph cannot be injected
        (missing .tmb, wrong figure, recursion guard trip, ...).
      - Must be re-entrancy safe: it is called from *inside* an injection.
**/
class MorphInjectionEnsurer
{
public:
    virtual ~MorphInjectionEnsurer() {}

    virtual DzNumericProperty* ensureInjectedAndGetValueChannel( int64_t morphId ) = 0;
};

/**
    The parsed pieces of a DSON formula operand URL.

    Real operand strings (formulas_json `{"op":"push","url":...}` values) take
    one of two shapes -- see design section 2:

      path-form : "Cloak:/data/.../GnHdCloak_G3_23369.dsf#Cloak?rotation/x"
      pathless  : "Genesis8Female:#pJCMCloakBend_m90?value"

    Grammar: [<label>':'] [<path>] '#' <element> ['?' <property>]

    `label` is the DSON asset/scene-id prefix and is intentionally ignored for
    resolution (Subsystem A's extract_referenced_guids() drops it too).
**/
struct OperandRef
{
    QString label;     //!< before the first ':' (may be empty)
    QString path;      //!< between ':' and '#' -- empty for the pathless form
    QString element;   //!< between '#' and '?' -- a node name or a property name
    QString property;  //!< after '?' -- e.g. "value", "rotation/x" (may be empty)

    //! True when this operand carries a content path (the "path-form").
    bool hasPath() const { return !path.isEmpty(); }
};

/**
    Where an operand is being resolved *from*: the morph currently being
    injected. Needed because pathless operands are relative and figure-scoped.

      node         -- the DzNode the morph being compiled is attached to. A
                      pathless operand with no resolvable node name resolves
                      against this node; it is also the root of the bone search.
      targetFigure -- the `target_figure` column of the morph being compiled.
                      Pathless morph references are figure-local (pJCM* names
                      repeat across base figures), so the morph-index lookup
                      must be scoped exactly the way Subsystem A's
                      rebuild_dependencies() scopes it:
                      MorphIndexReader::find_by_name(target_figure, name).
**/
struct ResolutionContext
{
    DzNode* node;
    QString targetFigure;

    ResolutionContext() : node( 0 ) {}
    ResolutionContext( DzNode* n, const QString& figure ) : node( n ), targetFigure( figure ) {}
};

/**
    Resolves operand strings (FormulaIR's PushOperand::operand and
    SplineFormula::driving_operand) to live scene properties.

    Holds non-owning references to the morph index and the injection ensurer;
    both must outlive the adapter.
**/
class PropertySourceAdapter
{
public:
    PropertySourceAdapter( const injector_core::MorphIndexReader& index,
                           MorphInjectionEnsurer& ensurer );

    /**
        Resolves `operand` to a live property, or returns nullptr and sets
        lastError(). Never throws; a failed operand is a skip-this-formula
        condition, not a fatal one (design section 7).
    **/
    DzNumericProperty* resolve( const std::string& operand, const ResolutionContext& ctx );
    DzNumericProperty* resolve( const QString& operand, const ResolutionContext& ctx );

    //! Human-readable reason the last resolve() returned nullptr. Empty on success.
    const QString& lastError() const { return m_lastError; }

    /**
        Splits an operand URL into its pieces. Static and side-effect free so it
        is exercisable without a scene. Returns false if `operand` has no '#'
        separator (the one shape Subsystem A also refuses to interpret).
    **/
    static bool parseOperand( const QString& operand, OperandRef& out );

    /**
        True when `property` names a channel that lives on a DzNode itself
        (a transform / origin / orientation channel) rather than on a modifier.
        Exposed for testing and for callers that want to pre-classify operands.
    **/
    static bool isNodeChannel( const QString& property );

private:
    //! Transform-channel path ("rotation/x", "general_scale", ...) -> node accessor.
    DzNumericProperty* resolveNodeChannel( DzNode* node, const QString& property );

    //! Finds the DzNode an operand's `element` names, relative to ctx.
    DzNode* resolveNode( const QString& elementName, const ResolutionContext& ctx );

    //! Morph-index lookup + ensureInjected for an operand that names a morph.
    DzNumericProperty* resolveIndexedMorph( const OperandRef& ref, const ResolutionContext& ctx );

    //! Last-resort: a named property living on `node` or one of its modifiers.
    DzNumericProperty* resolveSceneProperty( DzNode* node, const QString& propertyName );

    const injector_core::MorphIndexReader& m_index;
    MorphInjectionEnsurer& m_ensurer;
    QString m_lastError;

    // Non-copyable: holds references.
    PropertySourceAdapter( const PropertySourceAdapter& );
    PropertySourceAdapter& operator=( const PropertySourceAdapter& );
};

}  // namespace daz_plugin
