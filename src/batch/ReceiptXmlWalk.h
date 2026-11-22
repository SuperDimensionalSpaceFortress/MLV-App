#ifndef RECEIPTXMLWALK_H
#define RECEIPTXMLWALK_H

/* The token walk of the receipt element readers (ReceiptLoader::parseXmlElements and
 * MainWindow::readXmlElementsFromFile). Every element handler there ends with readElementText()
 * then readNext(). In a receipt written with whitespace between elements that readNext() lands on
 * the whitespace; in a compact one (<cutIn>1</cutIn><lookAssistEnabled>0</lookAssistEnabled>) it
 * lands on the NEXT start element, and the loop's own readNext() used to step past it, so that
 * element was never read and the walk stopped at its end element (EXPORT-PRORES-LOOK-ASSIST-1,
 * adversary A1 on 76653777). next() at the top of the loop, settle() at the bottom: a start element
 * a handler has already stepped onto is handed to the next pass instead of being skipped. With
 * whitespace between elements the walk is token for token what it was. */

#include <QXmlStreamReader>

class ReceiptXmlWalk
{
public:
    void next( QXmlStreamReader *reader )
    {
        if( !m_reuse ) reader->readNext();
        m_reuse = false;
        m_tokenAt = reader->characterOffset();
    }

    // Only an element in no namespace is handed on: the handlers compare local names, so a compact
    // <x:lookAssistEnabled> extension tag is stepped over exactly as before (adversary A5 on 16d54744).
    void settle( QXmlStreamReader *reader )
    {
        m_reuse = reader->isStartElement() && reader->characterOffset() != m_tokenAt
               && reader->namespaceUri().isEmpty();
    }

private:
    bool m_reuse = false;
    qint64 m_tokenAt = -1;
};

#endif // RECEIPTXMLWALK_H
