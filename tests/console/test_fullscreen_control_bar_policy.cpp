// PLAYER-FULLSCREEN-CONTROL-BAR-1: the fullscreen control bar's pure seam (FullscreenControlBarPolicy.h) on #855's one
// idle clock (viewer_idle::Controller), driven with a fake clock. The surfaces (GL quad, CPU widget) are covered by
// gui_tests and pinned as source by test_fullscreen_control_bar_wiring.cpp.
#include "../common/minitest.h"

#include "../../platform/qt/FullscreenControlBarPolicy.h"
#include "../../platform/qt/ViewerIdlePolicy.h"

#include <QEvent>
#include <QPoint>
#include <QPointF>
#include <QSize>

namespace
{

viewer_idle::Inputs allPass()
{
    viewer_idle::Inputs in;
    in.fullscreen = true;
    in.windowActive = true;
    return in;
}

// What MainWindow does with the idle clock and one surface, minus Qt: activity shows the bar, a Hide tick hides it,
// and a surface draw rasterises and uploads only what the Presenter asks for.
struct Rig
{
    viewer_idle::Controller idle;
    fullscreen_bar::Presenter bar;
    QSize barSize = fullscreen_bar::layoutFor(QSize(1920, 1080)).bar.size();

    void enter(qint64 now) { idle.setFullscreen(true, now); }
    void activity(qint64 now)
    {
        idle.noteActivity(now);
        bar.show(now);
    }
    viewer_idle::Tick tick(const viewer_idle::Inputs &in, qint64 now)
    {
        int rearm = 0;
        const viewer_idle::Tick t = idle.onTimeout(in, now, viewer_idle::kIdleHideMs(), &rearm, viewer_idle::kPointerPollMs());
        if (t == viewer_idle::Tick::Hide) bar.hide(now);
        return t;
    }
    void feed(int frame, bool playing)
    {
        fullscreen_bar::Feed f;
        f.frame = frame;
        f.totalFrames = 600;
        f.playing = playing;
        f.timecode = QStringLiteral("00:00:%1:%2").arg(frame / 24, 2, 10, QLatin1Char('0')).arg(frame % 24, 2, 10, QLatin1Char('0'));
        bar.noteFeed(f);
    }
    // One paint of the surface (the GL paintGL or the CPU widget's paintEvent; neither paints the bar while hidden).
    void draw()
    {
        const fullscreen_bar::Raster r = bar.rasterFor(barSize);
        if (r == fullscreen_bar::Raster::None) return;
        bar.noteRaster(r, barSize);
        bar.noteUpload();
    }
};

int tickValue(viewer_idle::Tick t) { return int(t); }
int partValue(fullscreen_bar::Part p) { return int(p); }

} // namespace

TEST(FullscreenControlBarPolicy, ShowsOnMove)
{
    Rig rig;
    rig.enter(0);
    ASSERT_FALSE(rig.bar.shown());
    rig.activity(100);
    ASSERT_TRUE(rig.bar.shown());
    ASSERT_EQ(1, rig.bar.shows());
    rig.activity(150);   // already shown: not a second show
    ASSERT_EQ(1, rig.bar.shows());

    // A move still at the fullscreen entry position (Windows synthesizes one) does not ask for the bar; a press does,
    // and so does a move once it has left the entry position (then every later move does).
    bool sawMove = false;
    const QPoint entry(500, 500);
    ASSERT_FALSE(fullscreen_bar::activityShowsBar(QEvent::MouseMove, &sawMove, entry, entry));
    ASSERT_FALSE(fullscreen_bar::activityShowsBar(QEvent::MouseMove, &sawMove, entry, QPoint(501, 500)));
    ASSERT_FALSE(sawMove);
    ASSERT_TRUE(fullscreen_bar::activityShowsBar(QEvent::MouseButtonPress, &sawMove, entry, entry));
    ASSERT_TRUE(fullscreen_bar::activityShowsBar(QEvent::Wheel, &sawMove, entry, entry));
    ASSERT_TRUE(fullscreen_bar::activityShowsBar(QEvent::MouseMove, &sawMove, entry, QPoint(510, 500)));
    ASSERT_TRUE(sawMove);
    ASSERT_TRUE(fullscreen_bar::activityShowsBar(QEvent::MouseMove, &sawMove, entry, entry));
}

TEST(FullscreenControlBarPolicy, HidesAfterIdle)
{
    Rig rig;
    rig.enter(0);
    rig.activity(1000);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(rig.tick(allPass(), 1000 + viewer_idle::kIdleHideMs() - 1)));
    ASSERT_TRUE(rig.bar.shown());
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(rig.tick(allPass(), 1000 + viewer_idle::kIdleHideMs())));
    ASSERT_FALSE(rig.bar.shown());
    ASSERT_EQ(2000, int(rig.bar.shownMs(5000)));   // shown exactly the idle period, nothing after the hide
    ASSERT_EQ(2000, viewer_idle::kIdleHideMs());
}

TEST(FullscreenControlBarPolicy, HeldWhileHovered)
{
    viewer_idle::Inputs held = allPass();
    held.overlayHeld = true;
    ASSERT_FALSE(viewer_idle::mayHide(held));
    ASSERT_TRUE(viewer_idle::mayHide(allPass()));

    Rig rig;
    rig.enter(0);
    rig.activity(0);
    for (qint64 t = 50; t <= 10000; t += 50)
    {
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(rig.tick(held, t)));
    }
    ASSERT_TRUE(rig.bar.shown());
    ASSERT_FALSE(rig.idle.hidden());
    // Released (the pointer left the bar, a move): the idle period starts again from that activity.
    rig.activity(10000);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(rig.tick(allPass(), 11999)));
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(rig.tick(allPass(), 12000)));
    ASSERT_FALSE(rig.bar.shown());
}

TEST(FullscreenControlBarPolicy, NoWorkWhileHidden)
{
    Rig rig;
    rig.enter(0);
    for (int i = 0; i < 600; ++i)
    {
        rig.feed(i, true);
        rig.draw();
    }
    ASSERT_EQ(0, rig.bar.rasterCount());
    ASSERT_EQ(0, rig.bar.uploadCount());
    ASSERT_EQ(0, rig.bar.shows());
    ASSERT_EQ(0, int(rig.bar.shownMs(100000)));
    ASSERT_FALSE(rig.bar.noteFeed(fullscreen_bar::Feed()));
    ASSERT_EQ(int(fullscreen_bar::Raster::None), int(rig.bar.rasterFor(rig.barSize)));

    rig.activity(100);
    rig.feed(600, false);
    rig.draw();
    ASSERT_EQ(1, rig.bar.rasterCount());
    ASSERT_EQ(1, rig.bar.uploadCount());
    rig.draw();   // nothing changed: the cached raster is drawn again
    ASSERT_EQ(1, rig.bar.rasterCount());

    // Hidden again: feeds and paints cost nothing; a re-show with the same state rasterises nothing.
    rig.bar.hide(200);
    for (int i = 0; i < 600; ++i)
    {
        rig.feed(i, true);
        rig.draw();
    }
    ASSERT_EQ(1, rig.bar.rasterCount());
    ASSERT_EQ(1, rig.bar.uploadCount());
    rig.bar.show(300);
    rig.draw();
    ASSERT_EQ(1, rig.bar.rasterCount());
}

TEST(FullscreenControlBarPolicy, TimecodeRasterOnlyOnChange)
{
    Rig rig;
    rig.enter(0);
    rig.activity(0);
    rig.feed(0, true);
    rig.draw();
    ASSERT_EQ(1, rig.bar.rasterCount());

    // Same timecode, new frame (the playhead moves; it is never rasterised): no raster.
    fullscreen_bar::Feed f = rig.bar.feed();
    f.frame = 1;
    ASSERT_TRUE(rig.bar.noteFeed(f));
    ASSERT_FALSE(rig.bar.needsRaster(f.timecode));
    ASSERT_EQ(int(fullscreen_bar::Raster::None), int(rig.bar.rasterFor(rig.barSize)));
    rig.draw();
    ASSERT_EQ(1, rig.bar.rasterCount());

    // A new timecode string: only its cell.
    f.timecode = QStringLiteral("00:00:00:01");
    ASSERT_TRUE(rig.bar.needsRaster(f.timecode));
    rig.bar.noteFeed(f);
    ASSERT_EQ(int(fullscreen_bar::Raster::Timecode), int(rig.bar.rasterFor(rig.barSize)));
    rig.draw();
    ASSERT_EQ(2, rig.bar.rasterCount());
    rig.draw();
    ASSERT_EQ(2, rig.bar.rasterCount());

    // Play state or size: the whole bar once.
    f.playing = false;
    rig.bar.noteFeed(f);
    ASSERT_EQ(int(fullscreen_bar::Raster::Full), int(rig.bar.rasterFor(rig.barSize)));
    rig.draw();
    ASSERT_EQ(3, rig.bar.rasterCount());
    ASSERT_EQ(int(fullscreen_bar::Raster::Full), int(rig.bar.rasterFor(QSize(rig.barSize.width() / 2, rig.barSize.height()))));
    // A lost GL context: the whole bar again.
    rig.bar.dropRaster();
    ASSERT_EQ(int(fullscreen_bar::Raster::Full), int(rig.bar.rasterFor(rig.barSize)));

    // 600 frames of 24 fps play: one raster per distinct timecode string, never per frame.
    Rig run;
    run.enter(0);
    run.activity(0);
    for (int i = 0; i < 600; ++i)
    {
        run.feed(i, true);
        run.draw();
    }
    ASSERT_EQ(600, run.bar.rasterCount());   // feed() makes a distinct string per frame: one cell each
    Rig sameSecond;
    sameSecond.enter(0);
    sameSecond.activity(0);
    for (int i = 0; i < 600; ++i)
    {
        fullscreen_bar::Feed g;
        g.frame = i;
        g.totalFrames = 600;
        g.playing = true;
        g.timecode = QStringLiteral("00:00:%1").arg(i / 24, 2, 10, QLatin1Char('0'));
        sameSecond.bar.noteFeed(g);
        sameSecond.draw();
    }
    ASSERT_EQ(25, sameSecond.bar.rasterCount());   // 1 full + 24 timecode changes
}

TEST(FullscreenControlBarPolicy, HitTestClaimsDoubleClickInsideBar)
{
    const fullscreen_bar::Layout l = fullscreen_bar::layoutFor(QSize(1920, 1080));
    ASSERT_EQ(60, l.bar.height());
    ASSERT_EQ(1020, l.bar.top());
    ASSERT_EQ(1920, l.bar.width());
    ASSERT_FALSE(l.seek.isEmpty());

    const QPointF onPlay = QRectF(l.play).center();
    const QPointF onExit = QRectF(l.exit).center();
    const QPointF onSeek = QRectF(l.seek).center();
    const QPointF onTimecode = QRectF(l.timecode).center();
    const QPointF above(960.0, 1019.5);
    ASSERT_EQ(partValue(fullscreen_bar::Part::Play), partValue(fullscreen_bar::hitTest(l, onPlay)));
    ASSERT_EQ(partValue(fullscreen_bar::Part::Prev), partValue(fullscreen_bar::hitTest(l, QRectF(l.prev).center())));
    ASSERT_EQ(partValue(fullscreen_bar::Part::Next), partValue(fullscreen_bar::hitTest(l, QRectF(l.next).center())));
    ASSERT_EQ(partValue(fullscreen_bar::Part::Exit), partValue(fullscreen_bar::hitTest(l, onExit)));
    ASSERT_EQ(partValue(fullscreen_bar::Part::Seek), partValue(fullscreen_bar::hitTest(l, onSeek)));
    ASSERT_EQ(partValue(fullscreen_bar::Part::Timecode), partValue(fullscreen_bar::hitTest(l, onTimecode)));
    ASSERT_EQ(partValue(fullscreen_bar::Part::None), partValue(fullscreen_bar::hitTest(l, above)));
    ASSERT_EQ(partValue(fullscreen_bar::Part::None), partValue(fullscreen_bar::hitTest(fullscreen_bar::Layout(), onPlay)));

    // The shown bar takes a double-click anywhere inside it (so the viewer never toggles fullscreen), never outside
    // it, and never while hidden.
    fullscreen_bar::InputState s;
    const fullscreen_bar::Part inside = fullscreen_bar::hitTest(l, onTimecode);
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::LeftButton, inside));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Exit));
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::hitTest(l, above)));
    ASSERT_FALSE(fullscreen_bar::claims(s, false, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::LeftButton, inside));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::Wheel, Qt::NoButton, Qt::NoButton, inside));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseMove, Qt::NoButton, Qt::NoButton, inside));
    // A viewer drag crossing the bar keeps its moves and its release.
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, inside));
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, inside));

    // A double-click on Exit exits once (the first press already did; Qt sends the second press as the double-click).
    ASSERT_EQ(int(fullscreen_bar::Action::ExitFullscreen),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Exit)));
    ASSERT_TRUE(s.owned != Qt::NoButton);
    // Its release is the bar's wherever it lands.
    ASSERT_TRUE(fullscreen_bar::claims(s, false, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None));
    ASSERT_EQ(int(fullscreen_bar::Action::None),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None)));
    ASSERT_FALSE(s.owned != Qt::NoButton);
    // On the timecode or bare bar a double-click does nothing but is still taken.
    ASSERT_EQ(int(fullscreen_bar::Action::None),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::LeftButton, inside)));
    fullscreen_bar::onMouse(&s, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, inside);

    // Buttons and the seek drag.
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Play)));
    fullscreen_bar::onMouse(&s, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::Play);
    ASSERT_EQ(int(fullscreen_bar::Action::Seek),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Seek)));
    ASSERT_TRUE(s.seekDrag);
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None));
    ASSERT_EQ(int(fullscreen_bar::Action::Seek),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None)));
    fullscreen_bar::onMouse(&s, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None);
    ASSERT_FALSE(s.seekDrag);
    ASSERT_FALSE(s.owned != Qt::NoButton);
    // A right press is taken but does nothing.
    ASSERT_EQ(int(fullscreen_bar::Action::None),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonPress, Qt::RightButton, Qt::RightButton, fullscreen_bar::Part::Play)));
}

TEST(FullscreenControlBarPolicy, LostReleaseNeverStealsTheViewer)
{
    fullscreen_bar::InputState s;
    fullscreen_bar::Action a = fullscreen_bar::Action::None;
    // The bar takes a seek press, then its release is lost (the window changed under it).
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Seek, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::Seek), int(a));
    ASSERT_TRUE(s.seekDrag);
    // Hidden now; a viewer press elsewhere is not the bar's and clears the stale state, so its drag and release stay
    // the viewer's.
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(s.seekDrag);
    ASSERT_FALSE(s.owned != Qt::NoButton);
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None, &a));

    // A press on Play, then the release is lost: the next buttonless move settles it, and a later viewer drag is kept.
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay), int(a));
    ASSERT_TRUE(s.owned != Qt::NoButton);
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseMove, Qt::NoButton, Qt::NoButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(s.owned != Qt::NoButton);
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));

    // The Exit press keeps its own release even though the bar is hidden by then.
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Exit, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::ExitFullscreen), int(a));
    ASSERT_TRUE(fullscreen_bar::route(&s, false, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::None), int(a));
    ASSERT_FALSE(s.owned != Qt::NoButton);
}

TEST(FullscreenControlBarPolicy, SeekFrameAtClamps)
{
    const fullscreen_bar::Layout l = fullscreen_bar::layoutFor(QSize(1920, 1080));
    const qreal left = l.seek.left();
    const qreal right = l.seek.left() + l.seek.width() - 1;
    ASSERT_EQ(0, fullscreen_bar::seekFrameAt(l, left, 600));
    ASSERT_EQ(599, fullscreen_bar::seekFrameAt(l, right, 600));
    ASSERT_EQ(0, fullscreen_bar::seekFrameAt(l, -1000.0, 600));
    ASSERT_EQ(599, fullscreen_bar::seekFrameAt(l, 100000.0, 600));
    const int mid = fullscreen_bar::seekFrameAt(l, (left + right) / 2.0, 601);
    ASSERT_EQ(300, mid);
    ASSERT_EQ(0, fullscreen_bar::seekFrameAt(l, right, 1));
    ASSERT_EQ(0, fullscreen_bar::seekFrameAt(l, right, 0));
    ASSERT_EQ(0, fullscreen_bar::seekFrameAt(fullscreen_bar::Layout(), right, 600));

    // The playhead stays inside the seek column at both ends.
    const QRect first = fullscreen_bar::playheadRect(l, 0, 600);
    const QRect last = fullscreen_bar::playheadRect(l, 599, 600);
    ASSERT_TRUE(l.seek.contains(first));
    ASSERT_TRUE(l.seek.contains(last));
    ASSERT_TRUE(last.left() > first.left());

    // Narrow and tiny surfaces never produce rects outside the bar; an empty surface has no bar.
    const fullscreen_bar::Layout narrow = fullscreen_bar::layoutFor(QSize(200, 1080));
    ASSERT_TRUE(narrow.bar.contains(narrow.play));
    ASSERT_TRUE(narrow.bar.contains(narrow.exit));
    ASSERT_TRUE(fullscreen_bar::layoutFor(QSize(0, 0)).bar.isEmpty());
    const fullscreen_bar::Layout k4 = fullscreen_bar::layoutFor(QSize(3840, 2160));
    ASSERT_EQ(120, k4.bar.height());
    ASSERT_TRUE(fullscreen_bar::summaryLine(0, 0, 0, 0) == QStringLiteral("control_bar.summary raster=0 upload=0 shown_ms=0 shows=0"));
}

// Adversary r1 pass 2 A1: a QWindow (the GL path) receives a native double-click as Press, Release,
// Press(MouseEventCreatedDoubleClick), DblClick, Release; a widget (the CPU bar) as Press, Release, DblClick, Release
// (QWidgetWindow drops the flagged press, QTBUG-25831). Each physical click acts once on both paths: Next steps twice,
// never three times; Play toggles twice.
TEST(FullscreenControlBarPolicy, DoubleClickActsOncePerPhysicalClick)
{
    struct Ev { QEvent::Type type; bool createdDoubleClick; };
    const Ev glSeq[] = { { QEvent::MouseButtonPress, false }, { QEvent::MouseButtonRelease, false },
                         { QEvent::MouseButtonPress, true }, { QEvent::MouseButtonDblClick, false },
                         { QEvent::MouseButtonRelease, false } };
    const Ev widgetSeq[] = { { QEvent::MouseButtonPress, false }, { QEvent::MouseButtonRelease, false },
                             { QEvent::MouseButtonDblClick, false }, { QEvent::MouseButtonRelease, false } };
    for (fullscreen_bar::Part part : { fullscreen_bar::Part::Next, fullscreen_bar::Part::Play, fullscreen_bar::Part::Prev })
    {
        fullscreen_bar::InputState gl;
        int glActs = 0;
        for (const Ev &e : glSeq)
        {
            const Qt::MouseButtons held = e.type != QEvent::MouseButtonRelease ? Qt::MouseButtons(Qt::LeftButton)
                                                                                : Qt::MouseButtons(Qt::NoButton);
            fullscreen_bar::Action a = fullscreen_bar::Action::None;
            ASSERT_TRUE(fullscreen_bar::route(&gl, true, e.type, Qt::LeftButton, held, part, &a));
            if (fullscreen_bar::actionOf(a, e.type, e.createdDoubleClick) != fullscreen_bar::Action::None) ++glActs;
        }
        ASSERT_EQ(2, glActs);
        ASSERT_TRUE(gl.owned == Qt::NoButton);

        fullscreen_bar::InputState cpu;
        int cpuActs = 0;
        for (const Ev &e : widgetSeq)
        {
            const Qt::MouseButtons held = e.type != QEvent::MouseButtonRelease ? Qt::MouseButtons(Qt::LeftButton)
                                                                                : Qt::MouseButtons(Qt::NoButton);
            const fullscreen_bar::Action a = fullscreen_bar::onMouse(&cpu, e.type, Qt::LeftButton, held, part);
            if (fullscreen_bar::actionOf(a, e.type, e.createdDoubleClick) != fullscreen_bar::Action::None) ++cpuActs;
        }
        ASSERT_EQ(2, cpuActs);
        ASSERT_TRUE(cpu.owned == Qt::NoButton);
    }
}

// Adversary r1 pass 2 A3: a gesture begun on the viewer keeps its events when another button is pressed on the bar
// mid-gesture: the right release still reaches the viewer (its context menu opens on release), and the bar's own left
// press/release pair stays balanced when the bar did take it.
TEST(FullscreenControlBarPolicy, ForeignButtonGestureKeepsItsRelease)
{
    fullscreen_bar::InputState s;
    fullscreen_bar::Action a = fullscreen_bar::Action::None;
    const Qt::MouseButtons both = Qt::LeftButton | Qt::RightButton;
    // Right press on the viewer, a right-held move onto the timecode cell.
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::RightButton, Qt::RightButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseMove, Qt::NoButton, Qt::RightButton, fullscreen_bar::Part::Timecode, &a));
    // A left press there while Right is still held belongs to the viewer's gesture.
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, both, fullscreen_bar::Part::Timecode, &a));
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseMove, Qt::NoButton, both, fullscreen_bar::Part::None, &a));
    // The right release (Left still held) is the viewer's.
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonRelease, Qt::RightButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None, &a));

    // The other order: the bar took a left press on Play; a right press elsewhere is the viewer's, and each release
    // goes back to whoever took its press.
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay), int(a));
    // A right press elsewhere mid-click is the viewer's and leaves the bar's left ownership alone.
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::RightButton, both, fullscreen_bar::Part::None, &a));
    ASSERT_TRUE(s.owned == Qt::MouseButtons(Qt::LeftButton));
    // Right released, Left still held: the viewer's. Left released, Right still held: the bar's.
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseButtonRelease, Qt::RightButton, Qt::LeftButton, fullscreen_bar::Part::None));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::RightButton, fullscreen_bar::Part::None));
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonRelease, Qt::RightButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::Play, &a));
    ASSERT_TRUE(s.owned == Qt::NoButton);
}

// Adversary r1 pass 3 A3 (route only, so it builds against 3b6f3c16 for the RED proof). Shape 1: the bar took a left
// press on Play; the viewer took a right press; both held, the pointer moves over Play; the left release is lost (the
// grab broke) with no move after it; then a left press on Play arrives with Left|Right held and the stale Left still
// owned. The right gesture is the viewer's, so that press is too: no TogglePlay, and the stale Left is settled.
// Shape 2: a wheel on the shown bar while a button the bar did not take is held belongs to that gesture.
TEST(FullscreenControlBarPolicy, StaleOwnershipNeverClaimsDuringAForeignGesture)
{
    fullscreen_bar::InputState s;
    fullscreen_bar::Action a = fullscreen_bar::Action::None;
    const Qt::MouseButtons both = Qt::LeftButton | Qt::RightButton;
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay), int(a));
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::RightButton, both, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseMove, Qt::NoButton, both, fullscreen_bar::Part::Play, &a));
    // (The left release is lost here.) The re-press of Left with Right held:
    ASSERT_FALSE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, both, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::None), int(a));
    ASSERT_TRUE(s.owned == Qt::NoButton);
    // The same with a double-click in place of the re-press.
    fullscreen_bar::InputState d;
    ASSERT_TRUE(fullscreen_bar::route(&d, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Next, &a));
    ASSERT_FALSE(fullscreen_bar::route(&d, true, QEvent::MouseButtonPress, Qt::RightButton, both, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(fullscreen_bar::route(&d, true, QEvent::MouseButtonDblClick, Qt::LeftButton, both, fullscreen_bar::Part::Next, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::None), int(a));
    ASSERT_TRUE(d.owned == Qt::NoButton);
    // Every other held button the bar's own: still taken (the bar took Right on Play, then Left on Play).
    fullscreen_bar::InputState o;
    ASSERT_TRUE(fullscreen_bar::route(&o, true, QEvent::MouseButtonPress, Qt::RightButton, Qt::RightButton, fullscreen_bar::Part::Play, &a));
    ASSERT_TRUE(fullscreen_bar::route(&o, true, QEvent::MouseButtonPress, Qt::LeftButton, both, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay), int(a));
    // A stale owned Left re-pressed alone (its release lost, nothing else held) is the bar's again on its own merits.
    ASSERT_TRUE(fullscreen_bar::route(&o, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay), int(a));

    // Shape 2: a wheel inside the shown bar with a foreign Right held is the viewer's; with nothing held, or only a
    // button the bar took, it is the bar's.
    fullscreen_bar::InputState w;
    ASSERT_FALSE(fullscreen_bar::route(&w, true, QEvent::Wheel, Qt::NoButton, Qt::RightButton, fullscreen_bar::Part::Play, &a));
    ASSERT_FALSE(fullscreen_bar::route(&w, true, QEvent::Wheel, Qt::NoButton, both, fullscreen_bar::Part::Timecode, &a));
    ASSERT_TRUE(fullscreen_bar::route(&w, true, QEvent::Wheel, Qt::NoButton, Qt::NoButton, fullscreen_bar::Part::Play, &a));
    ASSERT_TRUE(fullscreen_bar::route(&w, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Seek, &a));
    ASSERT_TRUE(fullscreen_bar::route(&w, true, QEvent::Wheel, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::Seek, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::None), int(a));
}

// Adversary r1 pass 3 A4 (advisory): buttons are the bar's height tall, and square only on a surface at least 4x the
// bar height wide; narrower, each is a quarter of the surface wide.
TEST(FullscreenControlBarPolicy, ButtonsAreSquareOnlyOnASurfaceFourBarHeightsWide)
{
    const fullscreen_bar::Layout wide = fullscreen_bar::layoutFor(QSize(1920, 1080));
    ASSERT_EQ(60, wide.bar.height());
    for (const QRect &b : { wide.play, wide.prev, wide.next, wide.exit }) ASSERT_TRUE(b.size() == QSize(60, 60));
    const fullscreen_bar::Layout exact = fullscreen_bar::layoutFor(QSize(240, 1080));
    ASSERT_TRUE(exact.play.size() == QSize(60, 60));
    const fullscreen_bar::Layout narrow = fullscreen_bar::layoutFor(QSize(200, 1080));
    for (const QRect &b : { narrow.play, narrow.prev, narrow.next, narrow.exit }) ASSERT_TRUE(b.size() == QSize(50, 60));
}

// Adversary r1 pass 1 A3: a seek column narrower than the marker (a tall narrow surface) and a surface shorter than
// the minimum track/marker height. Every accepted layout keeps the track and the playhead inside the seek column and
// the bar, at every frame.
TEST(FullscreenControlBarPolicy, DegenerateLayoutsKeepTrackAndPlayheadInsideTheBar)
{
    const QSize sizes[] = { QSize(880, 2160), QSize(1920, 1), QSize(1920, 3), QSize(500, 2160), QSize(1920, 1080),
                            QSize(13, 7), QSize(4000, 2) };
    for (const QSize &size : sizes)
    {
        const fullscreen_bar::Layout l = fullscreen_bar::layoutFor(size);
        ASSERT_FALSE(l.bar.isEmpty());
        ASSERT_TRUE(QRect(QPoint(0, 0), size).contains(l.bar));
        if (l.seek.isEmpty()) continue;
        ASSERT_TRUE(l.bar.contains(l.seek));
        ASSERT_TRUE(l.seek.contains(l.track));
        for (int frame : { 0, 1, 299, 599 })
        {
            const QRect head = fullscreen_bar::playheadRect(l, frame, 600);
            ASSERT_FALSE(head.isEmpty());
            ASSERT_TRUE(l.seek.contains(head));
        }
    }
    // The case the adversary traced: seek left=380 width=3 at 880x2160.
    const fullscreen_bar::Layout tall = fullscreen_bar::layoutFor(QSize(880, 2160));
    ASSERT_EQ(3, tall.seek.width());
    ASSERT_EQ(3, fullscreen_bar::playheadRect(tall, 0, 600).width());
}
