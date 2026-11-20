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
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseButtonDblClick, Qt::LeftButton, inside));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseButtonDblClick, Qt::LeftButton, fullscreen_bar::Part::Exit));
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseButtonDblClick, Qt::LeftButton, fullscreen_bar::hitTest(l, above)));
    ASSERT_FALSE(fullscreen_bar::claims(s, false, QEvent::MouseButtonDblClick, Qt::LeftButton, inside));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::Wheel, Qt::NoButton, inside));
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseMove, Qt::NoButton, inside));
    // A viewer drag crossing the bar keeps its moves and its release.
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseMove, Qt::LeftButton, inside));
    ASSERT_FALSE(fullscreen_bar::claims(s, true, QEvent::MouseButtonRelease, Qt::NoButton, inside));

    // A double-click on Exit exits once (the first press already did; Qt sends the second press as the double-click).
    ASSERT_EQ(int(fullscreen_bar::Action::ExitFullscreen),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Exit)));
    ASSERT_TRUE(s.pressOwned);
    // Its release is the bar's wherever it lands.
    ASSERT_TRUE(fullscreen_bar::claims(s, false, QEvent::MouseButtonRelease, Qt::NoButton, fullscreen_bar::Part::None));
    ASSERT_EQ(int(fullscreen_bar::Action::None),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None)));
    ASSERT_FALSE(s.pressOwned);
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
    ASSERT_TRUE(fullscreen_bar::claims(s, true, QEvent::MouseMove, Qt::LeftButton, fullscreen_bar::Part::None));
    ASSERT_EQ(int(fullscreen_bar::Action::Seek),
              int(fullscreen_bar::onMouse(&s, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None)));
    fullscreen_bar::onMouse(&s, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None);
    ASSERT_FALSE(s.seekDrag);
    ASSERT_FALSE(s.pressOwned);
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
    ASSERT_FALSE(s.pressOwned);
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None, &a));

    // A press on Play, then the release is lost: the next buttonless move settles it, and a later viewer drag is kept.
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Play, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::TogglePlay), int(a));
    ASSERT_TRUE(s.pressOwned);
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseMove, Qt::NoButton, Qt::NoButton, fullscreen_bar::Part::None, &a));
    ASSERT_FALSE(s.pressOwned);
    ASSERT_FALSE(fullscreen_bar::route(&s, false, QEvent::MouseMove, Qt::NoButton, Qt::LeftButton, fullscreen_bar::Part::None, &a));

    // The Exit press keeps its own release even though the bar is hidden by then.
    ASSERT_TRUE(fullscreen_bar::route(&s, true, QEvent::MouseButtonPress, Qt::LeftButton, Qt::LeftButton, fullscreen_bar::Part::Exit, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::ExitFullscreen), int(a));
    ASSERT_TRUE(fullscreen_bar::route(&s, false, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoButton, fullscreen_bar::Part::None, &a));
    ASSERT_EQ(int(fullscreen_bar::Action::None), int(a));
    ASSERT_FALSE(s.pressOwned);
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
