#include "PropertySourceAdapter.h"

#include <QtCore/QStringList>
#include <QtCore/QUrl>

#include "dzbone.h"
#include "dzfloatproperty.h"
#include "dzmodifier.h"
#include "dznode.h"
#include "dznumericproperty.h"
#include "dzobject.h"
#include "dzproperty.h"
#include "dzpropertyhelper.h"
#include "dzscene.h"
#include "dzskeleton.h"

#include "MorphIndexReader.h"

namespace daz_plugin {

namespace {

//! Percent-decodes a DSON URL component ("/data/DAZ%203D/..." -> "/data/DAZ 3D/...").
QString percentDecoded( const QString& s )
{
    return QUrl::fromPercentEncoding( s.toUtf8() );
}

//! Case-insensitive compare against a DSON channel-path literal.
bool channelIs( const QString& property, const char* literal )
{
    return property.compare( QString::fromLatin1( literal ), Qt::CaseInsensitive ) == 0;
}

}  // namespace

PropertySourceAdapter::PropertySourceAdapter( const injector_core::MorphIndexReader& index,
                                              MorphInjectionEnsurer& ensurer )
    : m_index( index ), m_ensurer( ensurer )
{
}

/*
    Grammar: [<label>':'] [<path>] '#' <element> ['?' <property>]

    The label is split on the *first* ':' only, and only when that ':' comes
    before the '#'. Paths in real content never contain ':' (they are
    Daz-content-relative, "/data/..."), so this is unambiguous; guarding on the
    '#' position additionally protects operands that carry no label at all.
    This mirrors src/dsf_parser.py's extract_referenced_guids() so that what this
    adapter looks up is byte-identical to what Subsystem A indexed.
*/
bool PropertySourceAdapter::parseOperand( const QString& operand, OperandRef& out )
{
    out = OperandRef();

    const int hashIdx = operand.indexOf( QLatin1Char( '#' ) );
    if ( hashIdx < 0 )
    {
        return false;
    }

    QString head = operand.left( hashIdx );
    const QString tail = operand.mid( hashIdx + 1 );

    const int colonIdx = head.indexOf( QLatin1Char( ':' ) );
    if ( colonIdx >= 0 )
    {
        out.label = head.left( colonIdx );
        head = head.mid( colonIdx + 1 );
    }
    out.path = head;

    const int qIdx = tail.indexOf( QLatin1Char( '?' ) );
    if ( qIdx >= 0 )
    {
        out.element = tail.left( qIdx );
        out.property = tail.mid( qIdx + 1 );
    }
    else
    {
        out.element = tail;
    }

    return true;
}

/*
    DSON channel-path vocabulary for channels that live on a DzNode itself.
    Everything else (most importantly "value", the modifier/morph channel) names
    a property that lives on a modifier or is otherwise found by name.
*/
bool PropertySourceAdapter::isNodeChannel( const QString& property )
{
    if ( property.isEmpty() )
    {
        return false;
    }

    const QString group = property.section( QLatin1Char( '/' ), 0, 0 );
    return channelIs( group, "rotation" )
        || channelIs( group, "translation" )
        || channelIs( group, "scale" )
        || channelIs( group, "general_scale" )
        || channelIs( group, "orientation" )
        || channelIs( group, "center_point" )
        || channelIs( group, "end_point" );
}

/*
    DSON channel path -> SDK accessor.

    Deliberately uses DzNode's typed per-channel accessors (dznode.h lines
    234-259) rather than DzElement::findProperty() with a guessed internal name
    ("XRotate" etc.). The accessors are a documented, compile-checked part of the
    SDK surface; the internal names are not, and they are also localisation- and
    version-sensitive. Design section 3.2 explicitly left this choice open
    ("whether rotation/x maps directly to a findProperty lookup or needs per-axis
    accessor methods") -- resolved here in favour of the accessors.
*/
DzNumericProperty* PropertySourceAdapter::resolveNodeChannel( DzNode* node, const QString& property )
{
    if ( !node )
    {
        return 0;
    }

    const QString group = property.section( QLatin1Char( '/' ), 0, 0 );
    const QString axis = property.section( QLatin1Char( '/' ), 1, 1 ).toLower();

    const bool x = ( axis == QLatin1String( "x" ) );
    const bool y = ( axis == QLatin1String( "y" ) );
    const bool z = ( axis == QLatin1String( "z" ) );

    if ( channelIs( group, "rotation" ) )
    {
        if ( x ) return node->getXRotControl();
        if ( y ) return node->getYRotControl();
        if ( z ) return node->getZRotControl();
    }
    else if ( channelIs( group, "translation" ) )
    {
        if ( x ) return node->getXPosControl();
        if ( y ) return node->getYPosControl();
        if ( z ) return node->getZPosControl();
    }
    else if ( channelIs( group, "scale" ) )
    {
        if ( x ) return node->getXScaleControl();
        if ( y ) return node->getYScaleControl();
        if ( z ) return node->getZScaleControl();
        // A bare "scale" with no axis is the uniform/general scale channel.
        if ( axis.isEmpty() ) return node->getScaleControl();
    }
    else if ( channelIs( group, "general_scale" ) )
    {
        return node->getScaleControl();
    }
    else if ( channelIs( group, "orientation" ) )
    {
        if ( x ) return node->getOrientXControl();
        if ( y ) return node->getOrientYControl();
        if ( z ) return node->getOrientZControl();
    }
    else if ( channelIs( group, "center_point" ) )
    {
        // DSON's center_point is the node origin (dznode.h getOrigin*/getOriginXControl).
        if ( x ) return node->getOriginXControl();
        if ( y ) return node->getOriginYControl();
        if ( z ) return node->getOriginZControl();
    }
    else if ( channelIs( group, "end_point" ) )
    {
        if ( x ) return node->getEndXControl();
        if ( y ) return node->getEndYControl();
        if ( z ) return node->getEndZControl();
    }

    return 0;
}

/*
    Resolves `label` (the DSON operand label prefix, e.g. "Genesis9/l_eye") to
    the figure root it names, then searches specifically within THAT figure's
    own hierarchy for `elementName` -- never ctx.node's, even if ctx.node
    happens to have a same-named node of its own (beads-caw: Genesis 9's
    companion figures -- Genesis9Eyes, Genesis9Eyelashes, ... -- each duplicate
    the base figure's bone names for rigging/skinning, so an unscoped search
    can silently resolve to the wrong figure's copy).

    The label's own grammar is not fully specified by the design doc beyond
    "the DSON asset/scene-id prefix"; observed real content uses both a bare
    figure id ("Cloak") and a compound "FigureId/NodeId" form (as in the
    "Genesis9/l_eye" example above). Trying the label whole first, then its
    last '/'-segment, covers both without needing to fully parse DSON's label
    grammar.
*/
DzNode* PropertySourceAdapter::resolveNodeViaLabel( const QString& elementName, const QString& label )
{
    if ( label.isEmpty() || !dzScene )
    {
        return 0;
    }

    QStringList candidates;
    candidates << label;
    const int slashIdx = label.lastIndexOf( QLatin1Char( '/' ) );
    if ( slashIdx >= 0 )
    {
        candidates << label.left( slashIdx );
    }

    for ( int i = 0; i < candidates.size(); ++i )
    {
        const QString& figureId = candidates[i];
        DzNode* figureRoot = dzScene->findNode( figureId );
        if ( !figureRoot )
        {
            figureRoot = dzScene->findNodeByLabel( figureId );
        }
        if ( !figureRoot )
        {
            continue;
        }

        if ( figureRoot->getName() == elementName )
        {
            return figureRoot;
        }

        DzSkeleton* skeleton = figureRoot->getSkeleton();
        if ( skeleton )
        {
            DzBone* bone = skeleton->findBone( elementName );
            if ( bone )
            {
                return bone;
            }
        }

        DzNode* child = figureRoot->findNodeChild( elementName, true );
        if ( child )
        {
            return child;
        }
    }

    return 0;
}

/*
    Which node an operand's `element` names.

    Search order, most specific first:
      1. empty element      -> the context node itself (a self-relative operand)
      2. `label`-scoped      -> the specific figure `label` names, searched via
                                resolveNodeViaLabel() (beads-caw). Tried before
                                ctx.node's own hierarchy because the whole point
                                of a label is to disambiguate from ctx.node's
                                figure -- an operand carrying a label that names
                                a DIFFERENT figure than ctx.node must not resolve
                                against ctx.node's own same-named node.
      3. the context node's own name
      4. a bone of the context node's skeleton (DzSkeleton::findBone)
      5. any descendant of the context node (findNodeChild, scanHierarchy=true)
      6. a scene-global node lookup (DzScene::findNode / findNodeByLabel)
*/
DzNode* PropertySourceAdapter::resolveNode( const QString& elementName, const QString& label,
                                            const ResolutionContext& ctx )
{
    if ( elementName.isEmpty() )
    {
        return ctx.node;
    }

    DzNode* viaLabel = resolveNodeViaLabel( elementName, label );
    if ( viaLabel )
    {
        return viaLabel;
    }

    if ( ctx.node )
    {
        if ( ctx.node->getName() == elementName )
        {
            return ctx.node;
        }

        DzSkeleton* skeleton = ctx.node->getSkeleton();
        if ( skeleton )
        {
            DzBone* bone = skeleton->findBone( elementName );
            if ( bone )
            {
                return bone;
            }
        }

        DzNode* child = ctx.node->findNodeChild( elementName, true );
        if ( child )
        {
            return child;
        }
    }

    if ( dzScene )
    {
        DzNode* node = dzScene->findNode( elementName );
        if ( !node )
        {
            node = dzScene->findNodeByLabel( elementName );
        }
        if ( node )
        {
            return node;
        }
    }

    return 0;
}

/*
    Morph-index lookup, matching Subsystem A's own resolution rules exactly
    (src/managers/morph_index_manager.py rebuild_dependencies):
      - path-form -> matched against morphs.guid, which for a DSON modifier is
        asset_info.id, i.e. the content path itself (src/dsf_parser.py line 75).
        Compared raw first, because Subsystem A never percent-decodes either
        side; a decoded retry is a tolerance for content whose asset_info.id and
        formula url disagree about encoding.
      - pathless -> matched by (target_figure, name), figure-scoped.
*/
DzNumericProperty* PropertySourceAdapter::resolveIndexedMorph( const OperandRef& ref,
                                                               const ResolutionContext& ctx )
{
    std::optional<injector_core::MorphRecord> record;

    if ( ref.hasPath() )
    {
        record = m_index.find_by_guid( std::string( ref.path.toUtf8().constData() ) );
        if ( !record )
        {
            const QString decoded = percentDecoded( ref.path );
            if ( decoded != ref.path )
            {
                record = m_index.find_by_guid( std::string( decoded.toUtf8().constData() ) );
            }
        }
    }
    else if ( !ctx.targetFigure.isEmpty() )
    {
        record = m_index.find_by_name( std::string( ctx.targetFigure.toUtf8().constData() ),
                                       std::string( ref.element.toUtf8().constData() ) );
    }

    if ( !record )
    {
        return 0;
    }

    return m_ensurer.ensureInjectedAndGetValueChannel( record->morph_id );
}

/*
    A property that already exists in the scene under `propertyName`.

    DzPropertyHelper::findPropertyOnNode (dzpropertyhelper.h) is used rather than
    DzElement::findProperty because a morph's value channel lives on a DzModifier
    owned by the node's DzObject, not on the DzNode element itself -- findProperty
    alone would miss every morph already present in the scene. The DzElement-level
    lookups are kept as fallbacks for channels that do live on the node element,
    and a direct DzObject::findModifier() pass covers modifiers whose value
    channel the helper does not surface.
*/
DzNumericProperty* PropertySourceAdapter::resolveSceneProperty( DzNode* node, const QString& propertyName )
{
    if ( !node || propertyName.isEmpty() )
    {
        return 0;
    }

    QString name( propertyName );

    DzProperty* prop = DzPropertyHelper::findPropertyOnNode( name, node );
    if ( !prop )
    {
        prop = DzPropertyHelper::findPropertyOnNodeByInternalName( name, node );
    }
    if ( !prop )
    {
        prop = node->findProperty( name );
    }
    if ( !prop )
    {
        prop = node->findPropertyByLabel( name );
    }

    if ( !prop )
    {
        DzObject* object = node->getObject();
        if ( object )
        {
            DzModifier* modifier = object->findModifier( name );
            if ( modifier )
            {
                prop = modifier->findProperty( QString::fromLatin1( "Value" ) );
            }
        }
    }

    return qobject_cast<DzNumericProperty*>( prop );
}

DzNumericProperty* PropertySourceAdapter::resolve( const std::string& operand, const ResolutionContext& ctx )
{
    return resolve( QString::fromUtf8( operand.c_str(), static_cast<int>( operand.size() ) ), ctx );
}

DzNumericProperty* PropertySourceAdapter::resolve( const QString& operand, const ResolutionContext& ctx )
{
    m_lastError.clear();

    OperandRef ref;
    if ( !parseOperand( operand, ref ) )
    {
        m_lastError = QString( "operand '%1' has no '#' separator; not a resolvable property URL" )
                          .arg( operand );
        return 0;
    }

    // 1. Node transform channels (bone rotations and friends). These never live
    //    in morph_index.db, so they are classified by channel name first --
    //    including for path-form operands, whose fragment names a node, e.g.
    //    "Cloak:/data/.../GnHdCloak_G3_23369.dsf#Cloak?rotation/x" (design section 2).
    if ( isNodeChannel( ref.property ) )
    {
        DzNode* node = resolveNode( ref.element, ref.label, ctx );
        if ( !node )
        {
            m_lastError = QString( "operand '%1': no scene node named '%2'" )
                              .arg( operand ).arg( ref.element );
            return 0;
        }

        DzNumericProperty* prop = resolveNodeChannel( node, ref.property );
        if ( !prop )
        {
            m_lastError = QString( "operand '%1': node '%2' has no channel for '%3'" )
                              .arg( operand ).arg( node->getName() ).arg( ref.property );
        }
        return prop;
    }

    // 2. Another indexed morph -- ensure it is injected, then take its value
    //    channel. This is the recursion that closes through InjectorCore.
    DzNumericProperty* prop = resolveIndexedMorph( ref, ctx );
    if ( prop )
    {
        return prop;
    }

    // 3. Not an indexed morph: a property that is already live in the scene
    //    (a morph shipped with the figure, a control property, ...). For the
    //    pathless "#PropertyName?value" shape the *element* is the property
    //    name; the trailing "?value" is just its channel selector.
    DzNode* node = resolveNode( QString(), QString(), ctx );  // properties resolve on the context node
    prop = resolveSceneProperty( node, ref.element );
    if ( !prop )
    {
        m_lastError = QString( "operand '%1': not an indexed morph and no live property "
                               "named '%2' on node '%3'" )
                          .arg( operand )
                          .arg( ref.element )
                          .arg( node ? node->getName() : QString( "<null>" ) );
    }
    return prop;
}

}  // namespace daz_plugin
