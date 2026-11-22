#ifndef RECEIPTXMLWALK_H
#define RECEIPTXMLWALK_H

/* The Look Assist opt-out of the receipt element readers (ReceiptLoader::parseXmlElements and
 * MainWindow::readXmlElementsFromFile).
 *
 * Their token walk is base's, unchanged: every element handler reads the element's text, then
 * readNext(), so in a compact receipt (no whitespace between elements) it reads the first element,
 * steps past the next one and stops at that one's end element. Reading the rest of a compact receipt
 * changed fields such as cutIn/cutOut with Look Assist off, so the off path was no longer base's
 * output (EXPORT-PRORES-LOOK-ASSIST-1 r2 B1, sol r1 on ed129ee1). What the walk misses that matters
 * to this card is an opt-out: <cutIn>1</cutIn><lookAssistEnabled>0</lookAssistEnabled> left Look Assist
 * on (adversary A1 on 76653777).
 *
 * OptOutWatch only observes the walk (it never moves the reader). When the walk may have missed or
 * mis-read an opt-out, optOut() reads the element again with a second reader over the SAME bytes the
 * walk parsed (the walk's device is a QBuffer the caller filled with one read of the file; a file
 * is never opened twice, so a file saved between the two reads cannot mix two versions: r2 adversary
 * A3). It looks only at the element's direct children in no namespace named lookAssistEnabled, so an
 * extension such as <x:lookAssistEnabled> neither hides a later plain opt-out nor overwrites an
 * earlier one (r2 B2), and answers true when the last such child holding an integer says 0 (a child with
 * no integer in it is skipped, lookAssistEnabledValue: r4, sol r3 B1). The caller only ever turns
 * Look Assist OFF on that answer, so every receipt base read as off stays off, and nothing else the
 * walk read is touched. */

#include <QBuffer>
#include <QByteArray>
#include <QString>
#include <QXmlStreamReader>

namespace ReceiptXmlWalk
{

/* The value of a lookAssistEnabled element, for the walk's handlers and the scan alike: true, with base's (bool)toInt()
 * in *on, when the text (surrounding whitespace aside) is an integer. An empty element, a child element or garbage text
 * is no value: the caller keeps what it had (the documented default is on), so a malformed field never reads as a
 * valid 0 and never switches Look Assist off (EXPORT-PRORES-LOOK-ASSIST-1 r4, sol r3 B1 on 875d5e40). */
inline bool lookAssistEnabledValue( const QString &text, bool *on )
{
    bool ok = false;
    const int value = text.trimmed().toInt( &ok );
    if( !ok ) return false;
    *on = ( value != 0 );
    return true;
}

class OptOutWatch
{
public:
    // Constructed while the reader stands on the receipt (or session clip) start element.
    explicit OptOutWatch( QXmlStreamReader *reader )
        : m_elementOffset( reader->characterOffset() )
    {}

    // In the lookAssistEnabled handler, before it reads the text: base's handler matches local names.
    void sawLookAssistEnabled( QXmlStreamReader *reader )
    {
        if( !reader->namespaceUri().isEmpty() ) m_extensionSeen = true;
    }

    // At the bottom of every pass of the walk. Only a handler's trailing readNext() can leave the reader
    // on a start element there (the catch-all handler reads every start element the loop lands on), and
    // the loop's next readNext() then steps past that element: the walk stops early (r2 adversary A1,
    // which a name comparison missed for a nested <receipt/>).
    void endOfPass( QXmlStreamReader *reader )
    {
        if( reader->isStartElement() ) m_steppedPast = true;
    }

    /* In the lookAssistEnabled handler: true while the walk is on the element's direct children. Once a pass ends on a
     * start element (endOfPass), the loop's next readNext() enters that element, and the walk stops at the first end
     * element it meets, so every element it reads from then on is nested inside a direct child. A lookAssistEnabled read
     * there is no value: the handler keeps what it had, as the scan does (it reads direct children only), so a nested
     * <lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled></lookAssistEnabled> never switches Look Assist off
     * and a later direct element still counts (EXPORT-PRORES-LOOK-ASSIST-1 r6, sol r5 B1 on feabee0a). */
    bool atDirectChild() const { return !m_steppedPast; }

    /* After the walk. With no element stepped past, no extension met and the walk on an end element,
     * the walk handled every direct child, so it read every plain lookAssistEnabled, last one winning,
     * exactly as the scan would: nothing to correct, nothing read again. Every receipt and session MLV
     * App writes is indented, so a session of many clips is read once, as on base. */
    bool optOut( QXmlStreamReader *reader ) const
    {
        if( !m_steppedPast && !m_extensionSeen && reader->isEndElement() && !reader->hasError() )
            return false;

        QBuffer *buffer = qobject_cast<QBuffer *>( reader->device() );
        if( !buffer ) return false;

        QXmlStreamReader scan( buffer->data() );
        bool found = false;
        while( !scan.atEnd() && !scan.hasError() )
        {
            scan.readNext();
            if( scan.isStartElement() && scan.characterOffset() == m_elementOffset )
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
                 && scan.name() == QString( "lookAssistEnabled" ) )
                {
                    // Read token by token to the element's own end. A child element in it (mixed content
                    // such as 0<value/>, which the handlers' text read refuses) makes it no value; the scan
                    // goes on, so a malformed earlier one cannot hide a later scalar opt-out (r2 adversary
                    // A2; r4 adversary A1 on 07462a6a). Its text is read with the handlers' rule: one with
                    // no integer in it is no value and changes nothing (r4, sol r3 B1).
                    QString text;
                    bool childElement = false;
                    int inner = 0;
                    while( !scan.atEnd() )
                    {
                        scan.readNext();
                        if( scan.hasError() ) break;
                        if( scan.isStartElement() )
                        {
                            childElement = true;
                            ++inner;
                        }
                        else if( scan.isEndElement() )
                        {
                            if( inner == 0 ) break; // the lookAssistEnabled element's own end
                            --inner;
                        }
                        else if( inner == 0 && scan.isCharacters() )
                            text += scan.text();
                    }
                    if( scan.hasError() ) break;
                    bool on = true;
                    if( !childElement && lookAssistEnabledValue( text, &on ) ) lastValue = on ? 1 : 0;
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

private:
    qint64 m_elementOffset = -1;
    bool m_steppedPast = false;
    bool m_extensionSeen = false;
};

} // namespace ReceiptXmlWalk

#endif // RECEIPTXMLWALK_H
