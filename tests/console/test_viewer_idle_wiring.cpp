// PLAYER-CURSOR-AUTOHIDE-1: wiring pins for the fullscreen cursor auto-hide. MainWindow.cpp needs
// a full GUI build (not linked into console_tests), so the call sites are pinned as source text,
// following test_playback_fullscreen_ui_wiring.cpp. This file deliberately includes neither
// ViewerIdlePolicy.h nor ViewerCursorSurfaces.h, so it compiles on a base without them (RED proof).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QString>
#include <QStringList>
#include <QTextStream>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

// The body of one function, from its definition to its closing brace in column 0.
QString functionBodyToBrace(const QString & source, const QString & signature)
{
    const int start = source.indexOf(signature);
    if (start < 0) return QString();
    const int end = source.indexOf(QStringLiteral("\n}\n"), start);
    if (end <= start) return QString();
    return source.mid(start, end - start);
}

} // namespace

// W1: one call in the application filter, after the Resize branch and as the last statement
// before the filter's return, so every existing branch is unchanged and every event is seen.
TEST(ViewerIdleWiring, EventFilterNotesEveryEventOnceBeforeItsReturn)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool MainWindow::eventFilter(QObject *watched, QEvent *event)"),
        QStringLiteral("bool MainWindow::playbackPolicyActive( void ) const"));
    ASSERT_FALSE(body.isEmpty());
    const QString call = QStringLiteral("viewerIdleNoteEvent( watched, event );");
    ASSERT_EQ(1, int(body.count(call)));
    const int resizeAt = body.indexOf(QStringLiteral("QEvent::Resize"));
    const int callAt = body.indexOf(call);
    ASSERT_TRUE(resizeAt >= 0);
    ASSERT_TRUE(callAt > resizeAt);

    const QStringList lines = body.split(QLatin1Char('\n'));
    int returnLine = -1;
    for (int i = 0; i < lines.size(); ++i)
    {
        if (lines.at(i).trimmed() == QStringLiteral("return QWidget::eventFilter(watched, event);")) returnLine = i;
    }
    ASSERT_TRUE(returnLine > 0);
    if (returnLine > 0)
    {
        ASSERT_TRUE(lines.at(returnLine - 1).trimmed().startsWith(call));
    }
}

// W2: start/stop is QAction::toggled on actionFullscreen; arming any of the three tools restores
// the cursor at once. Wired in the constructor after the application filter is installed.
TEST(ViewerIdleWiring, ConstructorConnectsFullscreenAndToolToggles)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString block = functionBody(source,
        QStringLiteral("qApp->installEventFilter( this );"),
        QStringLiteral("//Set bools for draw rules"));
    ASSERT_FALSE(block.isEmpty());
    ASSERT_TRUE(block.contains(QStringLiteral("m_viewerIdleTimer->setSingleShot( true );")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( m_viewerIdleTimer, &QTimer::timeout")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->actionFullscreen, &QAction::toggled")));
    ASSERT_TRUE(block.contains(QStringLiteral("viewerIdleFullscreenChanged( on );")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->actionWhiteBalancePicker, &QAction::toggled")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->toolButtonBadPixelsSearchMethodEdit, &QAbstractButton::toggled")));
    ASSERT_TRUE(block.contains(QStringLiteral("connect( ui->toolButtonGradientPaint, &QAbstractButton::toggled")));
    ASSERT_EQ(3, int(block.count(QStringLiteral("viewerIdleRestore();"))));
}

// W3: on_actionFullscreen_triggered is untouched (the toggled path owns start/stop and the
// restore on leaving fullscreen); no app-wide override cursor anywhere in MainWindow.cpp.
TEST(ViewerIdleWiring, FullscreenSlotUntouchedAndLeavingRestoresWithoutOverrideCursor)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString slot = functionBody(source,
        QStringLiteral("void MainWindow::on_actionFullscreen_triggered( bool checked )"),
        QStringLiteral("void MainWindow::exportHandler( void )"));
    ASSERT_FALSE(slot.isEmpty());
    ASSERT_FALSE(slot.contains(QStringLiteral("viewerIdle")));
    ASSERT_FALSE(source.contains(QStringLiteral("setOverrideCursor")));

    const QString changed = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleFullscreenChanged( bool on )"));
    ASSERT_FALSE(changed.isEmpty());
    ASSERT_TRUE(changed.contains(QStringLiteral("m_viewerIdleTimer->stop();")));
    ASSERT_TRUE(changed.contains(QStringLiteral("m_viewerIdle.forceShown()")));
    ASSERT_TRUE(changed.contains(QStringLiteral("m_viewerCursorHider.restore( viewerIdleSurfaces() );")));
}

// W4: the hide gate reads real window state and all three tools; the surfaces are read fresh,
// including the GL display window (never a cached pointer).
TEST(ViewerIdleWiring, InputsReadWindowStateAndToolsAndSurfacesReadGlWindowFresh)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString inputs = functionBodyToBrace(source,
        QStringLiteral("viewer_idle::Inputs MainWindow::viewerIdleInputs( void ) const"));
    ASSERT_FALSE(inputs.isEmpty());
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->actionFullscreen->isChecked()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("isFullScreen()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("isActiveWindow()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("QApplication::activePopupWidget()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("QApplication::activeModalWidget()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->actionWhiteBalancePicker->isChecked()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->toolButtonBadPixelsSearchMethodEdit->isChecked()")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("ui->toolButtonGradientPaint->isChecked()")));

    const QString surfaces = functionBodyToBrace(source,
        QStringLiteral("viewer_idle::Surfaces MainWindow::viewerIdleSurfaces( void ) const"));
    ASSERT_FALSE(surfaces.isEmpty());
    ASSERT_TRUE(surfaces.contains(QStringLiteral("ui->graphicsView->viewport()")));
    ASSERT_TRUE(surfaces.contains(QStringLiteral("GpuDisplayWindow::activeWindow()")));

    // Windowed mode costs one bool test: the note returns at once unless the controller is fullscreen.
    const QString note = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleNoteEvent( QObject *watched, QEvent *event )"));
    ASSERT_FALSE(note.isEmpty());
    ASSERT_TRUE(note.contains(QStringLiteral("if( !m_viewerIdle.fullscreen() ) return;")));
    ASSERT_TRUE(note.contains(QStringLiteral("m_viewerIdlePointer.seed( QCursor::pos() );")));

    // Adversary A2: every tick also asks the OS pointer, so movement during a wait that excludes user input
    // (no mouse event delivered, timers still run) keeps / brings the cursor back; Hide and Show re-arm.
    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    ASSERT_TRUE(timeout.contains(QStringLiteral("in.pointerMoved = m_viewerIdlePointer.moved( QCursor::pos() );")));
    ASSERT_TRUE(timeout.contains(QStringLiteral("case viewer_idle::Tick::Show:")));
    // Adversary pass 2 A1: a move away and back between two ticks leaves the sampled position unchanged, so
    // on Windows the tick also asks whether a WM_MOUSEMOVE is still queued (an input-excluded wait leaves
    // it there), guarded to Windows.
    // r2 sibling sweep: a button still queued is the same undelivered input, so the queue question is QS_MOUSE.
    // r7 (key B1): the tick asks it through lookAtQueue (QueueLook grammar), never as a bare status test.
    const int queueAt = timeout.indexOf(QStringLiteral("if( viewer_idle::win_hook::lookAtQueue() ) in.pointerMoved = true;"));
    ASSERT_TRUE(queueAt >= 0);
    ASSERT_FALSE(timeout.contains(QStringLiteral("GetQueueStatus(")));
    const int guardAt = timeout.lastIndexOf(QStringLiteral("#ifdef Q_OS_WIN"), queueAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(timeout.indexOf(QStringLiteral("#endif"), guardAt) > queueAt);
    const QString hookSource = readRepoFile(QStringLiteral("platform/qt/ViewerMouseRetrievalHook.h"));
    const QString look = functionBodyToBrace(hookSource, QStringLiteral("inline bool lookAtQueue()"));
    ASSERT_TRUE(look.contains(QStringLiteral("queueStatusReader()( QS_MOUSE )")));
    ASSERT_EQ(3, int(timeout.count(QStringLiteral("m_viewerIdleTimer->start( ms );"))));
}

// W5 (r2, adversary r1 pass 3 A1): Qt's excluded wait defers the WM_MOUSEMOVE out of the Win32 queue, so the
// tick also reads the thread mouse hook's witness, before the switch and inside the Windows guard. The hook is
// installed on entering fullscreen and removed on leaving it and in the destructor, and both event-path
// activity notes clear it so delivered input is not counted again at the next tick.
TEST(ViewerIdleWiring, TickReadsTheRetrievalHookInstalledOnlyWhileFullscreen)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("#include \"ViewerMouseRetrievalHook.h\"")));

    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    const QString take = QStringLiteral("if( viewer_idle::win_hook::witness().take() ) in.pointerMoved = true;");
    const int takeAt = timeout.indexOf(take);
    ASSERT_TRUE(takeAt >= 0);
    ASSERT_EQ(1, int(timeout.count(take)));
    const int guardAt = timeout.lastIndexOf(QStringLiteral("#ifdef Q_OS_WIN"), takeAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(timeout.indexOf(QStringLiteral("#endif"), guardAt) > takeAt);
    ASSERT_TRUE(takeAt > timeout.indexOf(QStringLiteral("in.pointerMoved = m_viewerIdlePointer.moved( QCursor::pos() );")));
    ASSERT_TRUE(takeAt < timeout.indexOf(QStringLiteral("switch( m_viewerIdle.onTimeout(")));

    const QString changed = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleFullscreenChanged( bool on )"));
    ASSERT_FALSE(changed.isEmpty());
    const int installAt = changed.indexOf(QStringLiteral("viewer_idle::win_hook::install();"));
    const int elseAt = changed.indexOf(QStringLiteral("else"));
    const int removeAt = changed.indexOf(QStringLiteral("viewer_idle::win_hook::remove();"));
    ASSERT_TRUE(installAt >= 0);
    ASSERT_TRUE(elseAt > installAt);
    ASSERT_TRUE(removeAt > elseAt);

    const QString dtor = functionBodyToBrace(source, QStringLiteral("MainWindow::~MainWindow()"));
    ASSERT_FALSE(dtor.isEmpty());
    ASSERT_TRUE(dtor.contains(QStringLiteral("viewer_idle::win_hook::remove();")));

    const QString clear = QStringLiteral("viewer_idle::win_hook::witness().clear();");
    const QString note = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleNoteEvent( QObject *watched, QEvent *event )"));
    ASSERT_EQ(1, int(note.count(clear)));
    const QString restore = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleRestore( void )"));
    ASSERT_EQ(1, int(restore.count(clear)));
}

// W6 (r3, adversary r1 pass 4 A1): the position sample is the only witness off Windows, so the tick hands the
// controller the poll cap (outside any platform guard) and every start of the idle timer is at most one poll
// period: entering fullscreen, the restore path and the event path start it with kPointerPollMs(), never with
// the full idle interval.
TEST(ViewerIdleWiring, TickPassesThePollCapOnEveryPlatformAndEveryStartIsOnePollPeriod)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    const QString call = QStringLiteral(
        "switch( m_viewerIdle.onTimeout( in, m_viewerIdleClock.elapsed(), viewer_idle::kIdleHideMs(), &ms, "
        "viewer_idle::kPointerPollMs() ) )");
    const int callAt = timeout.indexOf(call);
    ASSERT_TRUE(callAt >= 0);
    // Not inside a platform guard: the last #ifdef before the call is closed before it.
    const int guardAt = timeout.lastIndexOf(QStringLiteral("#ifdef"), callAt);
    if (guardAt >= 0)
    {
        const int endAt = timeout.indexOf(QStringLiteral("#endif"), guardAt);
        ASSERT_TRUE(endAt >= 0);
        ASSERT_TRUE(endAt < callAt);
    }

    const QString pollStart = QStringLiteral("m_viewerIdleTimer->start( viewer_idle::kPointerPollMs() );");
    const QString idleStart = QStringLiteral("m_viewerIdleTimer->start( viewer_idle::kIdleHideMs() );");
    ASSERT_FALSE(source.contains(idleStart));
    const QString changed = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleFullscreenChanged( bool on )"));
    ASSERT_EQ(1, int(changed.count(pollStart)));
    const QString restore = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleRestore( void )"));
    ASSERT_EQ(1, int(restore.count(pollStart)));
    const QString note = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleNoteEvent( QObject *watched, QEvent *event )"));
    ASSERT_EQ(1, int(note.count(pollStart)));
}

// W7 (r4, adversary r1 pass 6 A1): the sampled witness is QCursor::pos(), a logical QPoint, and Qt has no public
// sub-logical-pixel cursor position. The sub-logical-pixel bound is stated as a non-promise next to the
// between-samples bound, in the policy header (PointerWatch, file comment) and at the tick that samples, and no
// text there still claims integer positions are enough. P10 pins the behaviour.
TEST(ViewerIdleWiring, SampledWitnessStatesItsSubLogicalPixelBound)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/ViewerIdlePolicy.h"));
    ASSERT_FALSE(header.isEmpty());
    ASSERT_FALSE(header.contains(QStringLiteral("Integer positions are enough")));
    const int watchAt = header.indexOf(QStringLiteral("class PointerWatch"));
    ASSERT_TRUE(watchAt > 0);
    const int docAt = header.lastIndexOf(QStringLiteral("/*!"), watchAt);
    ASSERT_TRUE(docAt >= 0);
    const QString watchDoc = header.mid(docAt, watchAt - docAt);
    ASSERT_TRUE(watchDoc.contains(QStringLiteral("Sub-logical-pixel bound")));
    ASSERT_TRUE(watchDoc.contains(QStringLiteral("less than one logical pixel")));
    ASSERT_TRUE(watchDoc.contains(QStringLiteral("one logical pixel or more")));
    ASSERT_TRUE(watchDoc.contains(QStringLiteral("non-promise")));
    const int fileDocEnd = header.indexOf(QStringLiteral("#ifndef VIEWERIDLEPOLICY_H"));
    ASSERT_TRUE(fileDocEnd > 0);
    ASSERT_TRUE(header.left(fileDocEnd).contains(QStringLiteral("less than one logical pixel")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    ASSERT_TRUE(timeout.contains(QStringLiteral("less than one logical pixel")));
}

// W8 (r6, key B1; pass 8 A1): Windows coalesces an away-and-back into one move at the starting pixel. Every tick
// marks the system move history after it read the witness and the queue status and before the switch (inside the
// Windows guard, once, shown or hidden), and install() marks on entering fullscreen; the hook reads the history only
// for a same-position move while marked, notes an excursion, and unmarks on pending movement and on remove(). No
// comment still says, without condition, that the hook sees a between-samples excursion at once.
TEST(ViewerIdleWiring, EveryTickMarksTheMoveHistoryAndTheHookReadsItForASamePositionMove)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    // r7 (key B1, opus H1): the tick's mark is lookAtQueue's, made BEFORE it reads the queue status, with the one
    // history read that judged the queued input against the previous mark.
    const QString mark = QStringLiteral("if( viewer_idle::win_hook::lookAtQueue() ) in.pointerMoved = true;");
    ASSERT_EQ(1, int(timeout.count(mark)));
    ASSERT_EQ(0, int(source.count(QStringLiteral("viewer_idle::win_hook::markHistory();"))));
    const int markAt = timeout.indexOf(mark);
    const QString lookBody = functionBodyToBrace(readRepoFile(QStringLiteral("platform/qt/ViewerMouseRetrievalHook.h")),
                                                 QStringLiteral("inline bool lookAtQueue()"));
    const int lookMarkAt = lookBody.indexOf(QStringLiteral("q.haveCursor = markHistoryAt( &q.cursor, &q.leftSinceMark );"));
    const int queueAt = lookBody.indexOf(QStringLiteral("queueStatusReader()( QS_MOUSE )"));
    const int takeAt = timeout.indexOf(QStringLiteral("if( viewer_idle::win_hook::witness().take() ) in.pointerMoved = true;"));
    const int switchAt = timeout.indexOf(QStringLiteral("switch( m_viewerIdle.onTimeout("));
    ASSERT_TRUE(takeAt >= 0);
    ASSERT_TRUE(lookMarkAt >= 0 && queueAt > lookMarkAt);
    ASSERT_TRUE(markAt > takeAt);
    ASSERT_TRUE(markAt < switchAt);
    const int guardAt = timeout.lastIndexOf(QStringLiteral("#ifdef Q_OS_WIN"), markAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(timeout.indexOf(QStringLiteral("#endif"), guardAt) > markAt);
    ASSERT_FALSE(timeout.contains(QStringLiteral("the hook sees it at once")));

    const QString hook = readRepoFile(QStringLiteral("platform/qt/ViewerMouseRetrievalHook.h"));
    ASSERT_FALSE(hook.isEmpty());
    const QString proc = functionBodyToBrace(hook,
        QStringLiteral("inline LRESULT CALLBACK proc( int code, WPARAM wParam, LPARAM lParam )"));
    ASSERT_FALSE(proc.isEmpty());
    ASSERT_TRUE(proc.contains(QStringLiteral(
        "if( kind == RetrievedKind::Move && historyMark().marked() && witness().samePosition( pos ) )")));
    ASSERT_TRUE(proc.contains(QStringLiteral(
        "if( historyMark().leftSinceMark( points, n, pos.x(), pos.y() ) ) witness().noteExcursion();")));
    ASSERT_TRUE(proc.indexOf(QStringLiteral("witness().noteExcursion();"))
                < proc.indexOf(QStringLiteral("witness().note( kind, pos, code == HC_ACTION );")));
    ASSERT_TRUE(proc.contains(QStringLiteral("if( witness().pending() ) historyMark().unmark();")));
    const QString removeBody = functionBodyToBrace(hook, QStringLiteral("inline void remove()"));
    ASSERT_TRUE(removeBody.contains(QStringLiteral("historyMark().unmark();")));
    const QString installBody = functionBodyToBrace(hook, QStringLiteral("inline bool install()"));
    ASSERT_TRUE(installBody.contains(QStringLiteral("markHistory();")));
    ASSERT_TRUE(hook.contains(QStringLiteral("GetMouseMovePointsEx")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/ViewerIdlePolicy.h"));
    ASSERT_FALSE(header.contains(QStringLiteral("on Windows the thread mouse hook sees it at once")));
    ASSERT_FALSE(header.contains(QStringLiteral("and on Windows at once by the thread mouse hook")));
    const int markClassAt = header.indexOf(QStringLiteral("class MoveHistoryMark"));
    ASSERT_TRUE(markClassAt > 0);
    const int markDocAt = header.lastIndexOf(QStringLiteral("/*!"), markClassAt);
    const QString markDoc = header.mid(markDocAt, markClassAt - markDocAt);
    ASSERT_TRUE(markDoc.contains(QStringLiteral("Grammar:")));
    ASSERT_TRUE(markDoc.contains(QStringLiteral("Non-promises:")));
    ASSERT_TRUE(markDoc.contains(QStringLiteral("coalesces")));
}

// W9 (r6, adversary pass 9 A1): the tick hands the controller witnessMissing = the hook is not installed, inside the
// Windows guard and before the switch, so a failed SetWindowsHookExW never leaves the cursor hidden.
TEST(ViewerIdleWiring, TickGatesTheHideOnTheInstalledRetrievalHook)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString timeout = functionBodyToBrace(source,
        QStringLiteral("void MainWindow::viewerIdleTimeout( void )"));
    ASSERT_FALSE(timeout.isEmpty());
    const QString gate = QStringLiteral("in.witnessMissing = !viewer_idle::win_hook::installed();");
    ASSERT_EQ(1, int(timeout.count(gate)));
    const int gateAt = timeout.indexOf(gate);
    ASSERT_TRUE(gateAt < timeout.indexOf(QStringLiteral("switch( m_viewerIdle.onTimeout(")));
    const int guardAt = timeout.lastIndexOf(QStringLiteral("#ifdef Q_OS_WIN"), gateAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(timeout.indexOf(QStringLiteral("#endif"), guardAt) > gateAt);
    const QString header = readRepoFile(QStringLiteral("platform/qt/ViewerIdlePolicy.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("&& !i.witnessMissing;")));
}
