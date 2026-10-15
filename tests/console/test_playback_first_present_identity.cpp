// PLAYBACK-STARTUP-WAIT-PREWARM-1: the first-present identity check (PlaybackFirstPresentIdentity.h) -- the first Play-present is
// the paused start frame (not blank, mean absolute difference <= 6.0, display frame start or start + 1).
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackFirstPresentIdentity.h"

#include <QColor>
#include <QImage>

namespace id = playback_first_present_identity;

namespace
{

QImage gradient(int w, int h, int offset)
{
    QImage image(w, h, QImage::Format_RGB32);
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x)
            image.setPixel(x, y, qRgb(qBound(0, x * 8 + offset, 255), qBound(0, y * 8 + offset, 255), qBound(0, 100 + offset, 255)));
    return image;
}

} // namespace

TEST(PlaybackFirstPresentIdentity, TheSameFramePasses)
{
    const QImage paused = gradient(16, 8, 0);
    const id::Stats s = id::compare(paused, paused, 120, 120);
    ASSERT_TRUE(s.comparable);
    ASSERT_NEAR(0.0, s.meanAbsDiff, 1e-9);
    ASSERT_TRUE(s.firstStd >= id::kMinStd);
    ASSERT_TRUE(s.pass);
    ASSERT_TRUE(id::compare(paused, paused, 120, 121).pass); // the early advance may already show start + 1
}

TEST(PlaybackFirstPresentIdentity, ARenderPathDifferenceWithinSixPasses)
{
    const id::Stats s = id::compare(gradient(16, 8, 0), gradient(16, 8, 5), 0, 0);
    ASSERT_TRUE(s.meanAbsDiff > 0.0 && s.meanAbsDiff <= id::kMaxMeanAbsDiff);
    ASSERT_TRUE(s.pass);
    ASSERT_FALSE(id::compare(gradient(16, 8, 0), gradient(16, 8, 40), 0, 0).pass);
}

TEST(PlaybackFirstPresentIdentity, AnUnfilledTextureFails)
{
    QImage black(16, 8, QImage::Format_RGB32);
    black.fill(Qt::black);
    const id::Stats s = id::compare(black, black, 0, 0); // identical, but blank: an unfilled texture never passes
    ASSERT_NEAR(0.0, s.firstStd, 1e-9);
    ASSERT_FALSE(s.pass);
}

TEST(PlaybackFirstPresentIdentity, AnotherFrameOrAMissingGrabFails)
{
    const QImage paused = gradient(16, 8, 0);
    ASSERT_FALSE(id::compare(paused, paused, 120, 122).pass);
    ASSERT_FALSE(id::compare(paused, paused, 120, 119).pass);
    ASSERT_FALSE(id::compare(paused, paused, -1, 0).pass);
    const id::Stats missing = id::compare(paused, QImage(), 0, 0);
    ASSERT_FALSE(missing.comparable);
    ASSERT_FALSE(missing.pass);
    ASSERT_FALSE(id::compare(paused, gradient(8, 8, 0), 0, 0).comparable); // different sizes are not compared
}
