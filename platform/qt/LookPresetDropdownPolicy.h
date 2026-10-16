/*!
 * \file LookPresetDropdownPolicy.h
 * \brief LOOK-PRESET-DROPDOWN-1: the windowed look dropdown's entries and how an entry maps to the Look Assist
 *        state (the receipt's switch plus the flavor). Pure (QtCore only), so console_tests pins it; the widget
 *        is built by LookPresetDropdown.h.
 *
 * Owner, 2026-10-15: "User should be able to select from our Look Presets via a dropdown when in windowed mode
 * and have it change on the fly. The dropdown should have our Look Name without cropping". The list is "Off"
 * followed by the flavor registry (lookAssistFlavors), each under the name the look label shows.
 */

#ifndef LOOKPRESETDROPDOWNPOLICY_H
#define LOOKPRESETDROPDOWNPOLICY_H

#include "LookLabelPolicy.h"
#include "../../src/batch/LookAssistAnalysis.h"

#include <QString>
#include <QVector>

namespace look_preset_dropdown
{

/*! \brief The key of the "Off" entry; every other key is a flavor's machine name (lookAssistFlavorName). */
inline QString offKey() { return QStringLiteral( "off" ); }

struct Entry
{
    QString key;           // "off" or the flavor's machine name, stored as the item's data
    QString displayName;   // what the user reads: Off, then look_label::flavorDisplayName of each flavor
};

/*! \brief "Off", then every flavor of the registry in its order. */
inline QVector<Entry> entries()
{
    QVector<Entry> list;
    list.append( Entry{ offKey(), QStringLiteral( "Off" ) } );
    for( const lookassist::LookAssistFlavor flavor : lookassist::lookAssistFlavors() )
    {
        const QString name = lookassist::lookAssistFlavorName( flavor );
        list.append( Entry{ name, look_label::flavorDisplayName( name ) } );
    }
    return list;
}

/*! \brief The entry that shows this state: "Off" while Look Assist is off, else the flavor's entry. -1 for a
 *  flavor the registry does not know. */
inline int indexFor( bool lookAssistEnabled, const QString &flavorName )
{
    const QVector<Entry> list = entries();
    if( !lookAssistEnabled ) return 0;
    const QString name = flavorName.trimmed().toLower();
    for( int i = 1; i < list.size(); ++i )
    {
        if( list.at( i ).key == name ) return i;
    }
    return -1;
}

/*! \brief What choosing an entry asks for. Off switches Look Assist off and keeps the flavor setting; a flavor
 *  entry switches Look Assist on with that flavor. */
struct Selection
{
    bool valid = false;
    bool lookAssistEnabled = false;
    QString flavorName;   // empty for Off
};

inline Selection selectionFor( const QString &key )
{
    Selection selection;
    const QString k = key.trimmed().toLower();
    if( k == offKey() )
    {
        selection.valid = true;
        return selection;
    }
    for( const lookassist::LookAssistFlavor flavor : lookassist::lookAssistFlavors() )
    {
        if( lookassist::lookAssistFlavorName( flavor ) != k ) continue;
        selection.valid = true;
        selection.lookAssistEnabled = true;
        selection.flavorName = k;
        break;
    }
    return selection;
}

/*! \brief The display name with the most characters (Kodak Vision3 50D today). Sizing never uses it: the box
 *  measures every name with its own font (LookPresetDropdown.cpp). Tests and logs only. */
inline QString longestDisplayName()
{
    QString longest;
    for( const Entry &entry : entries() )
    {
        if( entry.displayName.size() > longest.size() ) longest = entry.displayName;
    }
    return longest;
}

} // namespace look_preset_dropdown

#endif // LOOKPRESETDROPDOWNPOLICY_H
