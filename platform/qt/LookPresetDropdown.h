/*!
 * \file LookPresetDropdown.h
 * \brief LOOK-PRESET-DROPDOWN-1: builds the windowed look dropdown (status bar) from LookPresetDropdownPolicy.h and
 *        sizes it so no look name is ever cropped, in the closed box or in the open list.
 */

#ifndef LOOKPRESETDROPDOWN_H
#define LOOKPRESETDROPDOWN_H

#include <QString>

class QComboBox;

namespace look_preset_dropdown
{

/*! \brief Fills the box with "Off" and every registry flavor (item data = the entry's key), turns eliding off and
 *  sizes the box and its list for the box's current font (applyNoElideSizing). Signals are blocked while filling. */
void populate( QComboBox *combo );

/*! \brief Sizes the box to the widest entry at its current font: the closed box's text field fits every name whole,
 *  and the list is at least as wide as its widest row. Call again after a font change. */
void applyNoElideSizing( QComboBox *combo );

/*! \brief The width, in the box's text field, that `text` needs at the box's font (font metrics, no padding). */
int textAdvance( const QComboBox *combo, const QString &text );

/*! \brief The width of the closed box's text field at the box's current size (the style's edit-field rect). */
int closedTextFieldWidth( const QComboBox *combo );

} // namespace look_preset_dropdown

#endif // LOOKPRESETDROPDOWN_H
