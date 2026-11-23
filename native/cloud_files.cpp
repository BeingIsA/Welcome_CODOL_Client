// 终版原包运行视图。Windows 首次请求时完整交付一个逻辑文件，原始资源始终按需读取。
#include <windows.h>
#include <cfapi.h>
#include <cstring>
#include <vector>
#include <exception>
#include <cwchar>

extern "C" __declspec(dllexport) HRESULT RegisterGameRoot(PCWSTR rootPath) {
    CF_SYNC_REGISTRATION registration{};
    registration.StructSize = sizeof(registration);
    registration.ProviderName = L"CODOL packed resources";
    registration.ProviderVersion = L"1";
    CF_SYNC_POLICIES policies{};
    policies.StructSize = sizeof(policies);
    policies.Hydration.Primary = CF_HYDRATION_POLICY_FULL;
    policies.Population.Primary = CF_POPULATION_POLICY_ALWAYS_FULL;
    return CfRegisterSyncRoot(rootPath, &registration, &policies, CF_REGISTER_FLAG_NONE);
}

// 进程被强制结束后注册仍可能存在。只撤销本组件在该目录上的注册。
extern "C" __declspec(dllexport) HRESULT UnregisterGameRoot(PCWSTR rootPath) {
    CF_SYNC_ROOT_STANDARD_INFO info{};
    HRESULT status = CfGetSyncRootInfoByPath(rootPath, CF_SYNC_ROOT_INFO_STANDARD, &info, sizeof(info), nullptr);
    if (status == HRESULT_FROM_WIN32(ERROR_NOT_A_CLOUD_FILE) ||
        status == HRESULT_FROM_WIN32(ERROR_CLOUD_FILE_NOT_UNDER_SYNC_ROOT)) return S_FALSE;
    if (FAILED(status)) return status;
    if (wcscmp(info.ProviderName, L"CODOL packed resources") != 0) return E_ACCESSDENIED;
    return CfUnregisterSyncRoot(rootPath);
}

struct GameMember {
    PCWSTR fileName;
    const char* relativePath;
    LONGLONG fileSize;
};

using ReadGameBytes = BOOL (CALLBACK *)(const char*, DWORD, LONGLONG, DWORD, void*);
using RecordTransfer = void (CALLBACK *)(const char*, DWORD, LONGLONG, LONGLONG, LONGLONG, LONG, BOOL);
struct ViewHandlers {
    ReadGameBytes read;
    RecordTransfer complete;
};

static void CALLBACK fetchGameBytes(const CF_CALLBACK_INFO* request, const CF_CALLBACK_PARAMETERS* parameters) {
    const auto& handlers = *static_cast<const ViewHandlers*>(request->CallbackContext);
    const auto processId = request->ProcessInfo ? request->ProcessInfo->ProcessId : 0;
    const auto byteCount = request->FileSize.QuadPart;
    CF_OPERATION_INFO operation{};
    operation.StructSize = sizeof(operation);
    operation.Type = CF_OPERATION_TYPE_TRANSFER_DATA;
    operation.ConnectionKey = request->ConnectionKey;
    operation.TransferKey = request->TransferKey;
    operation.RequestKey = request->RequestKey;
    CF_OPERATION_PARAMETERS response{};
    response.ParamSize = FIELD_OFFSET(CF_OPERATION_PARAMETERS, TransferData) + sizeof(response.TransferData);
    response.TransferData.Offset = parameters->FetchData.RequiredFileOffset;
    response.TransferData.Length = parameters->FetchData.RequiredLength;
    response.TransferData.CompletionStatus = static_cast<NTSTATUS>(0xc0000001);
    std::vector<BYTE> payload;
    auto identity = static_cast<const char*>(request->FileIdentity);
    try {
        if (byteCount > 0 && byteCount <= MAXDWORD && identity && request->FileIdentityLength &&
            identity[request->FileIdentityLength - 1] == '\0') {
            payload.resize(static_cast<size_t>(byteCount));
            if (handlers.read(identity, processId, 0, static_cast<DWORD>(byteCount), payload.data())) {
                response.TransferData.Offset.QuadPart = 0;
                response.TransferData.Length.QuadPart = byteCount;
                response.TransferData.Buffer = payload.data();
                response.TransferData.CompletionStatus = 0;
            }
        }
    } catch (const std::exception&) {
        response.TransferData.CompletionStatus = static_cast<NTSTATUS>(0xc0000017);
    }
    HRESULT status = CfExecute(&operation, &response);
    if (identity && request->FileIdentityLength && !identity[request->FileIdentityLength - 1]) {
        handlers.complete(identity, processId, parameters->FetchData.RequiredFileOffset.QuadPart,
                          parameters->FetchData.RequiredLength.QuadPart, byteCount, status,
                          response.TransferData.CompletionStatus == 0 && SUCCEEDED(status));
    }
}

extern "C" __declspec(dllexport) HRESULT ConnectGameView(PCWSTR rootPath, const ViewHandlers* handlers, CF_CONNECTION_KEY* connection) {
    const CF_CALLBACK_REGISTRATION callbacks[] = {
        {CF_CALLBACK_TYPE_FETCH_DATA, fetchGameBytes}, CF_CALLBACK_REGISTRATION_END
    };
    return CfConnectSyncRoot(rootPath, callbacks, handlers,
                             CF_CONNECT_FLAG_REQUIRE_PROCESS_INFO, connection);
}

extern "C" __declspec(dllexport) HRESULT AddGameMembers(PCWSTR directory, const GameMember* members, DWORD count) {
    std::vector<CF_PLACEHOLDER_CREATE_INFO> placeholders(count);
    FILETIME timestamp;
    GetSystemTimeAsFileTime(&timestamp);
    for (DWORD memberIndex = 0; memberIndex < count; ++memberIndex) {
        auto& placeholder = placeholders[memberIndex];
        const auto& member = members[memberIndex];
        placeholder.RelativeFileName = member.fileName;
        placeholder.FileIdentity = member.relativePath;
        placeholder.FileIdentityLength = static_cast<DWORD>(std::strlen(member.relativePath) + 1);
        placeholder.Flags = CF_PLACEHOLDER_CREATE_FLAG_MARK_IN_SYNC;
        placeholder.FsMetadata.FileSize.QuadPart = member.fileSize;
        auto& metadata = placeholder.FsMetadata.BasicInfo;
        metadata.FileAttributes = FILE_ATTRIBUTE_NORMAL;
        metadata.CreationTime.LowPart = timestamp.dwLowDateTime;
        metadata.CreationTime.HighPart = timestamp.dwHighDateTime;
        metadata.LastWriteTime = metadata.ChangeTime = metadata.LastAccessTime = metadata.CreationTime;
    }
    DWORD processed = 0;
    HRESULT status = CfCreatePlaceholders(directory, placeholders.data(), count, CF_CREATE_FLAG_STOP_ON_ERROR, &processed);
    if (FAILED(status)) return status;
    for (const auto& entry : placeholders) if (FAILED(entry.Result)) return entry.Result;
    return processed == count ? S_OK : E_FAIL;
}
