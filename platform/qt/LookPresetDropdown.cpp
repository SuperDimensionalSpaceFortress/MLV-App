/*!
 * \file LookPresetDropdown.cpp
 * \brief LOOK-PRESET-DROPDOWN-1: the windowed look dropdown's widget side (see LookPresetDropdown.h).
 */

#include "LookPresetDropdown.h"
#include "LookPresetDropdownPolicy.h"

#include <QAbstractItemView>
#include <QComboBox>
#include <QFontMetrics>
#include <QSignalBlocker>
#include <QStyle>
#include <QStyleOptionComboBox>

namespace look_preset_dropdown
{

namespace
{

// Room between the name and the edge of the text field: the style's label draw insets the field by 1 px a side.
constexpr int kTextSlackPx = 4;

int textFieldWidthAt( const QComboBox *combo, int boxWidth )
{
    QStyleOptionComboBox opt;
    opt.initFrom( combo );
    opt.editable = false;
    opt.frame = combo->hasFrame();
    opt.rect = QRect( 0, 0, boxWidth, qMax( combo->height(), combo->sizeHint().height() ) );
    return combo->style()->subControlRect( QStyle::CC_ComboBox, &opt, QStyle::SC_ComboBoxEditField, combo ).width();
}

} // namespace

int textAdvance( const QComboBox *combo, const QString &text )
{
    return combo ? QFontMetrics( combo->font() ).horizontalAdvance( text ) : 0;
}

int closedTextFieldWidth( const QComboBox *combo )
{
    return combo ? textFieldWidthAt( combo, combo->width() ) : 0;
}

void applyNoElideSizing( QComboBox *combo )
{
    if( !combo ) return;
    int widest = 0;
    for( int i = 0; i < combo->count(); ++i ) widest = qMax( widest, textAdvance( combo, combo->itemText( i ) ) );
    const int needed = widest + kTextSlackPx;

    // Start from the style's own answer for that much text, then widen until the text field really holds it: styles
    // differ in how much of the box the arrow and the frame take.
    QStyleOptionComboBox opt;
    opt.initFrom( combo );
    opt.editable = false;
    opt.frame = combo->hasFrame();
    const QSize contents( needed, QFontMetrics( combo->font() ).height() );
    int boxWidth = qMax( combo->style()->sizeFromContents( QStyle::CT_ComboBox, &opt, contents, combo ).width(),
                         combo->sizeHint().width() );
    for( int guard = 0; guard < 8; ++guard )
    {
        const int deficit = needed - textFieldWidthAt( combo, boxWidth );
        if( deficit <= 0 ) break;
        boxWidth += deficit;
    }
    combo->setMinimumWidth( boxWidth );

    // The open list: never elide, and at least as wide as its widest row.
    QAbstractItemView *view = combo->view();
    view->setTextElideMode( Qt::ElideNone );
    const int rowWidth = view->sizeHintForColumn( 0 );
    if( rowWidth > 0 ) view->setMinimumWidth( rowWidth + 2 * view->style()->pixelMetric( QStyle::PM_DefaultFrameWidth ) );
}

void populate( QComboBox *combo )
{
    if( !combo ) return;
    const QSignalBlocker blocker( combo );
    combo->clear();
    for( const Entry &entry : entries() ) combo->addItem( entry.displayName, entry.key );
    combo->setSizeAdjustPolicy( QComboBox::AdjustToContents );
    combo->setMaxVisibleItems( qMax( combo->maxVisibleItems(), combo->count() ) );
    applyNoElideSizing( combo );
}

} // namespace look_preset_dropdown
