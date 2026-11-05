// CPU-SCOPE-LIVE-CRASH-FIX-1: does a thread_local with a non-trivial destructor outlive its own storage on this toolchain?
//
// The dump of the 0xC0000374 soak crash (build 4f480f19) read as: at thread exit winpthreads runs libgcc's emutls key
// destructor, which frees the thread's thread-local objects; the exe's TLS callback then runs the C++ destructor on that
// freed storage. This fixture measures that order on the pinned toolchain, with no application code in it.
//
// RED does not prove the crash was this bug: the order is a toolchain property that also holds in clean runs. Only a valid
// GREEN refutes the explanation. INVALID means the fixture itself failed a control and says nothing either way.
//
// Usage: tls_exit_order_fixture <mode> [kind]
//   order <kind>   K's destructor stamps a sequence number, ~Probe stamps one. RED = K stamped first.
//   reuse <kind>   ~Probe searches the heap for its own base block. RED = found (it was already freed).
//   crash          K's destructor writes the dump's bytes into the freed block. Expected child exit 0xC0000374.
//   helper <kind>  The same adversary, with T from mlvThreadExitCache. Expected: ~T exactly once on the thread, exit 0.
//   identity       Repeated calls on one thread return the same object and do not reallocate.
//   bench          ns per access, old thread_local against the helper.
//   churn          100 std::threads each run the real recursive_bf_wrap on 256x256x3. Expected: PagefileUsage growth under 64 MB.
//   churn_control  The same with a filter nothing frees. Expected: over the bound, or the bound proves nothing.
//   setspecific_fail  The helper with pthread_setspecific failing. Expected: it stops with abort, GREEN; returning is RED.
//   kind is stdthread | qthread | omp, and also main for helper.
//
// Output: one "RESULT mode=... kind=... verdict=... ..." line on stdout. Exit 0 when a verdict was reached, 3 when INVALID,
// 4 on a usage error.

#include <windows.h>
#include <psapi.h>

#include <pthread.h>
#include <omp.h>

#include <QThread>

#include "processing/ThreadExitCache.h"
#include "processing/rbfilter/RBFilterPlain.h"
#include "processing/rbfilter/rbf_wrapper.h"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>

int tlsExitOrderInjectedAccess(); // tls_exit_order_inject.cpp

namespace
{

// The shape of PointwiseFastTables (platform/qt/GpuPreviewProcessing.cpp): three non-empty vectors, sizeof 0x48.
struct Probe
{
    std::vector<int32_t> a, b, c;
    Probe();
    ~Probe();
};
static_assert(sizeof(Probe) == 0x48, "Probe must keep the shape of PointwiseFastTables");

constexpr size_t kBlockSize = sizeof(Probe) + 8; // libgcc emutls: malloc(size + 8), base stored at *base, object at base + 8.
constexpr long kSearchLimit = 200000;

enum class Mode { Order, Reuse, Crash, Helper };

Mode g_mode = Mode::Order;
pthread_key_t g_key_k;

std::atomic<int> g_seq{0};
std::atomic<int> g_k_stamp{0};
std::atomic<int> g_probe_stamp{0};
std::atomic<bool> g_base_mismatch{false};

void * g_base = nullptr;     // Recorded while the object is alive. this[-1] is never read after thread exit has begun.
void * g_pos_block = nullptr; // A same-size block this thread frees: the search must find it, or the search proves nothing.
void * g_pos_target = nullptr; // Its address, kept after the free.
void * g_neg_block = nullptr; // A same-size block that stays live: the search must never find it.
HANDLE g_worker_handle = nullptr;

struct SearchResult
{
    bool ran = false;
    bool base_found = false;
    bool pos_found = false;
    bool neg_found = false;
    long attempts = 0;
};
SearchResult g_search;

void ** g_kept = nullptr; // VirtualAlloc'd in main, so the search's bookkeeping does not touch the heap under test.

void freePositiveControlOnce()
{
    if (g_pos_block)
    {
        void * const p = g_pos_block;
        g_pos_block = nullptr;
        std::free(p);
    }
}

// Allocates blocks of the emutls size until it has the base and the positive control back, or the limit. A block equal to
// the base means the base was free. Returns the block equal to the base, still allocated, when keepBase is set.
void * searchForBase(const bool keepBase)
{
    long n = 0;
    void * found = nullptr;
    for (; n < kSearchLimit; ++n)
    {
        void * const m = std::malloc(kBlockSize);
        if (!m) break;
        g_kept[n] = m;
        if (m == g_base) { g_search.base_found = true; found = m; }
        if (m == g_pos_target) g_search.pos_found = true;
        if (m == g_neg_block) g_search.neg_found = true;
        if (g_search.base_found && g_search.pos_found) { ++n; break; }
    }
    g_search.attempts = n;
    g_search.ran = true;
    return keepBase ? found : nullptr;
}

void freeKept(const long count)
{
    for (long i = 0; i < count; ++i) std::free(g_kept[i]);
}

// The dump's bytes: ref 0 at +0, alloc 31 at +8, UTF-16 text from +16. The vector at object +0x30 then reads "ock_".
void writeDumpBytes(void * const block)
{
    static const char16_t text[] = u"llrawproc_snapshot_lock_wait_ms";
    static_assert(sizeof(text) == 64, "31 UTF-16 code units and a terminator");
    static_assert(16 + sizeof(text) == kBlockSize, "the text must fill the block exactly");
    auto * const bytes = static_cast<unsigned char *>(block);
    const uint64_t ref = 0, alloc = 31;
    std::memcpy(bytes, &ref, 8);
    std::memcpy(bytes + 8, &alloc, 8);
    std::memcpy(bytes + 16, text, sizeof(text));
}

void keyDestructor(void *)
{
    g_k_stamp.store(++g_seq);
    // Free the control as close to the base's own free as this fixture can get.
    freePositiveControlOnce();
    if (g_mode == Mode::Crash || g_mode == Mode::Helper)
    {
        // Same search as reuse, from the key destructor: the storage is free, and this is the slot being taken by another
        // owner before the late destructor reads it.
        void * const block = searchForBase(true);
        if (block) writeDumpBytes(block);
        freeKept(g_search.attempts);
    }
}

Probe::Probe() : a(16, 1), b(16, 2), c(16, 3) {}

__attribute__((noinline)) Probe::~Probe()
{
    g_probe_stamp.store(++g_seq);
    if (g_mode == Mode::Reuse)
    {
        freePositiveControlOnce();
        searchForBase(false);
        freeKept(g_search.attempts);
    }
    // Members are destroyed after this. In crash mode that is the free() the dump shows failing.
}

// K that was never set never fires, which would read as a result. A failure here is the fixture's own: stop.
void setKeyK()
{
    if (pthread_setspecific(g_key_k, reinterpret_cast<void *>(1)) != 0)
    {
        std::fprintf(stderr, "pthread_setspecific(K) failed\n");
        std::_Exit(3);
    }
}

__attribute__((noinline)) void touchProbe()
{
    static thread_local Probe probe;
    void * const base = reinterpret_cast<void **>(&probe)[-1];
    g_base = base;
    if (base != reinterpret_cast<char *>(&probe) - 8) g_base_mismatch.store(true);
}

void workerBody()
{
    DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &g_worker_handle, 0, FALSE, DUPLICATE_SAME_ACCESS);
    touchProbe();
    g_pos_block = std::malloc(kBlockSize);
    g_pos_target = g_pos_block;
    g_neg_block = std::malloc(kBlockSize);
    setKeyK();
}

// Runs workerBody on a thread of the given kind and waits for that thread's handle: a handle is signalled only after the
// loader has finished DLL_THREAD_DETACH, so nothing below races the destructors. Returns false on an unknown kind or timeout.
bool runOnKind(const std::string & kind, const std::function<void()> & body)
{
    if (kind == "stdthread")
    {
        std::thread t(body);
        t.join();
    }
    else if (kind == "qthread")
    {
        QThread * const t = QThread::create(body);
        t->start();
        t->wait();
        delete t;
    }
    else if (kind == "omp")
    {
        // An OpenMP team started from a std::thread that then exits. Member 1 is a libgomp pool thread; it leaves when the
        // master's gomp_free_thread releases the pool, which std::thread::join does not wait for.
        std::thread t([&body] {
            omp_set_dynamic(0);
#pragma omp parallel num_threads(4)
            {
                if (omp_get_thread_num() == 1) body();
            }
        });
        t.join();
    }
    else
    {
        return false;
    }
    if (!g_worker_handle) return false;
    const DWORD wait = WaitForSingleObject(g_worker_handle, 30000);
    CloseHandle(g_worker_handle);
    g_worker_handle = nullptr;
    return wait == WAIT_OBJECT_0;
}

// Force the low-fragmentation heap on for this size class, as it was in the dump (slot 316 of an LFH user block). Without
// this a freed block can coalesce with a neighbour in the backend, and the search would miss it and read as GREEN.
void warmLfh()
{
    static void * warm[2000];
    for (void *& p : warm) p = std::malloc(kBlockSize);
    for (size_t i = 0; i < 2000; i += 2) std::free(warm[i]);
}

int printResult(const char * mode, const std::string & kind, const char * verdict, const std::string & detail)
{
    std::printf("RESULT mode=%s kind=%s verdict=%s %s\n", mode, kind.c_str(), verdict, detail.c_str());
    std::fflush(stdout);
    return std::strcmp(verdict, "INVALID") == 0 ? 3 : 0;
}

int reportOrder(const std::string & kind, const bool finished)
{
    const int k = g_k_stamp.load();
    const int probe = g_probe_stamp.load();
    const std::string detail = "k_seq=" + std::to_string(k) + " probe_seq=" + std::to_string(probe);
    if (!finished) return printResult("order", kind, "INVALID", detail + " why=worker_never_finished");
    if (g_base_mismatch.load()) return printResult("order", kind, "INVALID", detail + " why=base_is_not_object_minus_8");
    if (probe == 0) return printResult("order", kind, "INVALID", detail + " why=probe_destructor_never_ran");
    if (k == 0) return printResult("order", kind, kind == "stdthread" ? "INVALID" : "NO_KEY_DTOR", detail + " why=key_destructor_never_ran");
    return printResult("order", kind, k < probe ? "RED" : "GREEN", detail);
}

int reportReuse(const std::string & kind, const bool finished)
{
    const SearchResult & s = g_search;
    const std::string detail = "k_seq=" + std::to_string(g_k_stamp.load()) + " probe_seq=" + std::to_string(g_probe_stamp.load())
        + " base_found=" + std::to_string(s.base_found) + " pos_found=" + std::to_string(s.pos_found)
        + " neg_found=" + std::to_string(s.neg_found) + " attempts=" + std::to_string(s.attempts);
    if (!finished) return printResult("reuse", kind, "INVALID", detail + " why=worker_never_finished");
    if (g_base_mismatch.load()) return printResult("reuse", kind, "INVALID", detail + " why=base_is_not_object_minus_8");
    if (!s.ran) return printResult("reuse", kind, "INVALID", detail + " why=search_never_ran");
    if (!s.pos_found) return printResult("reuse", kind, "INVALID", detail + " why=positive_control_not_found");
    if (s.neg_found) return printResult("reuse", kind, "INVALID", detail + " why=negative_control_found");
    return printResult("reuse", kind, s.base_found ? "RED" : "GREEN", detail);
}

// --- The new pattern (src/processing/ThreadExitCache.h) ---------------------------------------------------------------

std::atomic<int> g_helper_ctor{0};
std::atomic<int> g_helper_dtor{0};
std::atomic<int> g_helper_dtor_stamp{0};
std::atomic<int> g_helper_bad{0};

// The Probe shape again, this time a heap object owned by the helper. Its destructor reads no thread-local.
struct HelperT
{
    std::vector<int32_t> a, b, c;
    HelperT() : a(16, 1), b(16, 2), c(16, 3) { g_helper_ctor.fetch_add(1); }
    ~HelperT()
    {
        const bool intact = a.size() == 16 && b.size() == 16 && c.size() == 16 && a[15] == 1 && b[15] == 2 && c[15] == 3;
        if (!intact) g_helper_bad.fetch_add(1);
        g_helper_dtor_stamp.store(++g_seq);
        g_helper_dtor.fetch_add(1);
    }
};

// A same-size object on emutls with no destructor: its storage is what emutls frees, so the adversary has a block to take.
struct Filler
{
    unsigned char bytes[sizeof(Probe)];
};

__attribute__((noinline)) void helperBody()
{
    static thread_local Filler filler;
    void * const self = &filler;
    if (!DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &g_worker_handle, 0, FALSE, DUPLICATE_SAME_ACCESS)) return;
    filler.bytes[0] = 1;
    void * const base = reinterpret_cast<void **>(self)[-1];
    g_base = base;
    if (base != static_cast<char *>(self) - 8) g_base_mismatch.store(true);
    mlvThreadExitCache<HelperT>();
    g_pos_block = std::malloc(kBlockSize);
    g_pos_target = g_pos_block;
    g_neg_block = std::malloc(kBlockSize);
    setKeyK();
}

int runHelper(const std::string & kind)
{
    g_mode = Mode::Helper;
    if (kind == "main")
    {
        // Bounded to one object. Exit 0 from the process is the result: no key destructor is promised on the main thread.
        helperBody();
        CloseHandle(g_worker_handle);
        g_worker_handle = nullptr;
        return printResult("helper", kind, g_helper_ctor.load() == 1 ? "GREEN" : "INVALID",
                           "ctor=" + std::to_string(g_helper_ctor.load()) + " dtor=" + std::to_string(g_helper_dtor.load()));
    }
    const bool finished = runOnKind(kind, helperBody);
    const SearchResult & s = g_search;
    const std::string detail = "ctor=" + std::to_string(g_helper_ctor.load()) + " dtor=" + std::to_string(g_helper_dtor.load())
        + " bad=" + std::to_string(g_helper_bad.load()) + " k_seq=" + std::to_string(g_k_stamp.load())
        + " dtor_seq=" + std::to_string(g_helper_dtor_stamp.load()) + " base_found=" + std::to_string(s.base_found)
        + " pos_found=" + std::to_string(s.pos_found) + " neg_found=" + std::to_string(s.neg_found) + " attempts=" + std::to_string(s.attempts);
    if (!finished) return printResult("helper", kind, "INVALID", detail + " why=worker_never_finished");
    if (g_base_mismatch.load()) return printResult("helper", kind, "INVALID", detail + " why=base_is_not_object_minus_8");
    // The adversary has to have run, or this exit 0 says nothing.
    if (!s.ran || !s.pos_found || !s.base_found || s.neg_found) return printResult("helper", kind, "INVALID", detail + " why=adversary_did_not_take_the_block");
    if (g_helper_ctor.load() != 1 || g_helper_bad.load() != 0) return printResult("helper", kind, "RED", detail);
    if (g_helper_dtor.load() == 0) return printResult("helper", kind, "NO_KEY_DTOR", detail);
    return printResult("helper", kind, g_helper_dtor.load() == 1 ? "GREEN" : "RED", detail);
}

int runIdentity()
{
    HelperT * const first = &mlvThreadExitCache<HelperT>();
    const int32_t * const firstData = first->a.data();
    long moved = 0;
    for (long i = 0; i < 1000000; ++i)
    {
        HelperT & t = mlvThreadExitCache<HelperT>();
        if (&t != first || t.a.data() != firstData) ++moved;
    }
    HelperT * other = nullptr;
    std::thread t([&other] { other = &mlvThreadExitCache<HelperT>(); });
    t.join();
    const std::string detail = "moved=" + std::to_string(moved) + " ctor=" + std::to_string(g_helper_ctor.load())
        + " dtor=" + std::to_string(g_helper_dtor.load()) + " other_thread_distinct=" + std::to_string(other != first);
    const bool ok = moved == 0 && g_helper_ctor.load() == 2 && g_helper_dtor.load() == 1 && other != first;
    return printResult("identity", "stdthread", ok ? "GREEN" : "RED", detail);
}

__attribute__((noinline)) int32_t benchOldOnce()
{
    static thread_local HelperT t;
    return t.a[0];
}

__attribute__((noinline)) int32_t benchNewOnce()
{
    return mlvThreadExitCache<HelperT>().a[0];
}

template <class Fn>
double nsPerCall(Fn fn, const long iterations)
{
    int64_t sum = 0;
    const auto start = std::chrono::steady_clock::now();
    for (long i = 0; i < iterations; ++i) sum += fn();
    const auto stop = std::chrono::steady_clock::now();
    if (sum != iterations) return -1.0;
    return std::chrono::duration<double, std::nano>(stop - start).count() / iterations;
}

int runBench()
{
    constexpr long kIterations = 10000000;
    benchOldOnce();
    benchNewOnce();
    // Twice each, new and old alternating, so one slow moment on a shared host is visible rather than averaged away.
    const double old1 = nsPerCall(benchOldOnce, kIterations);
    const double new1 = nsPerCall(benchNewOnce, kIterations);
    const double old2 = nsPerCall(benchOldOnce, kIterations);
    const double new2 = nsPerCall(benchNewOnce, kIterations);
    char detail[160];
    std::snprintf(detail, sizeof(detail), "old_ns=%.2f,%.2f new_ns=%.2f,%.2f iterations=%ld", old1, old2, new1, new2, kIterations);
    return printResult("bench", "main", (old1 < 0 || new1 < 0 || old2 < 0 || new2 < 0) ? "INVALID" : "REPORT", detail);
}

size_t pagefileUsage()
{
    PROCESS_MEMORY_COUNTERS counters{};
    counters.cb = sizeof(counters);
    GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof(counters));
    return counters.PagefileUsage;
}

// 100 threads, one after another, each filter a 256x256x3 image with the real rbf_wrapper.cpp and exit.
// churn_control does the same with a filter nothing ever frees: it must trip the same bound, or the bound proves nothing.
int runChurn(const bool leakControl)
{
    constexpr int kW = 256, kH = 256, kThreads = 100;
    constexpr size_t kBoundBytes = 64u * 1024u * 1024u;
    std::vector<uint16_t> in(size_t(kW) * kH * 3, 12345);
    std::vector<uint16_t> out(in.size());
    const auto work = [&] {
        if (leakControl)
        {
            auto * const leaked = new CRBFilterPlain();
            leaked->reserveMemory(kW, kH, 3);
            leaked->filter(in.data(), out.data(), 0.0025f, 0.075f, kW, kH, 3, nullptr, nullptr, nullptr, nullptr);
        }
        else
        {
            recursive_bf_wrap(in.data(), out.data(), 0.0025f, 0.075f, kW, kH, 3);
        }
    };
    { std::thread first(work); first.join(); }
    const size_t before = pagefileUsage();
    for (int i = 1; i < kThreads; ++i) { std::thread t(work); t.join(); }
    const size_t after = pagefileUsage();
    const double growthMb = (double(after) - double(before)) / (1024.0 * 1024.0);
    char detail[120];
    std::snprintf(detail, sizeof(detail), "threads=%d growth_mb=%.1f bound_mb=64 before_mb=%.1f", kThreads, growthMb, double(before) / (1024.0 * 1024.0));
    const bool underBound = after < before + kBoundBytes;
    if (leakControl) return printResult("churn_control", "stdthread", underBound ? "INVALID" : "CONTROL_OK", detail);
    return printResult("churn", "stdthread", underBound ? "GREEN" : "RED", detail);
}

// Adversary A1 (tls_exit_order_inject.cpp): the helper must stop rather than return an object no key will ever release.
int runSetspecificFail()
{
    std::signal(SIGABRT, [](int) {
        std::printf("RESULT mode=setspecific_fail kind=main verdict=GREEN stopped_with_abort=1\n");
        std::fflush(stdout);
        std::_Exit(0);
    });
    const int value = tlsExitOrderInjectedAccess();
    return printResult("setspecific_fail", "main", "RED", "returned_unregistered_object=1 value=" + std::to_string(value));
}

// mingw-w64 v11's werapi.h does not compile, and kernel32 exports this, so it is resolved rather than declared.
void disableWerUi()
{
    using WerSetFlagsFn = HRESULT(WINAPI *)(DWORD);
    constexpr DWORD kWerFaultReportingNoUi = 0x20;
    const auto fn = reinterpret_cast<WerSetFlagsFn>(reinterpret_cast<void *>(GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "WerSetFlags")));
    if (fn) fn(kWerFaultReportingNoUi);
}

} // namespace

// Touched on main so libgcc's emutls key exists before K: K then runs after emutls_destroy on every thread.
static thread_local int g_emutls_touch = 0;

int main(int argc, char ** argv)
{
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    disableWerUi();

    if (argc < 2)
    {
        std::fprintf(stderr, "usage: tls_exit_order_fixture order|reuse|crash|helper|identity|bench|churn|churn_control [stdthread|qthread|omp|main]\n");
        return 4;
    }
    const std::string mode = argv[1];
    const std::string kind = argc > 2 ? argv[2] : "stdthread";
    const bool knownKind = kind == "stdthread" || kind == "qthread" || kind == "omp" || (kind == "main" && mode == "helper");
    if (!knownKind)
    {
        std::fprintf(stderr, "unknown kind %s for mode %s\n", kind.c_str(), mode.c_str());
        return 4;
    }

    g_emutls_touch = 1;
    if (g_emutls_touch != 1) return 4;
    if (pthread_key_create(&g_key_k, keyDestructor) != 0) return 4;

    g_kept = static_cast<void **>(VirtualAlloc(nullptr, kSearchLimit * sizeof(void *), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
    if (!g_kept) return 4;
    warmLfh();

    if (mode == "order")
    {
        g_mode = Mode::Order;
        return reportOrder(kind, runOnKind(kind, workerBody));
    }
    if (mode == "reuse")
    {
        g_mode = Mode::Reuse;
        return reportReuse(kind, runOnKind(kind, workerBody));
    }
    if (mode == "crash")
    {
        g_mode = Mode::Crash;
        const bool finished = runOnKind(kind, workerBody);
        // Reached only when the free() did not fast-fail. Not a gate: heap termination policy may differ on this OS.
        return printResult("crash", kind, "NO_FAST_FAIL",
                           "finished=" + std::to_string(finished) + " base_found=" + std::to_string(g_search.base_found)
                               + " pos_found=" + std::to_string(g_search.pos_found) + " attempts=" + std::to_string(g_search.attempts));
    }
    if (mode == "setspecific_fail") return runSetspecificFail();
    if (mode == "helper") return runHelper(kind);
    if (mode == "identity") return runIdentity();
    if (mode == "bench") return runBench();
    if (mode == "churn") return runChurn(false);
    if (mode == "churn_control") return runChurn(true);
    std::fprintf(stderr, "unknown mode %s\n", mode.c_str());
    return 4;
}
