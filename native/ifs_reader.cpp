// 终版 x86 IFS2 成员读取器。宿主通过两个匿名管道逐次提交包名、成员名并接收分块正文。
#include <windows.h>
#include <cstdio>
#include <cstdint>
#include <fcntl.h>
#include <io.h>
#include <map>
#include <string>

using OpenArchive = HANDLE (__stdcall *)(const char*, int);
using OpenMember = bool (__stdcall *)(HANDLE, const char*, DWORD, HANDLE*, DWORD);

// 此版 DLL 的读取和释放成员入口使用寄存器传参，不是 Win32 导出接口。
static DWORD readMemberChunk(void* readAddress, HANDLE member, char* destination, DWORD capacity) {
    DWORD received = 0;
    DWORD* resultPointer = &received;
    __asm {
        push 1
        push resultPointer
        push capacity
        mov ecx, member
        mov edx, destination
        call readAddress
    }
    return received;
}

static void closeMember(void* closeAddress, HANDLE member) {
    __asm {
        push esi
        mov esi, member
        call closeAddress
        pop esi
    }
}

static bool sendSize(DWORD size) {
    return fwrite(&size, sizeof(size), 1, stdout) == 1;
}

static bool readName(std::string& name, DWORD length) {
    if (!length || length > 32767) return false;
    name.resize(length);
    return fread(name.data(), 1, length, stdin) == length && name.find('\0') == std::string::npos;
}

int wmain(int argumentCount, wchar_t** arguments) {
    if (argumentCount != 2) return 2;
    auto library = LoadLibraryExW(arguments[1], nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!library) return 3;
    auto base = reinterpret_cast<unsigned char*>(library);
    auto openArchive = reinterpret_cast<OpenArchive>(base + 0x163e0);
    auto openMember = reinterpret_cast<OpenMember>(base + 0x1fed0);
    if (_setmode(_fileno(stdin), _O_BINARY) == -1 || _setmode(_fileno(stdout), _O_BINARY) == -1) return 4;
    // 包内句柄随本次读取器进程统一释放，避免同一 IFS 每读一项都重新打开。
    std::map<std::string, HANDLE> archives;
    DWORD nameSizes[2];
    char data[65536];
    while (fread(nameSizes, sizeof(DWORD), 2, stdin) == 2) {
        std::string archiveName, memberName;
        if (!readName(archiveName, nameSizes[0]) || !readName(memberName, nameSizes[1])) return 5;
        if (archiveName.find_first_of("/\\:") != std::string::npos) return 5;
        auto& archive = archives[archiveName];
        if (!archive) archive = openArchive(archiveName.c_str(), 0);
        HANDLE member = nullptr;
        if (!archive || !openMember(archive, memberName.c_str(), 1, &member, 0) || !member) {
            if (!sendSize(MAXDWORD) || fflush(stdout)) return 6;
            continue;
        }
        for (;;) {
            DWORD received = readMemberChunk(base + 0x22570, member, data, sizeof(data));
            if (received > sizeof(data) || !sendSize(received)) return 7;
            if (!received) break;
            if (fwrite(data, 1, received, stdout) != received) return 8;
        }
        closeMember(base + 0x210a0, member);
        if (fflush(stdout)) return 9;
    }
    return ferror(stdin) ? 10 : 0;
}
