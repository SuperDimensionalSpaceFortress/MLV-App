#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackQualityPolicy.h"
#include "../../platform/qt/PlaybackSwapInterval.h"
#include "../../platform/qt/ReceiptSettings.h"

#include <QByteArray>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QSettings>

#include <array>

namespace
{

const std::array<const char *, 6> kPlaybackEnvironment = {
    "MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW",
    "MLVAPP_PLAYBACK_PHASE3_UNATTENDED",
    "MLVAPP_PLAYBACK_PREFER_HQ_MEAN23",
    "MLVAPP_PLAYBACK_PREVIEW_MODE",
    "MLVAPP_PLAYBACK_QUALITY_MODE",
    "MLVAPP_PLAYBACK_SCALE_FACTOR",
};

const std::array<const char *, 9> kPlaybackKeys = {
    PlaybackQualitySettings::kKeyQualityMode(),
    PlaybackQualitySettings::kKeyPreviewMode(),
    PlaybackQualitySettings::kKeyScaleFactorOverride(),
    PlaybackQualitySettings::kKeyPreviewResolution(),
    PlaybackQualitySettings::kKeyAutoTargetFps(),
    PlaybackQualitySettings::kKeyShowQualityIndicator(),
    PlaybackQualitySettings::kKeyShowExperimentalPhase3Modes(),
    PlaybackQualitySettings::kKeyPhase3Acknowledged(),
    PlaybackSwapIntervalSettings::kKeyVSync(),
};

class ScopedFreshPlaybackState
{
public:
    ScopedFreshPlaybackState()
    {
        for (std::size_t index = 0; index < kPlaybackEnvironment.size(); ++index) {
            const QByteArray name(kPlaybackEnvironment[index]);
            m_wasSet[index] = qEnvironmentVariableIsSet(name.constData());
            m_values[index] = qgetenv(name.constData());
            qunsetenv(name.constData());
        }
        clearSettings();
    }

    ~ScopedFreshPlaybackState()
    {
        clearSettings();
        for (std::size_t index = 0; index < kPlaybackEnvironment.size(); ++index) {
            const QByteArray name(kPlaybackEnvironment[index]);
            if (m_wasSet[index]) {
                qputenv(name.constData(), m_values[index]);
            } else {
                qunsetenv(name.constData());
            }
        }
    }

private:
    static void clearSettings()
    {
        QSettings settings(QSettings::UserScope,
                           PlaybackQualitySettings::kOrganization(),
                           PlaybackQualitySettings::kApplication());
        for (const char * key : kPlaybackKeys) {
            settings.remove(QString::fromLatin1(key));
        }
        settings.sync();
    }

    std::array<bool, kPlaybackEnvironment.size()> m_wasSet{};
    std::array<QByteArray, kPlaybackEnvironment.size()> m_values{};
};

QJsonObject loadManifest()
{
    const QString path = repo_file_path(QStringLiteral("tools/gates/shipping-defaults.json"));
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly));
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &error);
    ASSERT_EQ(static_cast<int>(QJsonParseError::NoError), static_cast<int>(error.error));
    ASSERT_TRUE(document.isObject());
    return document.object();
}

QJsonObject enumValue(const char * name, int value)
{
    QJsonObject object;
    object.insert(QStringLiteral("name"), QString::fromLatin1(name));
    object.insert(QStringLiteral("value"), value);
    return object;
}

QJsonObject actualPlaybackDefaults()
{
    QJsonObject scale;
    scale.insert(QStringLiteral("nonDualIso"),
                 playbackQualityScaleFactorForMode(PlaybackQualityMode::HighQuality, false));
    scale.insert(QStringLiteral("dualIso"),
                 playbackQualityScaleFactorForMode(PlaybackQualityMode::HighQuality, true));

    QJsonObject derived;
    derived.insert(QStringLiteral("proxyLevel"),
                   playbackPreviewResolutionToProxyLevel(playbackPreviewResolutionFromSettings()));
    derived.insert(QStringLiteral("initialScaleRequest"), scale);
    derived.insert(QStringLiteral("preferHqMean23"),
                   playbackQualityWantsHqMean23(PlaybackQualityMode::HighQuality));

    QJsonObject playback;
    playback.insert(QStringLiteral("qualityMode"),
                    enumValue("high_quality", static_cast<int>(playbackQualityModeFromSettings())));
    playback.insert(QStringLiteral("previewMode"),
                    enumValue("sharp_smooth", static_cast<int>(playbackPreviewModeFromSettings())));
    playback.insert(QStringLiteral("scaleFactorOverride"),
                    enumValue("auto", PlaybackQualitySettings::kDefaultScaleFactorOverride()));
    playback.insert(QStringLiteral("previewResolution"),
                    enumValue("auto", static_cast<int>(playbackPreviewResolutionFromSettings())));
    playback.insert(QStringLiteral("autoTargetFps"), playbackQualityAutoTargetFpsFromSettings());
    playback.insert(QStringLiteral("showQualityIndicator"), playbackQualityShowIndicatorFromSettings());
    playback.insert(QStringLiteral("showExperimentalPhase3Modes"),
                    playbackQualityShowExperimentalPhase3ModesFromSettings());
    playback.insert(QStringLiteral("phase3Acknowledged"),
                    playbackQualityPhase3AcknowledgedFromSettings());
    playback.insert(QStringLiteral("vsync"), playbackVSyncFromSettings());
    playback.insert(QStringLiteral("derived"), derived);
    return playback;
}

QJsonObject actualReceiptDefaults()
{
    ReceiptSettings receipt;
    QJsonObject actual;
#define ADD_INT(member, getter) actual.insert(QStringLiteral(member), static_cast<int>(receipt.getter()))
#define ADD_BOOL(member, getter) actual.insert(QStringLiteral(member), static_cast<bool>(receipt.getter()))
#define ADD_DOUBLE(member, getter) actual.insert(QStringLiteral(member), static_cast<double>(receipt.getter()))
#define ADD_STRING(member, getter) actual.insert(QStringLiteral(member), receipt.getter())

    ADD_INT("m_exposure", exposure); ADD_INT("m_contrast", contrast);
    ADD_INT("m_pivot", pivot); ADD_INT("m_temperature", temperature);
    ADD_INT("m_tint", tint); ADD_INT("m_clarity", clarity);
    ADD_INT("m_vibrance", vibrance); ADD_INT("m_saturation", saturation);
    ADD_INT("m_ds", ds); ADD_INT("m_dr", dr); ADD_INT("m_ls", ls); ADD_INT("m_lr", lr);
    ADD_INT("m_lightening", lightening); ADD_INT("m_shadows", shadows);
    ADD_INT("m_highlights", highlights);
    ADD_STRING("m_gradationCurve", gradationCurve); ADD_STRING("m_hueVsHue", hueVsHue);
    ADD_STRING("m_hueVsSat", hueVsSaturation); ADD_STRING("m_hueVsLuma", hueVsLuminance);
    ADD_STRING("m_lumaVsSat", lumaVsSaturation);
    ADD_BOOL("m_isGradientEnabled", isGradientEnabled);
    ADD_INT("m_gradientExposure", gradientExposure); ADD_INT("m_gradientContrast", gradientContrast);
    ADD_INT("m_gradientX1", gradientStartX); ADD_INT("m_gradientY1", gradientStartY);
    ADD_INT("m_gradientLength", gradientLength); ADD_INT("m_gradientAngle", gradientAngle);
    ADD_INT("m_sharpen", sharpen); ADD_INT("m_shMasking", shMasking);
    ADD_INT("m_chromaBlur", chromaBlur); ADD_INT("m_denoiserWindow", denoiserWindow);
    ADD_INT("m_denoiserStrength", denoiserStrength); ADD_INT("m_rbfDenoiserLuma", rbfDenoiserLuma);
    ADD_INT("m_rbfDenoiserChroma", rbfDenoiserChroma); ADD_INT("m_rbfDenoiserRange", rbfDenoiserRange);
    ADD_INT("m_grainStrength", grainStrength); ADD_INT("m_grainLumaWeight", grainLumaWeight);
    ADD_BOOL("m_highlightReconstruction", isHighlightReconstruction);
    ADD_INT("m_useCamMatrix", camMatrixUsed); ADD_BOOL("m_chromaSeparation", isChromaSeparation);
    ADD_BOOL("m_rawFixesEnabled", rawFixesEnabled); ADD_INT("m_vertical_stripes", verticalStripes);
    ADD_INT("m_focus_pixels", focusPixels); ADD_INT("m_fpi_method", fpiMethod);
    ADD_INT("m_bad_pixels", badPixels); ADD_INT("m_bps_method", bpsMethod);
    ADD_INT("m_bpi_method", bpiMethod); ADD_INT("m_chroma_smooth", chromaSmooth);
    ADD_INT("m_pattern_noise", patternNoise); ADD_INT("m_deflicker_target", deflickerTarget);
    ADD_INT("m_dualIsoForced", dualIsoForced); ADD_INT("m_dualIsoOn", dualIso);
    ADD_INT("m_dualIsoAutoCorrected", dualIsoAutoCorrected);
    ADD_INT("m_dualIsoPattern", dualIsoPattern); ADD_INT("m_dualIsoEvCorrection", dualIsoEvCorrection);
    ADD_INT("m_dualIsoBlackDelta", dualIsoBlackDelta);
    ADD_INT("m_dualIsoInt", dualIsoInterpolation); ADD_INT("m_dualIsoAliasMap", dualIsoAliasMap);
    ADD_INT("m_dualIsoFrBlending", dualIsoFrBlending); ADD_INT("m_dualIsoWhite", dualIsoWhite);
    ADD_INT("m_dualIsoBlack", dualIsoBlack); ADD_INT("m_darkFrameSubtractionMode", darkFrameEnabled);
    ADD_STRING("m_darkFrameSubtractionName", darkFrameFileName);
    ADD_DOUBLE("m_stretchFactorX", stretchFactorX); ADD_DOUBLE("m_stretchFactorY", stretchFactorY);
    ADD_BOOL("m_upsideDown", upsideDown); ADD_BOOL("m_vidstabEnable", vidStabEnabled);
    ADD_INT("m_vidstabZoom", vidStabZoom); ADD_INT("m_vidstabSmoothing", vidStabSmoothing);
    ADD_INT("m_vidstabStepsize", vidStabStepsize); ADD_INT("m_vidstabShakiness", vidStabShakiness);
    ADD_INT("m_vidstabAccuracy", vidStabAccuracy); ADD_BOOL("m_vidstabTripod", vidStabTripod);
    ADD_BOOL("m_lutEnabled", lutEnabled); ADD_STRING("m_lutName", lutName);
    ADD_INT("m_lutStrength", lutStrength); ADD_BOOL("m_filterEnabled", filterEnabled);
    ADD_INT("m_filterIndex", filterIndex); ADD_INT("m_filterStrength", filterStrength);
    ADD_INT("m_vignetteStrength", vignetteStrength); ADD_INT("m_vignetteRadius", vignetteRadius);
    ADD_INT("m_vignetteShape", vignetteShape); ADD_INT("m_caRed", caRed);
    ADD_INT("m_caBlue", caBlue); ADD_INT("m_caDesaturate", caDesaturate); ADD_INT("m_caRadius", caRadius);
    ADD_INT("m_profile", profile); ADD_INT("m_tonemap", tonemap);
    ADD_STRING("m_transferFunction", transferFunction); ADD_INT("m_gamut", gamut);
    ADD_INT("m_gamma", gamma); ADD_BOOL("m_creativeAdjustments", allowCreativeAdjustments);
    ADD_BOOL("m_exrMode", exrMode); ADD_BOOL("m_agx", agx);
    ADD_INT("m_rawWhite", rawWhite); ADD_INT("m_rawBlack", rawBlack);
    ADD_BOOL("m_lookAssistEnabled", lookAssistEnabled);
    ADD_STRING("m_lookAssistFlavor", lookAssistFlavor);
    ADD_BOOL("m_lookAssistBaselineValid", lookAssistBaselineValid);
    ADD_INT("m_lookAssistBaselineExposure", lookAssistBaselineExposure);
    ADD_INT("m_lookAssistBaselineContrast", lookAssistBaselineContrast);
    ADD_INT("m_lookAssistBaselinePivot", lookAssistBaselinePivot);
    ADD_INT("m_lookAssistBaselineTemperature", lookAssistBaselineTemperature);
    ADD_INT("m_lookAssistBaselineTint", lookAssistBaselineTint);
    ADD_INT("m_lookAssistBaselineVibrance", lookAssistBaselineVibrance);
    ADD_INT("m_lookAssistBaselineShadows", lookAssistBaselineShadows);
    ADD_INT("m_lookAssistBaselineHighlights", lookAssistBaselineHighlights);
    ADD_INT("m_lookAssistBaselineRawBlack", lookAssistBaselineRawBlack);
    ADD_INT("m_lookAssistBaselineRawWhite", lookAssistBaselineRawWhite);
    ADD_INT("m_lookAssistBaselineChromaSmooth", lookAssistBaselineChromaSmooth);
    ADD_DOUBLE("m_lookAssistBaselineStretchX", lookAssistBaselineStretchX);
    ADD_DOUBLE("m_lookAssistBaselineStretchY", lookAssistBaselineStretchY);
    ADD_STRING("m_lookAssistBaselineGradationCurve", lookAssistBaselineGradationCurve);
    ADD_INT("m_tone", tone); ADD_INT("m_toningStrength", toningStrength);
    ADD_INT("m_cutIn", cutIn); ADD_INT("m_cutOut", cutOut);
    actual.insert(QStringLiteral("m_debayer"), enumValue("amaze", static_cast<int>(receipt.debayer())));

#undef ADD_STRING
#undef ADD_DOUBLE
#undef ADD_BOOL
#undef ADD_INT
    return actual;
}

} // namespace

TEST(ShippingDefaults, ManifestMatchesRuntimeDefaults)
{
    ScopedFreshPlaybackState freshState;
    const QJsonObject root = loadManifest();
    ASSERT_EQ(std::string("mlvapp.shipping-defaults.v1"),
              root.value(QStringLiteral("schema")).toString().toStdString());
    ASSERT_TRUE(actualPlaybackDefaults() == root.value(QStringLiteral("playback")).toObject());
    const QJsonObject processing = root.value(QStringLiteral("processing")).toObject();
    ASSERT_FALSE(processing.value(QStringLiteral("useDefaultReceipt")).toBool(true));
    ASSERT_TRUE(actualReceiptDefaults() == processing.value(QStringLiteral("freshReceipt")).toObject());
}
