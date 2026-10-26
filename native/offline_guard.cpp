// 本地离线研究守卫；只管理宿主已创建并挂起的一个进程。
// 来源：D:/Games/Launcher source code/server2697/client_loopback_guard.cpp。
// 来源指纹及本次构建记录见 rebuild/evidence/offline-guard-build.json。
// 改动：全 IPv4/IPv6 收发拒绝、创建时间绑定、只清理已核实身份的子进程。
// 不修改防火墙配置文件、网卡或持久过滤器；动态 WFP 会话关闭即撤销规则。
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#include <windows.h>
#include <fwpmu.h>
#include <rpc.h>
#include <filesystem>
#include <string>
#include <vector>
#include <stdexcept>
#include <cstdint>
#include <cstdlib>
#include <cwchar>
#include <limits>
#include <algorithm>

namespace fs = std::filesystem;
// MinGW 头省略这些 WFP 常量；逐项取自本机 Windows SDK 10.0.26100.0。
// GUID 来源：um/fwpmu.h；动态会话标志来源：shared/fwpmtypes.h。
static constexpr UINT32 CODOL_SESSION_DYNAMIC = 0x00000001;
static const GUID CODOL_APP_ID = {0xd78e1e87, 0x8644, 0x4ea5, {0x94,0x37,0xd8,0x09,0xec,0xef,0xc9,0x71}};
static const GUID CODOL_CONNECT_V4 = {0xc38d57d1, 0x05a7, 0x4c33, {0x90,0x4f,0x7f,0xbc,0xee,0xe6,0x0e,0x82}};
static const GUID CODOL_CONNECT_V6 = {0x4a72393b, 0x319f, 0x44bc, {0x84,0xc3,0xba,0x54,0xdc,0xb3,0xb6,0xb4}};
static const GUID CODOL_RECEIVE_V4 = {0xe1cd9fe7, 0xf4b5, 0x4273, {0x96,0xc0,0x59,0x2e,0x48,0x7b,0x86,0x50}};
static const GUID CODOL_RECEIVE_V6 = {0xa3b42c97, 0x9f04, 0x4672, {0xb8,0x7e,0xce,0xe9,0xc4,0x83,0x25,0x7f}};

static void require(bool ok, const char* what) {
    if (!ok) throw std::runtime_error(std::string(what) + " win32=" + std::to_string(GetLastError()));
}
static void wfp(DWORD status, const char* what) {
    if (status) throw std::runtime_error(std::string(what) + " status=" + std::to_string(status));
}
static std::string json_escape(const std::string& value) {
    std::string result;
    const char* hex = "0123456789abcdef";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { result += '\\'; result += static_cast<char>(c); }
        else if (c < 0x20) { result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15]; }
        else result += static_cast<char>(c);
    }
    return result;
}
static uint64_t parse_unsigned(const wchar_t* value, const char* what) {
    if (!value || !*value) throw std::runtime_error(what);
    uint64_t result = 0;
    for (const wchar_t* p = value; *p; ++p) {
        if (*p < L'0' || *p > L'9') throw std::runtime_error(what);
        unsigned digit = static_cast<unsigned>(*p - L'0');
        if (result > (std::numeric_limits<uint64_t>::max() - digit) / 10) throw std::runtime_error(what);
        result = result * 10 + digit;
    }
    return result;
}
static uint64_t process_creation(HANDLE process) {
    FILETIME creation{}, exit{}, kernel{}, user{};
    require(GetProcessTimes(process, &creation, &exit, &kernel, &user) != FALSE, "Get target creation time");
    return (static_cast<uint64_t>(creation.dwHighDateTime) << 32) | creation.dwLowDateTime;
}
static bool is_descendant(const fs::path& parent, const fs::path& target) {
    auto a = parent.begin(), b = target.begin();
    for (; a != parent.end(); ++a, ++b) {
        if (b == target.end() || _wcsicmp(a->c_str(), b->c_str()) != 0) return false;
    }
    return b != target.end();
}

// 仅放行本守卫所属项目中的固定版本槽位；不能由调用方指定任意公共 EXE 根。
static bool is_reusable_target(const fs::path& client_root, const fs::path& run,
                               const fs::path& target) {
    const fs::path sessions = client_root / L"workspaces" / L"offline-runs";
    const fs::path programs = client_root / L"workspaces" / L"runtime-programs";
    if (run.parent_path() != sessions || target.filename() != L"codol1649370-offline.exe") return false;
    const fs::path runtime = target.parent_path();
    const fs::path slot = runtime.parent_path();
    const fs::path revision = slot.parent_path();
    if (runtime.filename() != L"runtime" || revision.parent_path() != programs) return false;
    const std::wstring revision_name = revision.filename().wstring();
    const std::wstring slot_name = slot.filename().wstring();
    return revision_name.size() == 64 &&
        std::all_of(revision_name.begin(), revision_name.end(), [](wchar_t character) {
            return (character >= L'0' && character <= L'9') || (character >= L'a' && character <= L'f');
        }) && slot_name.size() >= 9 && slot_name.substr(0, 5) == L"slot-" &&
        std::all_of(slot_name.begin() + 5, slot_name.end(), [](wchar_t character) {
            return character >= L'0' && character <= L'9';
        });
}
static void write_new(const fs::path& path, const std::string& text) {
    // 只新建本次会话证据，不覆盖已有运行记录。
    HANDLE file = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                              CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    require(file != INVALID_HANDLE_VALUE, "Create guard state");
    DWORD written = 0;
    const BOOL ok = WriteFile(file, text.data(), static_cast<DWORD>(text.size()), &written, nullptr);
    const DWORD error = GetLastError();
    const BOOL flushed = ok && written == text.size() ? FlushFileBuffers(file) : FALSE;
    CloseHandle(file);
    if (!ok || written != text.size() || !flushed) {
        SetLastError(error);
        throw std::runtime_error("Could not save complete guard state");
    }
}

int wmain(int argc, wchar_t** argv) {
    // 参数：run_directory pid absolute_exe_path creation_FILETIME [bound_seconds]。
    // 时限 0 表示等待宿主 done 标记或子进程退出；有限时限允许 1..3660 秒。
    if (argc != 5 && argc != 6) return 2;
    fs::path run, target;
    DWORD pid = 0;
    uint64_t creation = 0, bound_seconds = 120;
    try {
        const uint64_t parsed_pid = parse_unsigned(argv[2], "Invalid PID");
        if (!parsed_pid || parsed_pid > MAXDWORD) throw std::runtime_error("Invalid PID");
        pid = static_cast<DWORD>(parsed_pid);
        creation = parse_unsigned(argv[4], "Invalid creation FILETIME");
        if (!creation) throw std::runtime_error("Invalid creation FILETIME");
        if (argc == 6) bound_seconds = parse_unsigned(argv[5], "Invalid time bound");
        if (bound_seconds > 3660) throw std::runtime_error("Invalid time bound");
        run = fs::canonical(fs::path(argv[1]));
        target = fs::canonical(fs::path(argv[3]));
        wchar_t guard_filename[32768];
        const DWORD filename_length = GetModuleFileNameW(nullptr, guard_filename, 32768);
        require(filename_length > 0 && filename_length < 32768, "Read guard path");
        const fs::path client_root = fs::canonical(fs::path(guard_filename)).parent_path().parent_path();
        if (!fs::is_directory(run) || !fs::is_regular_file(target) ||
            (!is_descendant(run, target) && !is_reusable_target(client_root, run, target)))
            throw std::runtime_error("Target must be inside this run or a project runtime slot");
        for (const wchar_t* name : {L"guard-ready.json", L"guard-result.json", L"guard-done.json"})
            if (fs::exists(run / name)) throw std::runtime_error("Run already has guard state");
    } catch (const std::exception&) { return 2; }

    HANDLE process = nullptr, job = nullptr, engine = nullptr;
    FWP_BYTE_BLOB* app = nullptr;
    bool owned = false;
    std::vector<UINT64> filter_ids;
    std::string failure;
    try {
        process = OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA |
                              PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, pid);
        require(process != nullptr, "Open target process");
        wchar_t image[32768]; DWORD length = 32768;
        require(QueryFullProcessImageNameW(process, 0, image, &length) != FALSE, "Query target image");
        const fs::path actual = fs::canonical(fs::path(image));
        if (_wcsicmp(actual.c_str(), target.c_str()) != 0)
            throw std::runtime_error("Target image path mismatch");
        if (process_creation(process) != creation)
            throw std::runtime_error("Target creation time mismatch");
        require(WaitForSingleObject(process, 0) == WAIT_TIMEOUT, "Target already exited");
        // 仅路径、创建时间和存活状态全部符合，之后才允许结束这个进程。
        owned = true;
        job = CreateJobObjectW(nullptr, nullptr);
        require(job != nullptr, "Create target job");
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
        limits.BasicLimitInformation.ActiveProcessLimit = 1;
        require(SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)) != FALSE, "Set target job limits");
        require(AssignProcessToJobObject(job, process) != FALSE, "Assign target job");

        FWPM_SESSION0 session{};
        session.displayData.name = const_cast<wchar_t*>(L"CODOL temporary offline research");
        session.flags = CODOL_SESSION_DYNAMIC;
        wfp(FwpmEngineOpen0(nullptr, RPC_C_AUTHN_WINNT, nullptr, &session, &engine), "Open dynamic WFP session");
        wfp(FwpmGetAppIdFromFileName0(target.c_str(), &app), "Get target application ID");
        GUID sub_key{};
        RPC_STATUS uuid_status = UuidCreate(&sub_key);
        if (uuid_status != RPC_S_OK && uuid_status != RPC_S_UUID_LOCAL_ONLY)
            throw std::runtime_error("Create WFP sublayer UUID failed");
        FWPM_SUBLAYER0 sub{};
        sub.subLayerKey = sub_key;
        sub.weight = 0xffff;
        sub.displayData.name = const_cast<wchar_t*>(L"CODOL per-image offline boundary");
        wfp(FwpmSubLayerAdd0(engine, &sub, nullptr), "Add temporary WFP sublayer");

        FWPM_FILTER_CONDITION0 condition{};
        condition.fieldKey = CODOL_APP_ID;
        condition.matchType = FWP_MATCH_EQUAL;
        condition.conditionValue.type = FWP_BYTE_BLOB_TYPE;
        condition.conditionValue.byteBlob = app;
        const GUID layers[] = {CODOL_CONNECT_V4, CODOL_CONNECT_V6,
                               CODOL_RECEIVE_V4, CODOL_RECEIVE_V6};
        wfp(FwpmTransactionBegin0(engine, 0), "Begin WFP filter transaction");
        for (const GUID& layer : layers) {
            FWPM_FILTER0 filter{};
            filter.displayData.name = const_cast<wchar_t*>(L"CODOL deny all IPv4/IPv6 traffic");
            filter.layerKey = layer;
            filter.subLayerKey = sub_key;
            filter.weight.type = FWP_UINT8;
            filter.weight.uint8 = 15;
            filter.numFilterConditions = 1;
            filter.filterCondition = &condition;
            filter.action.type = FWP_ACTION_BLOCK;
            UINT64 id = 0;
            wfp(FwpmFilterAdd0(engine, &filter, nullptr, &id), "Add offline block filter");
            filter_ids.push_back(id);
        }
        wfp(FwpmTransactionCommit0(engine), "Commit all four offline filters");
        // ready 只包含数值和固定字符串，避免路径转义或编码问题。
        std::string ready = "{\"guard_pid\":" + std::to_string(GetCurrentProcessId()) +
            ",\"child_pid\":" + std::to_string(pid) + ",\"creation_filetime\":\"" + std::to_string(creation) +
            "\",\"filter_ids\":[";
        for (size_t i = 0; i < filter_ids.size(); ++i)
            ready += (i ? "," : "") + std::to_string(filter_ids[i]);
        ready += "],\"dynamic\":true,\"job_kill_on_close\":true,\"max_processes\":1,\"bound_seconds\":" +
            std::to_string(bound_seconds) + ",\"scope\":\"offline-deny-all\",\"identity_verified\":true}";
        write_new(run / L"guard-ready.json", ready);
        const ULONGLONG deadline = GetTickCount64() + bound_seconds * 1000;
        while ((bound_seconds == 0 || GetTickCount64() < deadline) &&
               !fs::exists(run / L"guard-done.json") && WaitForSingleObject(process, 0) == WAIT_TIMEOUT)
            Sleep(100);
    } catch (const std::exception& error) { failure = error.what(); }

    // 先结束并确认子进程退出，再关闭动态网络过滤；身份不匹配时绝不碰该进程。
    bool terminated = !owned;
    if (owned && process) {
        if (WaitForSingleObject(process, 0) == WAIT_TIMEOUT) {
            if (!TerminateProcess(process, 0xd1a6) && failure.empty()) failure = "Terminate target failed";
        }
        terminated = WaitForSingleObject(process, 3000) == WAIT_OBJECT_0;
    }
    if (job) CloseHandle(job); // 第二道保障：关闭 job 同样结束其唯一成员。
    if (owned && process && !terminated) terminated = WaitForSingleObject(process, 3000) == WAIT_OBJECT_0;
    if (!terminated && failure.empty()) failure = "Target exit was not confirmed";
    if (app) FwpmFreeMemory0(reinterpret_cast<void**>(&app));
    const DWORD closed = engine ? FwpmEngineClose0(engine) : 0;
    if (process) CloseHandle(process);
    const std::string result = "{\"closed\":true,\"engine_close_status\":" + std::to_string(closed) +
        ",\"owned_identity_verified\":" + (owned ? "true" : "false") +
        ",\"child_terminated\":" + (owned && terminated ? "true" : "false") +
        ",\"error\":\"" + json_escape(failure) + "\"}";
    try { write_new(run / L"guard-result.json", result); }
    catch (const std::exception&) { return 1; }
    return failure.empty() && closed == 0 && owned && terminated ? 0 : 1;
}

