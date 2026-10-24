#include "../common/hash_helpers.h"
#include "../common/repo_paths.h"
#include "../common/test_runtime.h"
#include "../pipeline/mlv_pipeline_fixture.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <tlhelp32.h>
#endif

static QElapsedTimer g_helper_clock;

// The last stage line and when it was printed, read by the stall watchdog.
static std::mutex g_stage_mutex;
static std::condition_variable g_stage_changed;
static std::string g_stage_line;
static long long g_stage_started_ms = 0;
static unsigned g_stage_serial = 0;

// The fixture whose clip state the watchdog prints, or null. Cleared under g_probe_mutex before the
// fixture is destroyed, so a probe never reads a freed object.
static std::mutex g_probe_mutex;
static const MlvPipelineFixture * g_probed_fixture = nullptr;

// Flushed stage breadcrumbs on stderr. The parent merges them into the output tail it prints
// on a timeout, so a hang names the stage it was stuck in.
static void note_stage(const char * stage, const QString & detail = QString())
{
    char line[1024];
    std::snprintf(line, sizeof(line), "avx_parity_helper t=%lldms stage=%s %s",
                  static_cast<long long>(g_helper_clock.elapsed()),
                  stage,
                  detail.toLocal8Bit().constData());
    std::fprintf(stderr, "%s\n", line);
    std::fflush(stderr);
    {
        std::lock_guard<std::mutex> lock(g_stage_mutex);
        g_stage_line = line;
        g_stage_started_ms = g_helper_clock.elapsed();
        ++g_stage_serial;
    }
    g_stage_changed.notify_all();
}

// Publishes a fixture to the watchdog for the lifetime of this object. Declare it after the fixture,
// so it is destroyed (and unpublished) first.
class ProbedFixtureScope {
public:
    explicit ProbedFixtureScope(const MlvPipelineFixture * fixture)
    {
        std::lock_guard<std::mutex> lock(g_probe_mutex);
        g_probed_fixture = fixture;
    }
    ~ProbedFixtureScope()
    {
        std::lock_guard<std::mutex> lock(g_probe_mutex);
        g_probed_fixture = nullptr;
    }
};

// A read of a field another thread writes, with no synchronization. Diagnostics only: the watchdog
// takes no clip lock, so a deadlocked clip cannot also stall the report. The volatile cast only stops
// the compiler from caching or dropping the load; it is still a data race, and a value may be stale.
// The fields are plain members of mlvObject_t that src/ writes with plain stores, so no reader-side
// atomic (std::atomic_ref, __atomic_load_n) or helper-published copy can make the read race-free;
// only the writers can. The probe line says so in its own text.
template <typename T>
static T racy_read(const T & field)
{
    return *static_cast<const volatile T *>(&field);
}

// One line of the clip's open/cache state. Inside the open-clip window it tells the sub-steps apart:
// video=null is initMlvObjectWithClip (parse, cache allocation, cache worker start); processing=null
// is initProcessingObject; stop_caching=1 with cache_thread_count>0 is disableMlvCaching waiting
// for the cache workers that openMlvClip started.
static std::string probe_line()
{
    std::lock_guard<std::mutex> lock(g_probe_mutex);
    char line[512];
    if (!g_probed_fixture) {
        std::snprintf(line, sizeof(line), "AVX_PARITY_HELPER_STALL probe fixture=none");
        return line;
    }
    // Null until initMlvObjectWithClip returns inside openClipFile.
    mlvObject_t * video = g_probed_fixture->video();
    if (!video) {
        std::snprintf(line, sizeof(line),
                      "AVX_PARITY_HELPER_STALL probe read=\"unsynchronized best-effort read\" fixture=open video=null");
        return line;
    }
    std::snprintf(line, sizeof(line),
                  "AVX_PARITY_HELPER_STALL probe read=\"unsynchronized best-effort read\" fixture=open video=set "
                  "processing=%s is_active=%d frames=%u "
                  "cpu_cores=%d cache_thread_count=%d stop_caching=%d cache_lifecycle_users=%d cache_closing=%d "
                  "raw_uint16_prefetch_threads_started=%d processed8_prefetch_thread_started=%d",
                  racy_read(video->processing) ? "set" : "null",
                  racy_read(video->is_active),
                  static_cast<unsigned>(racy_read(video->frames)),
                  racy_read(video->cpu_cores),
                  racy_read(video->cache_thread_count),
                  racy_read(video->stop_caching),
                  racy_read(video->cache_lifecycle_users),
                  racy_read(video->cache_closing),
                  racy_read(video->raw_uint16_prefetch_threads_started),
                  racy_read(video->processed8_prefetch_thread_started));
    return line;
}

#ifdef _WIN32
static DWORD g_main_thread_id = 0;

// Function symbols of this executable, from its COFF symbol table (a debug MinGW link keeps one),
// so a thread dump names functions rather than bare offsets.
class ExeSymbols {
public:
    void load()
    {
        if (loaded_) {
            return;
        }
        loaded_ = true;
        base_ = reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
        const auto * dos = reinterpret_cast<const IMAGE_DOS_HEADER *>(base_);
        const auto * nt = reinterpret_cast<const IMAGE_NT_HEADERS *>(base_ + dos->e_lfanew);
        const IMAGE_SECTION_HEADER * sections = IMAGE_FIRST_SECTION(nt);
        const WORD section_count = nt->FileHeader.NumberOfSections;
        for (WORD i = 0; i < section_count; ++i) {
            if (std::memcmp(sections[i].Name, ".text", 5) == 0) {
                text_begin_ = base_ + sections[i].VirtualAddress;
                text_end_ = text_begin_ + sections[i].Misc.VirtualSize;
            }
        }

        const DWORD symbol_offset = nt->FileHeader.PointerToSymbolTable;
        const DWORD symbol_count = nt->FileHeader.NumberOfSymbols;
        if (symbol_offset == 0 || symbol_count == 0 || symbol_count > 4000000) {
            return;
        }
        wchar_t path[MAX_PATH];
        if (GetModuleFileNameW(nullptr, path, MAX_PATH) == 0) {
            return;
        }
        std::FILE * file = _wfopen(path, L"rb");
        if (!file) {
            return;
        }
        std::vector<unsigned char> table(static_cast<size_t>(symbol_count) * IMAGE_SIZEOF_SYMBOL);
        std::vector<char> strings;
        bool read_ok = _fseeki64(file, symbol_offset, SEEK_SET) == 0
            && std::fread(table.data(), 1, table.size(), file) == table.size();
        uint32_t strings_size = 0;
        if (read_ok && std::fread(&strings_size, sizeof(strings_size), 1, file) == 1
            && strings_size > 4 && strings_size < (64u << 20)) {
            strings.assign(strings_size, '\0');
            read_ok = std::fread(strings.data() + 4, 1, strings_size - 4, file) == strings_size - 4;
        }
        std::fclose(file);
        if (!read_ok) {
            return;
        }

        for (DWORD i = 0; i < symbol_count; ++i) {
            const auto * symbol = reinterpret_cast<const IMAGE_SYMBOL *>(table.data() + static_cast<size_t>(i) * IMAGE_SIZEOF_SYMBOL);
            const DWORD aux = symbol->NumberOfAuxSymbols;
            const SHORT section = symbol->SectionNumber;
            const bool is_function = ISFCN(symbol->Type)
                || symbol->StorageClass == IMAGE_SYM_CLASS_EXTERNAL
                || symbol->StorageClass == IMAGE_SYM_CLASS_STATIC;
            if (section > 0 && section <= section_count && is_function) {
                const uintptr_t address = base_ + sections[section - 1].VirtualAddress + symbol->Value;
                if (address >= text_begin_ && address < text_end_) {
                    std::string name;
                    if (symbol->N.Name.Short == 0) {
                        const DWORD offset = symbol->N.Name.Long;
                        if (offset < strings.size()) {
                            name = strings.data() + offset;
                        }
                    } else {
                        name.assign(reinterpret_cast<const char *>(symbol->N.ShortName),
                                    strnlen(reinterpret_cast<const char *>(symbol->N.ShortName), 8));
                    }
                    if (!name.empty() && name[0] != '.') {
                        symbols_.emplace_back(address, std::move(name));
                    }
                }
            }
            i += aux;
        }
        std::sort(symbols_.begin(), symbols_.end());
    }

    bool in_text(uint64_t address) const { return address >= text_begin_ && address < text_end_; }
    uintptr_t text_begin() const { return text_begin_; }
    uintptr_t text_end() const { return text_end_; }

    // "name+0xoffset" for an address in this executable, else "module+0xoffset", else the raw address.
    std::string describe(uint64_t address) const
    {
        char text[512];
        if (in_text(address)) {
            auto it = std::upper_bound(symbols_.begin(), symbols_.end(),
                                       std::make_pair(static_cast<uintptr_t>(address), std::string("\x7f")));
            if (it != symbols_.begin()) {
                --it;
                std::snprintf(text, sizeof(text), "%s+0x%llx", it->second.c_str(),
                              static_cast<unsigned long long>(address - it->first));
                return text;
            }
            std::snprintf(text, sizeof(text), "exe+0x%llx", static_cast<unsigned long long>(address - base_));
            return text;
        }
        HMODULE module = nullptr;
        if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                               reinterpret_cast<LPCSTR>(static_cast<uintptr_t>(address)), &module) && module) {
            char path[MAX_PATH] = {0};
            GetModuleFileNameA(module, path, MAX_PATH);
            const char * slash = std::strrchr(path, '\\');
            std::snprintf(text, sizeof(text), "%s+0x%llx", slash ? slash + 1 : path,
                          static_cast<unsigned long long>(address - reinterpret_cast<uintptr_t>(module)));
            return text;
        }
        std::snprintf(text, sizeof(text), "0x%llx", static_cast<unsigned long long>(address));
        return text;
    }

private:
    bool loaded_ = false;
    uintptr_t base_ = 0;
    uintptr_t text_begin_ = 0;
    uintptr_t text_end_ = 0;
    std::vector<std::pair<uintptr_t, std::string>> symbols_;
};

static const int kMaxDumpedThreads = 64;
static const int kStackCandidates = 12;
static const size_t kStackScanBytes = 16 * 1024;

struct ThreadSample {
    DWORD tid = 0;
    bool sampled = false;
    unsigned long long cpu_100ns = 0;
    DWORD64 rip = 0;
    int candidate_count = 0;
    DWORD64 candidates[kStackCandidates] = {};
};

// Every other thread of this process: CPU time, instruction pointer, and the first return-address
// candidates on its stack that fall inside this executable. Each thread is suspended only while its
// context and stack are read (kernel calls and a fixed buffer, no heap and no CRT lock), then resumed
// before anything is printed. CPU time that does not grow between two reports means a blocked thread;
// growing time means a spin.
static void dump_threads(ExeSymbols & symbols, std::vector<std::pair<DWORD, unsigned long long>> & previous_cpu)
{
    symbols.load();
    std::vector<DWORD> tids;
    tids.reserve(kMaxDumpedThreads);
    const HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snapshot != INVALID_HANDLE_VALUE) {
        THREADENTRY32 entry;
        entry.dwSize = sizeof(entry);
        const DWORD process_id = GetCurrentProcessId();
        for (BOOL more = Thread32First(snapshot, &entry); more && static_cast<int>(tids.size()) < kMaxDumpedThreads;
             more = Thread32Next(snapshot, &entry)) {
            if (entry.th32OwnerProcessID == process_id && entry.th32ThreadID != GetCurrentThreadId()) {
                tids.push_back(entry.th32ThreadID);
            }
        }
        CloseHandle(snapshot);
    }

    std::vector<ThreadSample> samples(tids.size());
    static unsigned char stack_bytes[kStackScanBytes];
    for (size_t i = 0; i < tids.size(); ++i) {
        ThreadSample & sample = samples[i];
        sample.tid = tids[i];
        const HANDLE thread = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION,
                                         FALSE, tids[i]);
        if (!thread) {
            continue;
        }
        FILETIME created, exited, kernel, user;
        if (GetThreadTimes(thread, &created, &exited, &kernel, &user)) {
            const auto as_u64 = [](const FILETIME & time) {
                return (static_cast<unsigned long long>(time.dwHighDateTime) << 32) | time.dwLowDateTime;
            };
            sample.cpu_100ns = as_u64(kernel) + as_u64(user);
        }
        if (SuspendThread(thread) != static_cast<DWORD>(-1)) {
            CONTEXT context;
            std::memset(&context, 0, sizeof(context));
            context.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
            if (GetThreadContext(thread, &context)) {
                sample.sampled = true;
                sample.rip = context.Rip;
                SIZE_T got = 0;
                size_t have = 0;
                // Page by page, so a stack that ends early still yields what was readable.
                for (size_t offset = 0; offset < kStackScanBytes; offset += 4096) {
                    if (!ReadProcessMemory(GetCurrentProcess(),
                                           reinterpret_cast<LPCVOID>(static_cast<uintptr_t>(context.Rsp + offset)),
                                           stack_bytes + offset, 4096, &got) || got != 4096) {
                        break;
                    }
                    have = offset + 4096;
                }
                for (size_t at = 0; at + sizeof(DWORD64) <= have && sample.candidate_count < kStackCandidates;
                     at += sizeof(DWORD64)) {
                    DWORD64 value;
                    std::memcpy(&value, stack_bytes + at, sizeof(value));
                    if (value >= symbols.text_begin() && value < symbols.text_end()) {
                        sample.candidates[sample.candidate_count++] = value;
                    }
                }
            }
            ResumeThread(thread);
        }
        CloseHandle(thread);
    }

    std::fprintf(stderr, "AVX_PARITY_HELPER_STALL threads count=%d stack=\"return-address candidates (stack scan, not unwound)\"\n",
                 static_cast<int>(samples.size()));
    std::vector<std::pair<DWORD, unsigned long long>> current_cpu;
    for (const ThreadSample & sample : samples) {
        long long delta_ms = -1;
        for (const auto & seen : previous_cpu) {
            if (seen.first == sample.tid) {
                delta_ms = static_cast<long long>((sample.cpu_100ns - seen.second) / 10000ull);
            }
        }
        current_cpu.emplace_back(sample.tid, sample.cpu_100ns);
        std::string stack;
        for (int c = 0; c < sample.candidate_count; ++c) {
            if (!stack.empty()) {
                stack += " < ";
            }
            stack += symbols.describe(sample.candidates[c]);
        }
        std::fprintf(stderr,
                     "AVX_PARITY_HELPER_STALL thread tid=%lu main=%s cpu_ms=%llu cpu_ms_since_last_report=%lld "
                     "rip=%s stack=[%s]\n",
                     static_cast<unsigned long>(sample.tid),
                     sample.tid == g_main_thread_id ? "yes" : "no",
                     sample.cpu_100ns / 10000ull,
                     delta_ms,
                     sample.sampled ? symbols.describe(sample.rip).c_str() : "unsampled",
                     stack.c_str());
    }
    previous_cpu.swap(current_cpu);
}
#endif

// Watches the stage clock. When one stage outlasts the stall bound it prints, every bound, the last
// stage line, the clip's open/cache state and (on Windows) a thread dump, so a hang that the parent
// later kills has already said where it was. It never ends the run: the parent's timeout owns that.
class StallWatchdog {
public:
    static const int kMaxReports = 8;

    explicit StallWatchdog(long long stall_ms)
        : stall_ms_(stall_ms)
        , thread_([this] { run(); })
    {
    }

    ~StallWatchdog()
    {
        {
            std::lock_guard<std::mutex> lock(g_stage_mutex);
            stopping_ = true;
        }
        g_stage_changed.notify_all();
        thread_.join();
    }

private:
    void run()
    {
        unsigned watched_serial = 0;
        int reports_this_stage = 0;
        int reports = 0;
#ifdef _WIN32
        ExeSymbols symbols;
        std::vector<std::pair<DWORD, unsigned long long>> previous_cpu;
#endif
        std::unique_lock<std::mutex> lock(g_stage_mutex);
        while (!stopping_ && reports < kMaxReports) {
            g_stage_changed.wait_for(lock, std::chrono::milliseconds(100));
            if (stopping_) {
                break;
            }
            if (g_stage_serial != watched_serial) {
                watched_serial = g_stage_serial;
                reports_this_stage = 0;
            }
            const long long age_ms = g_helper_clock.elapsed() - g_stage_started_ms;
            if (watched_serial == 0 || age_ms < stall_ms_ * (reports_this_stage + 1)) {
                continue;
            }
            const std::string stage_line = g_stage_line;
            ++reports_this_stage;
            ++reports;
            lock.unlock();

            std::fprintf(stderr, "AVX_PARITY_HELPER_STALL report=%d stage_age_ms=%lld stall_ms=%lld last_stage=\"%s\"\n",
                         reports, age_ms, stall_ms_, stage_line.c_str());
            std::fprintf(stderr, "%s\n", probe_line().c_str());
#ifdef _WIN32
            dump_threads(symbols, previous_cpu);
#endif
            std::fflush(stderr);
            lock.lock();
        }
    }

    const long long stall_ms_;
    bool stopping_ = false;
    std::thread thread_;
};

// A healthy run takes about 15 s in all (both clips); one stage taking a minute is already a stall.
static long long stall_bound_ms()
{
    const QByteArray value = qgetenv("AVX_PARITY_HELPER_STALL_MS");
    bool ok = false;
    const long long parsed = value.toLongLong(&ok);
    return ok && parsed > 0 ? parsed : 60 * 1000;
}

// Test-only fault injection, so the runner tests can drive the real open-clip window into a hang.
// "cache-worker-start-pause" parks every cache worker that openMlvClip starts before it runs, so
// the fixture's disableMlvCaching waits for them forever: the same "open-clip and then silence" as
// the CI hang. Returns false for an unknown fault name.
static bool apply_fault_injection()
{
    const QByteArray fault = qgetenv("AVX_PARITY_HELPER_FAULT");
    if (fault.isEmpty()) {
        return true;
    }
    if (fault == "cache-worker-start-pause") {
        mlvCacheSetWorkerStartPauseForTesting(1);
        note_stage("fault-injected", QString::fromLatin1(fault));
        return true;
    }
    note_stage("error", QStringLiteral("unknown AVX_PARITY_HELPER_FAULT=%1").arg(QString::fromLatin1(fault)));
    return false;
}

static QJsonObject render_clip_hashes(const QString & clip_relative_path,
                                      const QString & receipt_relative_path,
                                      QString * error_message)
{
    MlvPipelineFixture fixture;
    const ProbedFixtureScope probed(&fixture);
    note_stage("open-clip", clip_relative_path);
    if (!fixture.openClipFile(repo_file_path(clip_relative_path), error_message)) {
        return QJsonObject();
    }
    note_stage("load-receipt", receipt_relative_path);
    if (!fixture.loadReceipt(receipt_relative_path, error_message)) {
        return QJsonObject();
    }
    note_stage("apply-receipt");
    if (!fixture.applyReceipt(error_message)) {
        return QJsonObject();
    }

    note_stage("render", QStringLiteral("frame0_16"));
    const std::vector<uint16_t> frame0_16 = fixture.renderFrame16(0, 1);
    note_stage("render", QStringLiteral("frame1_16"));
    const std::vector<uint16_t> frame1_16 = fixture.renderFrame16(1, 1);
    note_stage("render", QStringLiteral("frame0_8"));
    const std::vector<uint8_t> frame0_8 = fixture.renderFrame8(0, 1);
    note_stage("render", QStringLiteral("frame1_8"));
    const std::vector<uint8_t> frame1_8 = fixture.renderFrame8(1, 1);

    QJsonObject object;
    object.insert(QStringLiteral("width"), fixture.width());
    object.insert(QStringLiteral("height"), fixture.height());
    object.insert(QStringLiteral("frame0_16"),
                  QString::fromStdString(sha256_bytes(frame0_16.data(), frame0_16.size() * sizeof(uint16_t))));
    object.insert(QStringLiteral("frame1_16"),
                  QString::fromStdString(sha256_bytes(frame1_16.data(), frame1_16.size() * sizeof(uint16_t))));
    object.insert(QStringLiteral("frame0_8"),
                  QString::fromStdString(sha256_bytes(frame0_8.data(), frame0_8.size())));
    object.insert(QStringLiteral("frame1_8"),
                  QString::fromStdString(sha256_bytes(frame1_8.data(), frame1_8.size())));
    // The fixture's destructor (freeMlvObject) runs after this line.
    note_stage("teardown", clip_relative_path);
    return object;
}

int main(int argc, char ** argv)
{
    g_helper_clock.start();
#ifdef _WIN32
    g_main_thread_id = GetCurrentThreadId();
#endif
    test_runtime::force_single_threaded_pipeline();
    QCoreApplication app(argc, argv);
    const StallWatchdog watchdog(stall_bound_ms());

    QString output_path;
    for (int i = 1; i < argc; ++i) {
        const QString argument = QString::fromLocal8Bit(argv[i]);
        if (argument == QStringLiteral("--output") && (i + 1) < argc) {
            output_path = QString::fromLocal8Bit(argv[++i]);
        }
    }

    if (output_path.isEmpty()) {
        return 2;
    }
    if (!apply_fault_injection()) {
        return 6;
    }

    QString error_message;
    QJsonObject root;
    root.insert(QStringLiteral("build_avx"),
#ifdef MLVAPP_BUILD_AVX
                true
#else
                false
#endif
    );

    root.insert(QStringLiteral("tiny_dual_iso"),
                render_clip_hashes(QStringLiteral("tests/fixtures/clips/tiny_dual_iso.mlv"),
                                   QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"),
                                   &error_message));
    if (!error_message.isEmpty()) {
        note_stage("error", error_message);
        return 3;
    }

    root.insert(QStringLiteral("large_dual_iso"),
                render_clip_hashes(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv"),
                                   QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"),
                                   &error_message));
    if (!error_message.isEmpty()) {
        note_stage("error", error_message);
        return 4;
    }

    note_stage("write-output", output_path);
    QFile output(output_path);
    if (!output.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) {
        note_stage("error", output.errorString());
        return 5;
    }
    output.write(QJsonDocument(root).toJson(QJsonDocument::Indented));
    output.close();
    note_stage("done");
    return 0;
}
