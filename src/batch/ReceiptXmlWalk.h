#ifndef RECEIPTXMLWALK_H
#define RECEIPTXMLWALK_H

/* The Look Assist opt-out of the receipt element readers (ReceiptLoader::parseXmlElements and
 * MainWindow::readXmlElementsFromFile).
 *
 * Their token walk is base's, unchanged: every element handler ends with readElementText() then
 * readNext(), so in a compact receipt (no whitespace between elements) it reads the first element,
 * steps past the next one and stops at that one's end element. Reading the rest of a compact receipt
 * changed fields such as cutIn/cutOut with Look Assist off, so the off path was no longer base's
 * output (EXPORT-PRORES-LOOK-ASSIST-1 r2 B1, sol r1 on ed129ee1). What the walk misses that matters
 * to this card is an opt-out: <cutIn>1</cutIn><lookAssistEnabled>0</lookAssistEnabled> left Look Assist
 * on (adversary A1 on 76653777).
 *
 * plainLookAssistOptOut() therefore reads the receipt element again with a second reader on its own
 * handle of the same file, which leaves the main reader's position and error state exactly as base
 * leaves them. It looks only at the element's direct children in no namespace named lookAssistEnabled,
 * so an extension such as <x:lookAssistEnabled> neither hides a later plain opt-out nor overwrites an
 * earlier one (r2 B2). It answers true when the last such child says 0. The caller only ever turns
 * Look Assist OFF on that answer, so every receipt base read as off stays off, and nothing else the
 * walk read is touched. */

#include <QFile>
#include <QFileDevice>
#include <QXmlStreamReader>

namespace ReceiptXmlWalk
{

/* elementOffset / elementName: reader->characterOffset() and reader->qualifiedName() taken while the
 * main reader stands on the receipt (or session clip) start element whose children are read.
 * extensionSeen: the walk's lookAssistEnabled handler met one in a namespace.
 *
 * Called after the walk. When the walk stopped on the element's own end element it handled every
 * direct child (a skipped compact element always stops it on that child's end instead), so it read
 * every plain lookAssistEnabled, last one winning, exactly as the scan would; unless an extension
 * was among them there is nothing to correct and the file is not read again. Every receipt and
 * session MLV App writes is indented, so a session of many clips is still read once, as on base.
 *
 * False when the reader's device is not a named file, the file cannot be reopened, or the element
 * is not found. A parse error ends the scan with what it read before the error. */
inline bool plainLookAssistOptOut( QXmlStreamReader *reader, qint64 elementOffset,
                                   const QString &elementName, bool extensionSeen )
{
    if( !extensionSeen && reader->isEndElement() && reader->qualifiedName() == elementName )
        return false;

    QFileDevice *device = qobject_cast<QFileDevice *>( reader->device() );
    if( !device || device->fileName().isEmpty() ) return false;

    QFile file( device->fileName() );
    // The same Text flag as the main reader's handle, so both readers count the same characters.
    if( !file.open( QIODevice::ReadOnly | ( device->openMode() & QIODevice::Text ) ) ) return false;

    QXmlStreamReader scan( &file );
    bool found = false;
    while( !scan.atEnd() && !scan.hasError() )
    {
        scan.readNext();
        if( scan.isStartElement() && scan.characterOffset() == elementOffset )
        {
            found = true;
            break;
        }
    }
    if( !found ) return false;

    int depth = 0;      // 0: a direct child of the element
    int lastValue = -1; // -1: no plain lookAssistEnabled child read
    while( !scan.atEnd() && !scan.hasError() )
    {
        scan.readNext();
        if( scan.hasError() ) break;
        if( scan.isStartElement() )
        {
            if( depth == 0 && scan.namespaceUri().isEmpty()
             && scan.name() == QLatin1String( "lookAssistEnabled" ) )
            {
                const QString text = scan.readElementText();
                if( scan.hasError() ) break;
                lastValue = ( text.toInt() != 0 ) ? 1 : 0; // the handlers' (bool)toInt()
                continue;
            }
            ++depth;
        }
        else if( scan.isEndElement() )
        {
            if( depth == 0 ) break; // the element's own end
            --depth;
        }
    }
    return lastValue == 0;
}

} // namespace ReceiptXmlWalk

#endif // RECEIPTXMLWALK_H
