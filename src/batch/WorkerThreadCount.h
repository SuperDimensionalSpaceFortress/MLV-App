#ifndef MLVAPP_WORKER_THREAD_COUNT_H
#define MLVAPP_WORKER_THREAD_COUNT_H

#include <QByteArray>
#include <QThread>
#include <QtGlobal>

#include "EnvFlags.h"

#ifdef _OPENMP
#include <omp.h>
#endif

inline bool mlvappWorkerThreadOverrideActive()
{
    bool singleThreadOk = false;
    const int singleThread =
        qEnvironmentVariableIntValue("MLVAPP_FORCE_SINGLETHREAD", &singleThreadOk);
    if (singleThreadOk && singleThread > 0) {
        return true;
    }

    bool forcedOk = false;
    const int forced =
        qEnvironmentVariableIntValue("MLVAPP_FORCE_THREADS", &forcedOk);
    return forcedOk && forced > 0;
}

inline bool mlvappEnvFlagEnabled(const char *name)
{
    return mlvappEnvFlagEnabled(qgetenv(name));
}

/* Test and debug override.
 * When MLVAPP_FORCE_THREADS is set to a positive integer, prefer that value
 * over the host's ideal thread count so pipeline outputs can be reproduced
 * deterministically across machines. */
inline int mlvappEffectiveWorkerThreadCount()
{
    bool singleThreadOk = false;
    const int singleThread =
        qEnvironmentVariableIntValue("MLVAPP_FORCE_SINGLETHREAD", &singleThreadOk);
    if (singleThreadOk && singleThread > 0) {
        return 1;
    }

    bool ok = false;
    const int forced = qEnvironmentVariableIntValue("MLVAPP_FORCE_THREADS", &ok);
    if (ok && forced > 0) {
        return forced;
    }

    const int ideal = QThread::idealThreadCount();
    return ideal > 0 ? ideal : 1;
}

inline int mlvappDefaultPlaybackWorkerThreadCapFor(int workerThreads)
{
    if (workerThreads <= 0) {
        return 1;
    }

    return qBound(1, workerThreads, 16);
}

/* GUI playback can become slower when CPU-bound hosts spawn more llrawproc and
 * processing workers than the host can run without contention. Keep explicit
 * test/user thread overrides exact, but let auto playback scale on many-core
 * hosts while staying below the high-contention zone. */
inline int mlvappEffectivePlaybackWorkerThreadCount()
{
    const int workerThreads = mlvappEffectiveWorkerThreadCount();
    if (mlvappWorkerThreadOverrideActive()
        || mlvappEnvFlagEnabled("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP")) {
        return workerThreads;
    }

    bool capOk = false;
    int cap = qEnvironmentVariableIntValue("MLVAPP_PLAYBACK_MAX_THREADS", &capOk);
    if (!capOk || cap <= 0) {
        cap = mlvappDefaultPlaybackWorkerThreadCapFor(workerThreads);
    }

    return qBound(1, workerThreads, cap);
}

/* CPU-S1-FS-SPIKE-TEAM-PARTITION-1: an opt-in, default-OFF asymmetric split of the playback
 * OpenMP teams (bench knob). MLVAPP_PLAYBACK_RECON_OMP_THREADS sizes the recon side (the
 * render thread, the cpu_cores llrawproc reads, and the Phase-3 recon worker) and
 * MLVAPP_PLAYBACK_PREP_OMP_THREADS sizes the playback prep thread's team. A value is read only
 * when it is a positive decimal integer (digits only); anything else is unset. It is clamped to
 * [1, mlvappEffectivePlaybackWorkerThreadCount()] and is INERT while MLVAPP_FORCE_THREADS or
 * MLVAPP_FORCE_SINGLETHREAD is active, so deterministic test runs keep their exact count.
 * Returns the request, 0 when it is unset or inert. */
inline int mlvappPlaybackTeamRequestFromValue(const QByteArray &raw)
{
    if (raw.isEmpty()) {
        return 0;
    }
    qint64 value = 0;
    for (const char c : raw) {
        if (c < '0' || c > '9') {
            return 0;
        }
        if (value < 1000000) {
            value = value * 10 + (c - '0');
        }
    }
    return value > 0 ? static_cast<int>(qMin<qint64>(value, 1000000)) : 0;
}

/* Adversary pass 2 A2 on 95d5498e: MLVAPP_FORCE_SINGLETHREAD is ACTIVE by ForceSingleThread.cpp's envTruthy rule (trimmed; anything but
 * empty, "0", "false", "off" or "no", case-insensitive), so "true" activates single-thread mode while the integer read in
 * mlvappWorkerThreadOverrideActive() says no override. The team knobs are inert under either reading. (mlvappWorkerThreadOverrideActive
 * itself is unchanged: its callers predate this card.) */
inline bool mlvappForceSingleThreadEnvTruthy(const QByteArray &raw)
{
    const QByteArray value = raw.trimmed();
    if (value.isEmpty() || value == "0") {
        return false;
    }
    return value.compare("false", Qt::CaseInsensitive) != 0 && value.compare("off", Qt::CaseInsensitive) != 0
        && value.compare("no", Qt::CaseInsensitive) != 0;
}

inline bool mlvappPlaybackTeamOverrideActive()
{
    return mlvappWorkerThreadOverrideActive() || mlvappForceSingleThreadEnvTruthy(qgetenv("MLVAPP_FORCE_SINGLETHREAD"));
}

inline int mlvappPlaybackTeamRequest(const char *name)
{
    if (mlvappPlaybackTeamOverrideActive()) {
        return 0;
    }
    const int requested = mlvappPlaybackTeamRequestFromValue(qgetenv(name));
    return requested > 0 ? qBound(1, requested, mlvappEffectivePlaybackWorkerThreadCount()) : 0;
}

inline int mlvappPlaybackReconTeamRequest()
{
    return mlvappPlaybackTeamRequest("MLVAPP_PLAYBACK_RECON_OMP_THREADS");
}

inline int mlvappPlaybackPrepTeamRequest()
{
    return mlvappPlaybackTeamRequest("MLVAPP_PLAYBACK_PREP_OMP_THREADS");
}

/* The recon / prep team size: the request when it is set, otherwise exactly
 * mlvappEffectivePlaybackWorkerThreadCount() (so an unset knob changes nothing). */
inline int mlvappPlaybackReconWorkerThreadCount()
{
    const int requested = mlvappPlaybackReconTeamRequest();
    return requested > 0 ? requested : mlvappEffectivePlaybackWorkerThreadCount();
}

inline int mlvappPlaybackPrepWorkerThreadCount()
{
    const int requested = mlvappPlaybackPrepTeamRequest();
    return requested > 0 ? requested : mlvappEffectivePlaybackWorkerThreadCount();
}

inline int mlvappPlaybackOpenMpThreadTargetFor(int workerThreads, int currentOpenMpMaxThreads)
{
    if (workerThreads <= 0) {
        return currentOpenMpMaxThreads > 0 ? currentOpenMpMaxThreads : 1;
    }

    if (currentOpenMpMaxThreads > 0 && currentOpenMpMaxThreads < workerThreads) {
        return currentOpenMpMaxThreads;
    }

    return workerThreads;
}

inline int mlvappApplyPlaybackOpenMpThreadCount(int workerThreads)
{
#ifdef _OPENMP
    const int currentOpenMpMaxThreads = omp_get_max_threads();
    const int target =
        mlvappPlaybackOpenMpThreadTargetFor(workerThreads, currentOpenMpMaxThreads);
    if (target > 0 && target != currentOpenMpMaxThreads) {
        omp_set_num_threads(target);
    }
    return target;
#else
    Q_UNUSED(workerThreads);
    return 1;
#endif
}

#endif
