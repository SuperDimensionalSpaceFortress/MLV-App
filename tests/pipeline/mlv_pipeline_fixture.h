#ifndef MLV_APP_MLV_PIPELINE_FIXTURE_H
#define MLV_APP_MLV_PIPELINE_FIXTURE_H

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/mlv_include.h"
#include "../../src/mlv/llrawproc/llrawproc_object.h"

#include <QString>
#include <vector>

class MlvPipelineFixture
{
public:
    MlvPipelineFixture();
    ~MlvPipelineFixture();

    bool openTinyDualIso(QString * error_message);
    bool openClipFile(const QString & clip_path, QString * error_message);
    bool loadReceipt(const QString & relative_path, QString * error_message);
    bool applyReceipt(QString * error_message);
    /* Replaces the processing object with a fresh initProcessingObject() linked to
     * the open clip; call applyReceipt again to restore the receipt's settings. */
    bool resetProcessing(QString * error_message);

    std::vector<float> renderRawFrameFloat(uint64_t frame_index) const;
    std::vector<uint16_t> renderDebayeredFrame16(uint64_t frame_index) const;
    std::vector<uint16_t> renderFrame16(uint64_t frame_index, int threads = 1) const;
    std::vector<uint8_t> renderFrame8(uint64_t frame_index, int threads = 1) const;
    /* Phase 4A: scale-aware variants. scaleFactor must be 1, 2, 4, or 8.
     * Phase 4A returns full-resolution output regardless of scale; the
     * factor is observed only by the cache key. */
    std::vector<uint16_t> renderFrame16Scaled(uint64_t frame_index,
                                              int threads,
                                              int scaleFactor) const;
    std::vector<uint8_t> renderFrame8Scaled(uint64_t frame_index,
                                            int threads,
                                            int scaleFactor) const;

    int width() const;
    int height() const;
    const llrawprocWorkerState_t * currentLlrawprocWorker() const;

    mlvObject_t * video() const { return m_video; }
    processingObject_t * processing() const { return m_processing; }
    ReceiptSettings & receipt() { return m_receipt; }

private:
    bool openClip(const QString & clip_path, QString * error_message);
    void applyDebayerSelection();
    void resetSingleThreadedRuntime();

    mlvObject_t * m_video;
    processingObject_t * m_processing;
    ReceiptSettings m_receipt;
};

#endif
