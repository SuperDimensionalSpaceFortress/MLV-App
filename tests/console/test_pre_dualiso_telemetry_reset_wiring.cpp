// Wiring test: pins that RenderFrameThread::drawFrame clears the thread-local pre-dual-ISO fix telemetry
// (llrpResetLastPreDualIsoFixTelemetry: the completed flag and its elapsed-ms twin) exactly once per frame, BEFORE
// any render path is chosen, and reads it (preDualIsoFixActive / llrawproc_pre_dualiso_fix_ms) only AFTER every
// render call. PROD-TELEMETRY-DURATION-AS-PROOF-3b round 3 (both formal keys' blocker): the flag was reset only inside
// the CPU render entries, so a frame that never entered one -- getMlvProcessedFrame8ScaledFromReconnedRaw16 (no
// llrawproc call) or the CUDA texture no-readback skip (no CPU render at all) -- read the previous CPU render's
// "fix ran" on the same thread. The consumer-side reset makes that impossible for every present and future path.
// RenderFrameThread.cpp needs the full GUI build (not linked into console_tests), so this test reads the source as
// text and pins the ordering by position (mirrors test_playback_display_wake_wiring.cpp).
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

int countOccurrences(const QString & haystack, const QString & needle)
{
    int count = 0;
    int from = 0;
    while (true) {
        const int at = haystack.indexOf(needle, from);
        if (at < 0) break;
        ++count;
        from = at + needle.length();
    }
    return count;
}

// drawFrame is the last member function in RenderFrameThread.cpp, so its body runs to the end of the file.
QString drawFrameBody()
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const int at = source.indexOf(QStringLiteral("void RenderFrameThread::drawFrame( int slotIndex,"));
    ASSERT_TRUE(at >= 0);
    return source.mid(at);
}

const QString kReset = QStringLiteral("llrpResetLastPreDualIsoFixTelemetry();");

} // namespace

TEST(PreDualIsoTelemetryResetWiring, DrawFrameResetsExactlyOncePerFrame)
{
    const QString body = drawFrameBody();
    ASSERT_EQ(1, countOccurrences(body, kReset));
}

TEST(PreDualIsoTelemetryResetWiring, ResetPrecedesEveryRenderPathIncludingTheGpuNoReadbackSkip)
{
    const QString body = drawFrameBody();
    const int resetAt = body.indexOf(kReset);
    ASSERT_TRUE(resetAt >= 0);

    const QStringList renderSites = {
        // GPU texture no-readback skip branch (renders nothing on the CPU).
        QStringLiteral("if( skipCpuDebayerForGpuTextureNoReadback )"),
        // CPU render entries, all output modes.
        QStringLiteral("getMlvProcessedFrame16Scaled("),
        QStringLiteral("getMlvRawFrameFloat("),
        QStringLiteral("getMlvRawFrameDebayered("),
        QStringLiteral("getMlvProcessedFrame8ScaledFromReconnedRaw16("),
        QStringLiteral("getMlvProcessedFrame8ScaledFromRaw16("),
        QStringLiteral("getMlvProcessedFrame8Scaled(") };
    for (const QString & site : renderSites) {
        const int siteAt = body.indexOf(site);
        ASSERT_TRUE(siteAt >= 0);
        ASSERT_TRUE(resetAt < siteAt);
    }
}

TEST(PreDualIsoTelemetryResetWiring, TelemetryIsReadOnlyAfterEveryRenderCall)
{
    const QString body = drawFrameBody();
    const int readAt = body.indexOf(QStringLiteral("llrpGetLastPreDualIsoFixCompleted()"));
    ASSERT_TRUE(readAt >= 0);
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("llrpGetLastPreDualIsoFixCompleted()")));
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("llrpGetLastPreDualIsoFixMilliseconds()")));

    const QStringList renderCalls = {
        QStringLiteral("getMlvProcessedFrame16Scaled("),
        QStringLiteral("getMlvProcessedFrame8ScaledFromReconnedRaw16("),
        QStringLiteral("getMlvProcessedFrame8ScaledFromRaw16("),
        QStringLiteral("getMlvProcessedFrame8Scaled(") };
    for (const QString & call : renderCalls) {
        int from = 0;
        while (true) {
            const int callAt = body.indexOf(call, from);
            if (callAt < 0) break;
            ASSERT_TRUE(callAt < readAt);
            from = callAt + call.length();
        }
    }
    // The elapsed-ms twin is read beside the flag, after the same renders, never before the reset.
    ASSERT_TRUE(body.indexOf(kReset) < body.indexOf(QStringLiteral("llrpGetLastPreDualIsoFixMilliseconds()")));
}
