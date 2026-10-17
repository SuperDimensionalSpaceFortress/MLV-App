// GPU-LAUNCH-BUDGET-TILING-1: the per-launch budget guard (owner spec event153 r2, Part 2, and
// the Part 4 sentence "no contact-sheet-path launch exceeds the budget at the M16-1456 frame
// size (1920x804)"). Pins (1) the budget, 100 ms per launch; (2) the verdict over the measured
// M16-1456 fixture: no GPU kernel over budget, and the two host waits over it (recon's blocking
// constant uploads) listed, never counted as launches; (3) the AMaZE tile kernels' grids are
// built from kTileSize, so they do not grow with the frame; (4) every whole-frame launch is on an
// explicit list, and its measured 1920x804 time scaled to the largest budgeted frame (8192x4320)
// stays under budget. The .cu files need nvcc, which console_tests does not have, so (3) and (4)
// read them as text, like test_cuda_launch_timing.cpp.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../tools/gpu/backend/igpu_launch_budget.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <cstdio>
#include <iostream>
#include <set>
#include <string>
#include <vector>

namespace
{

const char * const kFixture = "tests/console/fixtures/gpu_launch_budget/m16-1456-1920x804.txt";
const char * const kRecon = "tools/gpu/backend/igpu_recon_cuda.cu";
const char * const kAmaze = "tools/gpu/backend/igpu_amaze_debayer_cuda.cu";
const char * const kStageProbe = "tools/gpu/cuda_amaze_debayer_stage_probe.cu";
const char * const kRunTile = "void run_tile(const float * dRaw,";
const char * const kRunTilePostWb = "void run_tile_bayer16_post_wb(const uint16_t * dBayer16,";
// The stage-probe launch helpers the AMaZE DLL compiles in; run_tile* call each of them.
const char * const kProbeHelpersFrom = "void launch_variance_selection_prefix(";
const char * const kProbeHelpersTo = "void allocate_device(";

// Every launch whose grid is NOT built from kTileSize: its work grows with the frame. Adding a
// launch outside run_tile / run_tile_bayer16_post_wb means adding it here, with a budget
// justification. The justification for a stage the fixture measures is checked by
// WholeFrameScaledBoundAtMaxFrame (its 1920x804 max, scaled by pixel count to 8192x4320, stays
// <= 100 ms). A stage the M16-1456 path does not run has no measurement yet; its bound is
// successor card GPU-LAUNCH-BUDGET-MAXFRAME-1 (measured max-frame timings on Bachelor).
struct WholeFrameLaunch
{
    const char * dll;
    const char * stage;
};
const WholeFrameLaunch kWholeFrameLaunches[] = {
    {"recon", "k_promote_match"},
    {"recon", "k_mean23"},
    {"recon", "k_border_toprows"},   // grid spans W x 3 rows: bounded well below the w*h scaling
    {"recon", "k_border_botrows"},   // W x 4 rows
    {"recon", "k_border_cols"},      // H rows
    {"recon", "k_chroma_smooth_2x2:bright"},
    {"recon", "k_chroma_smooth_2x2:dark"},
    {"recon", "k_fullres_phase_balance"},
    {"recon", "k_fullres"},
    {"recon", "k_mix_halfres_avx2"},
    {"recon", "k_mix_halfres"},
    {"recon", "k_fullres:smooth"},
    {"recon", "k_alias_init"},
    {"recon", "k_alias_rank"},
    {"recon", "k_alias_gauss"},
    {"recon", "k_alias_gray"},
    {"recon", "k_over_mark"},
    {"recon", "k_over_bordercopy"},
    {"recon", "k_over_blur"},
    {"recon", "k_final_blend_mesh_guard"},
    {"recon", "k_output_vertical_mesh_stabilize"},
    {"recon", "k_source_mesh_stabilize"},
    {"recon", "k_flat_green_cell_balance"},
    {"recon", "k_flat_chroma_guard"},
    {"recon", "k_convert16"},
    {"recon", "k_final_blend16_avx2"},
    {"recon", "k_final_blend"},
    {"recon", "k_reduced_iso_period_notch"},
    {"amaze", "k_reduced_hnyquist121"},
    {"amaze", "k_pack_rgb16_to_rgba16"},
    {"amaze", "k_pack_rgb16_to_rgba16_post_wb_undo"},
    // PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1: outside the tile bodies but NOT frame-sized: a <<<1, 1>>> spin of
    // kProbeSpinNs (50 us), and the clock kick's <<<1, 32>>> chunk, each launch bounded by kClockKickChunkMs (20 ms),
    // whatever the frame. Neither runs on the measured M16-1456 fixture path (armed / Play-start only).
    {"amaze", "k_sm_clock_probe"},
    {"amaze", "k_clock_kick"},
};

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

std::string fixtureText()
{
    return readRepoFile(QString::fromLatin1(kFixture)).toStdString();
}

std::string key(const std::string & dll, const std::string & stage)
{
    return dll + "/" + stage;
}

// The body of the function whose definition starts with `signature`, up to its closing "\n}".
QString functionBody(const QString & source, const QString & signature)
{
    const int start = source.indexOf(signature);
    ASSERT_TRUE(start >= 0);
    ASSERT_EQ(-1, source.indexOf(signature, start + 1));
    const int end = source.indexOf(QStringLiteral("\n}"), start);
    ASSERT_TRUE(end > start);
    return source.mid(start, end - start);
}

QString regionBetween(const QString & source, const QString & from, const QString & to)
{
    const int start = source.indexOf(from);
    ASSERT_TRUE(start >= 0);
    const int end = source.indexOf(to, start);
    ASSERT_TRUE(end > start);
    return source.mid(start, end - start);
}

struct LaunchSite
{
    QString stage;
    QString grid;  // the second macro argument, whitespace collapsed
};

// Every IGPU_LT_LAUNCH("stage", grid, ...) in `source`.
std::vector<LaunchSite> launchSites(const QString & source)
{
    static const QString macro = QStringLiteral("IGPU_LT_LAUNCH(");
    std::vector<LaunchSite> sites;
    for (int at = source.indexOf(macro); at >= 0; at = source.indexOf(macro, at + macro.size()))
    {
        int p = at + macro.size();
        while (p < source.size() && source.at(p).isSpace()) ++p;
        ASSERT_TRUE(p < source.size() && source.at(p) == QLatin1Char('"'));
        const int close = source.indexOf(QLatin1Char('"'), p + 1);
        ASSERT_TRUE(close > p);
        LaunchSite site;
        site.stage = source.mid(p + 1, close - p - 1);
        p = close + 1;
        while (p < source.size() && source.at(p).isSpace()) ++p;
        ASSERT_TRUE(p < source.size() && source.at(p) == QLatin1Char(','));
        ++p;
        int depth = 0;
        const int gridStart = p;
        for (; p < source.size(); ++p)
        {
            const QChar c = source.at(p);
            if (c == QLatin1Char('(')) ++depth;
            else if (c == QLatin1Char(')')) --depth;
            else if (c == QLatin1Char(',') && depth == 0) break;
        }
        ASSERT_TRUE(p < source.size());
        site.grid = source.mid(gridStart, p - gridStart).simplified();
        sites.push_back(site);
    }
    return sites;
}

// Each tile-body launch whose grid is not the kTileSize `grid` (or another allowed form).
QStringList tileGridViolations(const QString & body, const QStringList & allowedGrids)
{
    QStringList bad;
    for (const LaunchSite & s : launchSites(body))
    {
        if (s.grid == QStringLiteral("grid")) continue;
        if (allowedGrids.contains(s.grid)) continue;
        if (s.grid == QStringLiteral("1") && s.stage == QStringLiteral("k_nyquist_refine_row_prefix")) continue;
        bad << QStringLiteral("%1 grid=%2").arg(s.stage, s.grid);
    }
    return bad;
}

// The AMaZE source with the two tile bodies cut out.
QString amazeOutsideTiles(const QString & amaze)
{
    QString rest = amaze;
    for (const char * sig : {kRunTile, kRunTilePostWb})
        rest.remove(functionBody(rest, QString::fromLatin1(sig)));
    return rest;
}

// Stage names of every tile-bounded launch: the two run_tile* bodies plus the stage-probe helpers.
std::set<std::string> tileBoundedStages(const QString & amaze, const QString & probe)
{
    std::set<std::string> stages;
    QStringList regions;
    regions << functionBody(amaze, QString::fromLatin1(kRunTile))
            << functionBody(amaze, QString::fromLatin1(kRunTilePostWb))
            << regionBetween(probe, QString::fromLatin1(kProbeHelpersFrom), QString::fromLatin1(kProbeHelpersTo));
    for (const QString & region : regions)
        for (const LaunchSite & s : launchSites(region)) stages.insert(key("amaze", s.stage.toStdString()));
    return stages;
}

// The whole-frame launch set the sources have, against kWholeFrameLaunches; one line per mismatch.
QStringList wholeFrameListProblems(const QString & recon, const QString & amaze)
{
    std::set<std::string> actual;
    for (const LaunchSite & s : launchSites(recon)) actual.insert(key("recon", s.stage.toStdString()));
    for (const LaunchSite & s : launchSites(amazeOutsideTiles(amaze))) actual.insert(key("amaze", s.stage.toStdString()));
    std::set<std::string> listed;
    for (const WholeFrameLaunch & w : kWholeFrameLaunches) listed.insert(key(w.dll, w.stage));

    QStringList problems;
    for (const std::string & k : actual)
        if (!listed.count(k))
            problems << QStringLiteral("new whole-frame launch %1: its grid grows with the frame, so add it to "
                                       "kWholeFrameLaunches with a budget justification (its measured max at 1920x804, "
                                       "scaled to %2x%3, must stay <= %4 ms) or launch it per tile")
                            .arg(QString::fromStdString(k))
                            .arg(IGPU_LT_BUDGET_MAX_FRAME_W)
                            .arg(IGPU_LT_BUDGET_MAX_FRAME_H)
                            .arg(IGPU_LT_LAUNCH_BUDGET_MS);
    for (const std::string & k : listed)
        if (!actual.count(k))
            problems << QStringLiteral("stale kWholeFrameLaunches entry %1: no such launch outside the tile bodies")
                            .arg(QString::fromStdString(k));
    return problems;
}

std::string describe(const std::vector<IgpuLbStageMax> & stages)
{
    std::string out;
    for (const IgpuLbStageMax & s : stages)
    {
        char ms[32];
        std::snprintf(ms, sizeof(ms), "%.3f", s.max_ms);
        if (!out.empty()) out += "; ";
        out += s.dll + "/" + s.kind + "/" + s.stage + "=" + ms;
    }
    return out;
}

// The fixture with the max_ms of the first summary line for `stage` replaced by `ms`.
std::string withStageMs(const std::string & text, const std::string & stage, const char * ms)
{
    std::string out = text;
    const std::size_t line = out.find(" stage=" + stage + " ");
    ASSERT_TRUE(line != std::string::npos);
    const std::size_t field = out.find(" max_ms=", line);
    ASSERT_TRUE(field != std::string::npos && field < out.find('\n', line));
    const std::size_t value = field + 8;
    const std::size_t end = out.find(' ', value);
    out.replace(value, end - value, ms);
    return out;
}

} // namespace

TEST(GpuLaunchBudget, BudgetIs100Ms)
{
    ASSERT_NEAR(100.0, IGPU_LT_LAUNCH_BUDGET_MS, 0.0);
    ASSERT_EQ(8192, IGPU_LT_BUDGET_MAX_FRAME_W);
    ASSERT_EQ(4320, IGPU_LT_BUDGET_MAX_FRAME_H);
}

TEST(GpuLaunchBudget, ParserReadsBothLineFormsAndIgnoresOtherFields)
{
    // Unknown key=value fields (later recorder versions add some) and every other line are
    // skipped; a line with no duration (an error) is a record with ms < 0 and is not judged.
    const std::string text =
        "igpu_launch_timing_config dll=recon enabled=1 min_ms=2.000\n"
        "igpu_launch_timing dll=recon kind=kernel stage=k_mean23 seq=3 utc_ms=1.000 grid=60x51x1 "
        "block=16x16x1 w=1920 h=804 ms=3.414 new_field=7\r\n"
        "igpu_launch_timing_summary dll=amaze kind=sync stage=tiles:drain_sync at=periodic count=9 "
        "over_min=0 errors=0 sum_ms=40.000 p95_ms=12.000 max_ms=29.650 max_seq=4 max_utc_ms=2.000 "
        "max_grid=0x0x0 max_block=0x0x0 max_w=1920 max_h=804 warn_count=0\n"
        "igpu_launch_timing dll=recon kind=kernel stage=k_fullres seq=4 utc_ms=1.000 grid=120x51x1 "
        "block=16x16x1 w=1920 h=804 ms=na err=700 err_name=cudaErrorIllegalAddress\n"
        "igpu_launch_timing_drops dll=amaze at=destroy pending=0 dropped=1 abandoned=0 error_lines_suppressed=0\n"
        "# igpu_launch_timing dll=recon kind=kernel stage=commented w=1 h=1 ms=500.000\n"
        "igpu_launch_timing kind=kernel stage=no_dll ms=500.000\n"
        "[igpu_amaze_debayer_cuda] something else ms=500.000\n";
    const std::vector<IgpuLbRecord> records = igpu_lb_parse_text(text);
    ASSERT_EQ(3, (int)records.size());
    ASSERT_EQ(std::string("k_mean23"), records[0].stage);
    ASSERT_NEAR(3.414, records[0].ms, 1e-9);
    ASSERT_EQ(1920, records[0].width);
    ASSERT_EQ(804, records[0].height);
    ASSERT_EQ(0, records[0].summary);
    ASSERT_EQ(std::string("sync"), records[1].kind);
    ASSERT_NEAR(29.650, records[1].ms, 1e-9);  // max_ms, not p95_ms or sum_ms
    ASSERT_EQ(1, records[1].summary);
    ASSERT_TRUE(records[2].ms < 0.0);
    const IgpuLbVerdict v = igpu_lb_verdict(records, IGPU_LT_LAUNCH_BUDGET_MS);
    ASSERT_EQ(2, (int)v.stages.size());
    ASSERT_EQ(0, (int)v.violations.size());
}

TEST(GpuLaunchBudget, FixtureAt1920x804HasNoKernelOverBudget)
{
    const std::string text = fixtureText();
    const std::vector<IgpuLbRecord> records = igpu_lb_parse_text(text);
    // Every non-comment line is a record: nothing in the fixture is silently skipped.
    int dataLines = 0;
    for (const QString & line : QString::fromStdString(text).split(QLatin1Char('\n')))
        if (!line.trimmed().isEmpty() && !line.startsWith(QLatin1Char('#'))) ++dataLines;
    ASSERT_EQ(dataLines, (int)records.size());

    const IgpuLbVerdict v = igpu_lb_verdict(records, IGPU_LT_LAUNCH_BUDGET_MS);
    ASSERT_EQ(std::string(), describe(v.violations));

    std::set<std::string> dlls;
    int kernelStages = 0;
    const IgpuLbStageMax * longest = nullptr;
    for (const IgpuLbStageMax & s : v.stages)
    {
        if (s.kind != "kernel") continue;
        ++kernelStages;
        dlls.insert(s.dll);
        if (!longest || s.max_ms > longest->max_ms) longest = &s;
    }
    for (const IgpuLbRecord & r : records)
    {
        if (r.kind != "kernel") continue;
        ASSERT_EQ(1920, r.width);
        ASSERT_EQ(804, r.height);
    }
    // Non-vacuous: both DLLs, and the whole AMaZE + recon kernel set the run exercised.
    ASSERT_EQ(2, (int)dlls.size());
    ASSERT_TRUE(kernelStages >= 20);
    ASSERT_TRUE(longest != nullptr);
    ASSERT_EQ(std::string("amaze/k_fancy_chrominance_interpolation"), key(longest->dll, longest->stage));
    ASSERT_NEAR(9.605, longest->max_ms, 1e-9);
}

TEST(GpuLaunchBudget, HostWaitsAreListedNotViolations)
{
    // recon's set_clip and run each wrap 7 blocking cudaMemcpyToSymbol calls in one SyncScope:
    // host waits on the legacy stream, not GPU launches.
    const IgpuLbVerdict v = igpu_lb_verdict(igpu_lb_parse_text(fixtureText()), IGPU_LT_LAUNCH_BUDGET_MS);
    ASSERT_EQ(0, (int)v.violations.size());
    ASSERT_EQ(std::string("recon/sync/run:constants=105.976; recon/sync/set_clip:constants=175.135"),
              describe(v.host_waits));
}

TEST(GpuLaunchBudget, RedOnAnOverBudgetKernel)
{
    const std::string text = fixtureText();
    struct Case { const char * ms; int violations; };
    for (const Case c : {Case{"150.000", 1}, Case{"100.001", 1}, Case{"100.000", 0}})
    {
        const IgpuLbVerdict v = igpu_lb_verdict(igpu_lb_parse_text(withStageMs(text, "k_mean23", c.ms)),
                                                IGPU_LT_LAUNCH_BUDGET_MS);
        ASSERT_EQ(c.violations, (int)v.violations.size());
        if (c.violations)
        {
            ASSERT_EQ(std::string("recon/kernel/k_mean23=") + c.ms, describe(v.violations));
        }
    }
    // A per-launch line over budget counts the same as a summary.
    const IgpuLbVerdict line = igpu_lb_verdict(
        igpu_lb_parse_text(text + "igpu_launch_timing dll=amaze kind=kernel stage=k_gradients seq=9 utc_ms=1.000 "
                                  "grid=32x32x1 block=16x16x1 w=1920 h=804 tile=-16:-16 ms=150.000\n"),
        IGPU_LT_LAUNCH_BUDGET_MS);
    ASSERT_EQ(std::string("amaze/kernel/k_gradients=150.000"), describe(line.violations));
}

TEST(GpuLaunchBudget, TileBoundedKernelsDoNotScaleWithFrame)
{
    const QString amaze = readRepoFile(QString::fromLatin1(kAmaze));
    const QString probe = readRepoFile(QString::fromLatin1(kStageProbe));
    // The AMaZE DLL compiles the stage probe in; its tile edge is 512.
    ASSERT_TRUE(amaze.contains(QStringLiteral("#include \"../cuda_amaze_debayer_stage_probe.cu\"")));
    ASSERT_EQ(1, (int)probe.count(QStringLiteral("constexpr int kTileSize = 512;")));
    ASSERT_FALSE(amaze.contains(QRegularExpression(QStringLiteral("\\bkTileSize\\s*="))));

    static const QRegularExpression tileGrid(QStringLiteral(
        "const dim3 block\\(16, 16\\);\\s*const dim3 grid\\(\\(kTileSize \\+ block\\.x - 1\\) / block\\.x,\\s*"
        "\\(kTileSize \\+ block\\.y - 1\\) / block\\.y\\);"));
    for (const char * sig : {kRunTile, kRunTilePostWb})
    {
        const QString body = functionBody(amaze, QString::fromLatin1(sig));
        ASSERT_TRUE(body.contains(tileGrid));
        ASSERT_EQ(1, (int)body.count(QRegularExpression(QStringLiteral("\\bconst dim3 grid\\b"))));
        ASSERT_TRUE(launchSites(body).size() >= 15);  // non-vacuous
        ASSERT_EQ(std::string(), tileGridViolations(body, QStringList()).join(QStringLiteral("; ")).toStdString());
        // The probe helpers it calls get the same kTileSize grid.
        ASSERT_TRUE(body.contains(QStringLiteral("launch_variance_selection_prefix(d, rr1, cc1, stream")));
        ASSERT_TRUE(body.contains(QRegularExpression(QStringLiteral("launch_green_plane_assembly\\(grid,\\s*block,"))));
        ASSERT_TRUE(body.contains(QRegularExpression(QStringLiteral("launch_pmwt_refinement_and_rbint\\(grid,\\s*block,"))));
    }
    const QString helpers =
        regionBetween(probe, QString::fromLatin1(kProbeHelpersFrom), QString::fromLatin1(kProbeHelpersTo));
    ASSERT_TRUE(helpers.contains(tileGrid));
    ASSERT_TRUE(helpers.contains(QStringLiteral("const dim3 prefixGrid(kTileSize, 2);")));
    ASSERT_TRUE(launchSites(helpers).size() >= 9);
    // In the helpers, `1` is the single-block row-parallel kernels and prefixGrid is kTileSize x 2.
    ASSERT_EQ(std::string(),
              tileGridViolations(helpers, QStringList() << QStringLiteral("prefixGrid") << QStringLiteral("1"))
                  .join(QStringLiteral("; ")).toStdString());

    // RED on a tile launch whose grid is built from the frame.
    QString mutant = functionBody(amaze, QString::fromLatin1(kRunTile));
    const QString site = QStringLiteral("IGPU_LT_LAUNCH(\"k_gradients\", grid,");
    ASSERT_TRUE(mutant.contains(site));
    mutant.replace(site, QStringLiteral("IGPU_LT_LAUNCH(\"k_gradients\", dim3((width + 15) / 16, (height + 15) / 16),"));
    ASSERT_EQ(std::string("k_gradients grid=dim3((width + 15) / 16, (height + 15) / 16)"),
              tileGridViolations(mutant, QStringList()).join(QStringLiteral("; ")).toStdString());
}

TEST(GpuLaunchBudget, WholeFrameLaunchesAreAnExplicitList)
{
    const QString recon = readRepoFile(QString::fromLatin1(kRecon));
    const QString amaze = readRepoFile(QString::fromLatin1(kAmaze));
    ASSERT_EQ(std::string(), wholeFrameListProblems(recon, amaze).join(QStringLiteral("\n")).toStdString());
    // 3 frame-sized packs/filters + PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1's probe and kick (constant-size grids)
    ASSERT_EQ(5, (int)launchSites(amazeOutsideTiles(amaze)).size());

    // RED on a new whole-frame launch, with the message that says what to do.
    const QString added = QStringLiteral(
        "\nstatic void mutant(igpu_recon_backend* b, int W, int H) { dim3 g((W+15)/16,(H+15)/16), bt(16,16);\n"
        "    IGPU_LT_LAUNCH(\"k_new_whole_frame\", g, bt, 0, k_new_whole_frame<<<g,bt>>>(b->d_raw32,W,H)); }\n");
    const QStringList problems = wholeFrameListProblems(recon + added, amaze);
    ASSERT_EQ(1, (int)problems.size());
    ASSERT_TRUE(problems.first().startsWith(QStringLiteral("new whole-frame launch recon/k_new_whole_frame:")));
    ASSERT_TRUE(problems.first().contains(QStringLiteral("budget justification")));
    // ... and on an AMaZE launch added outside the tile bodies.
    const QStringList amazeProblems = wholeFrameListProblems(
        recon, amaze + QStringLiteral("\nvoid mutant() { IGPU_LT_LAUNCH(\"k_frame_pass\", blocks, threads, 0, k<<<1,1>>>()); }\n"));
    ASSERT_EQ(1, (int)amazeProblems.size());
    ASSERT_TRUE(amazeProblems.first().startsWith(QStringLiteral("new whole-frame launch amaze/k_frame_pass:")));
}

TEST(GpuLaunchBudget, WholeFrameScaledBoundAtMaxFrame)
{
    const QString amaze = readRepoFile(QString::fromLatin1(kAmaze));
    const QString probe = readRepoFile(QString::fromLatin1(kStageProbe));
    const std::set<std::string> tileStages = tileBoundedStages(amaze, probe);
    std::set<std::string> wholeFrame;
    for (const WholeFrameLaunch & w : kWholeFrameLaunches) wholeFrame.insert(key(w.dll, w.stage));

    const IgpuLbVerdict v = igpu_lb_verdict(igpu_lb_parse_text(fixtureText()), IGPU_LT_LAUNCH_BUDGET_MS);
    int scaledCount = 0;
    int tileCount = 0;
    std::string tightest;
    double tightestMs = -1.0;
    std::set<std::string> measured;
    for (const IgpuLbStageMax & s : v.stages)
    {
        if (s.kind != "kernel") continue;
        const std::string k = key(s.dll, s.stage);
        measured.insert(k);
        char line[256];
        if (wholeFrame.count(k))
        {
            ASSERT_TRUE(s.width > 0 && s.height > 0);
            const double scaled = igpu_lb_scaled_to_max_frame_ms(s.max_ms, s.width, s.height);
            std::snprintf(line, sizeof(line),
                          "[budget] whole-frame %-40s max %7.3f ms @%dx%d -> %8.3f ms @%dx%d, margin %8.3f ms",
                          k.c_str(), s.max_ms, s.width, s.height, scaled, IGPU_LT_BUDGET_MAX_FRAME_W,
                          IGPU_LT_BUDGET_MAX_FRAME_H, IGPU_LT_LAUNCH_BUDGET_MS - scaled);
            std::cout << line << "\n";
            ASSERT_TRUE(scaled <= IGPU_LT_LAUNCH_BUDGET_MS);
            if (scaled > tightestMs)
            {
                tightestMs = scaled;
                tightest = k;
            }
            ++scaledCount;
        }
        else
        {
            // A kernel the fixture measured is either whole-frame (listed) or tile-bounded.
            ASSERT_EQ(std::string("tile-bounded ") + k, std::string(tileStages.count(k) ? "tile-bounded " : "unclassified ") + k);
            std::snprintf(line, sizeof(line), "[budget] tile-bounded %-40s max %7.3f ms (any frame), margin %8.3f ms",
                          k.c_str(), s.max_ms, IGPU_LT_LAUNCH_BUDGET_MS - s.max_ms);
            std::cout << line << "\n";
            ASSERT_TRUE(s.max_ms <= IGPU_LT_LAUNCH_BUDGET_MS);
            ++tileCount;
        }
    }
    for (const WholeFrameLaunch & w : kWholeFrameLaunches)
        if (!measured.count(key(w.dll, w.stage)))
            std::cout << "[budget] whole-frame " << key(w.dll, w.stage)
                      << " not run on the M16-1456 path: unmeasured (GPU-LAUNCH-BUDGET-MAXFRAME-1)\n";
    ASSERT_TRUE(scaledCount >= 10);
    ASSERT_TRUE(tileCount >= 20);
    // The tightest whole-frame launch at 8192x4320: 4.172 ms x (8192*4320)/(1920*804) = 95.645 ms.
    ASSERT_EQ(std::string("recon/k_final_blend16_avx2"), tightest);
    ASSERT_NEAR(95.645, tightestMs, 0.01);
}
