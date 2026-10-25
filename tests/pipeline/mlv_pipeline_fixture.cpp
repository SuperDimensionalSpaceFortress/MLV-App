#include "mlv_pipeline_fixture.h"

#include "../common/repo_paths.h"

#include "../../src/batch/ReceiptLoader.h"
#include "../../src/batch/ReceiptApplier.h"

#include <QByteArray>
#include <pthread.h>

MlvPipelineFixture::MlvPipelineFixture()
    : m_video(nullptr)
    , m_processing(nullptr)
{
}

MlvPipelineFixture::~MlvPipelineFixture()
{
    if( m_video )
    {
        freeMlvObject( m_video );
        m_video = nullptr;
    }
    if( m_processing )
    {
        freeProcessingObject( m_processing );
        m_processing = nullptr;
    }
}

bool MlvPipelineFixture::openTinyDualIso(QString * error_message)
{
    return openClip(repo_file_path(QStringLiteral("tests/fixtures/clips/tiny_dual_iso.mlv")), error_message);
}

bool MlvPipelineFixture::openClipFile(const QString & clip_path, QString * error_message)
{
    return openClip(clip_path, error_message);
}

bool MlvPipelineFixture::openClip(const QString & clip_path, QString * error_message)
{
    QByteArray clip_bytes = clip_path.toLocal8Bit();
    char open_error[256] = { 0 };
    int open_code = MLV_ERR_NONE;

    m_video = initMlvObjectWithClip(clip_bytes.data(), MLV_OPEN_FULL, &open_code, open_error);
    if( open_code != MLV_ERR_NONE || !m_video )
    {
        if( error_message )
        {
            *error_message = QStringLiteral("Failed to open clip: %1").arg(QString::fromLocal8Bit(open_error));
        }
        return false;
    }

    m_processing = initProcessingObject();
    setMlvProcessing( m_video, m_processing );
    resetSingleThreadedRuntime();
    return true;
}

bool MlvPipelineFixture::loadReceipt(const QString & relative_path, QString * error_message)
{
    return ReceiptLoader::loadFromFile(repo_file_path(relative_path), &m_receipt, error_message);
}

bool MlvPipelineFixture::applyReceipt(QString * error_message)
{
    if( !m_video || !m_processing )
    {
        if( error_message ) *error_message = QStringLiteral("Fixture is not open.");
        return false;
    }

    ReceiptApplier::applyToMlv(&m_receipt, m_video, m_processing);
    applyDebayerSelection();
    resetSingleThreadedRuntime();
    return true;
}

bool MlvPipelineFixture::resetProcessing(QString * error_message)
{
    if( !m_video || !m_processing )
    {
        if( error_message ) *error_message = QStringLiteral("Fixture is not open.");
        return false;
    }

    processingObject_t * previous = m_processing;
    m_processing = initProcessingObject();
    setMlvProcessing( m_video, m_processing );
    freeProcessingObject( previous );
    resetSingleThreadedRuntime();
    return true;
}

void MlvPipelineFixture::applyDebayerSelection()
{
    switch( m_receipt.debayer() )
    {
    case ReceiptSettings::None:
        setMlvUseNoneDebayer( m_video );
        break;
    case ReceiptSettings::Simple:
        setMlvUseSimpleDebayer( m_video );
        break;
    case ReceiptSettings::Bilinear:
        setMlvDontAlwaysUseAmaze( m_video );
        break;
    case ReceiptSettings::LMMSE:
        setMlvUseLmmseDebayer( m_video );
        break;
    case ReceiptSettings::IGV:
        setMlvUseIgvDebayer( m_video );
        break;
    case ReceiptSettings::AMaZE:
        setMlvAlwaysUseAmaze( m_video );
        break;
    case ReceiptSettings::AHD:
        setMlvUseAhdDebayer( m_video );
        break;
    case ReceiptSettings::RCD:
        setMlvUseRcdDebayer( m_video );
        break;
    case ReceiptSettings::DCB:
        setMlvUseDcbDebayer( m_video );
        break;
    }
}

void MlvPipelineFixture::resetSingleThreadedRuntime()
{
    disableMlvCaching( m_video );
    setMlvCpuCores( m_video, 1 );
    m_video->cache_next = 0;
}

std::vector<uint16_t> MlvPipelineFixture::renderFrame16(uint64_t frame_index, int threads) const
{
    std::vector<uint16_t> frame(static_cast<std::size_t>(width()) * static_cast<std::size_t>(height()) * 3u);
    getMlvProcessedFrame16(m_video, frame_index, frame.data(), threads);
    return frame;
}

std::vector<float> MlvPipelineFixture::renderRawFrameFloat(uint64_t frame_index) const
{
    std::vector<float> frame(static_cast<std::size_t>(width()) * static_cast<std::size_t>(height()));
    getMlvRawFrameFloat(m_video, frame_index, frame.data());
    return frame;
}

std::vector<uint16_t> MlvPipelineFixture::renderDebayeredFrame16(uint64_t frame_index) const
{
    std::vector<uint16_t> frame(static_cast<std::size_t>(width()) * static_cast<std::size_t>(height()) * 3u);
    getMlvRawFrameDebayered(m_video, frame_index, frame.data());
    return frame;
}

std::vector<uint8_t> MlvPipelineFixture::renderFrame8(uint64_t frame_index, int threads) const
{
    std::vector<uint8_t> frame(static_cast<std::size_t>(width()) * static_cast<std::size_t>(height()) * 3u);
    getMlvProcessedFrame8(m_video, frame_index, frame.data(), threads);
    return frame;
}

std::vector<uint16_t> MlvPipelineFixture::renderFrame16Scaled(uint64_t frame_index,
                                                              int threads,
                                                              int scaleFactor) const
{
    /* Phase 4A: dimensions helper still returns full resolution; sized
     * accordingly so existing Phase-4A wiring is forward-compatible. */
    int outW = 0;
    int outH = 0;
    mlvFrameOutputDimensions(m_video, scaleFactor, &outW, &outH);
    std::vector<uint16_t> frame(static_cast<std::size_t>(outW) * static_cast<std::size_t>(outH) * 3u);
    getMlvProcessedFrame16Scaled(m_video, frame_index, frame.data(), threads, scaleFactor);
    return frame;
}

std::vector<uint8_t> MlvPipelineFixture::renderFrame8Scaled(uint64_t frame_index,
                                                            int threads,
                                                            int scaleFactor) const
{
    int outW = 0;
    int outH = 0;
    mlvFrameOutputDimensions(m_video, scaleFactor, &outW, &outH);
    std::vector<uint8_t> frame(static_cast<std::size_t>(outW) * static_cast<std::size_t>(outH) * 3u);
    getMlvProcessedFrame8Scaled(m_video, frame_index, frame.data(), threads, scaleFactor);
    return frame;
}

int MlvPipelineFixture::width() const
{
    return getMlvWidth( m_video );
}

int MlvPipelineFixture::height() const
{
    return getMlvHeight( m_video );
}

const llrawprocWorkerState_t * MlvPipelineFixture::currentLlrawprocWorker() const
{
    if (!m_video || !m_video->llrawproc_workers || !m_video->llrawproc_worker_capacity) {
        return nullptr;
    }

    const pthread_t current_thread = pthread_self();
    const llrawprocWorkerState_t * match = nullptr;

    pthread_mutex_lock(&m_video->llrawproc_worker_mutex);
    for (uint32_t index = 0; index < m_video->llrawproc_worker_capacity; ++index) {
        const llrawprocWorkerState_t * candidate = &m_video->llrawproc_workers[index];
        if (candidate->in_use && pthread_equal(candidate->thread_id, current_thread)) {
            match = candidate;
            break;
        }
    }
    pthread_mutex_unlock(&m_video->llrawproc_worker_mutex);

    return match;
}
