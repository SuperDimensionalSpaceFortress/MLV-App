// gui_tests links GpuDebayer.cpp and GpuDisplayViewport.cpp but not llrawproc or CrashForensics,
// which the real GpuFieldTelemetry.cpp needs. Telemetry is reported OFF here, so every guarded call
// site in those files is skipped exactly as with MLVAPP_GPU_TELEMETRY=0.
#include "../../platform/qt/GpuFieldTelemetry.h"

namespace GpuFieldTelemetry {

bool enabled() { return false; }
void install() {}
void noteStage(const char *, const char *, double) {}
void noteFault(const CudaFault &) {}
bool isDeviceLossCode(int) { return false; }
void setFaultListener(std::function<void(const CudaFault &)>) {}
void noteRecovery(const char *, const char *) {}
void noteClip(int, int, int, bool, bool, const QString &) {}
void noteGlIdentity(const QString &, const QString &, const QString &, const QString &, const QString &) {}
void noteGlContext(QOpenGLContext *) {}
void flush(const char *) {}
QString clipHash(const QString &) { return QString(); }

} // namespace GpuFieldTelemetry
