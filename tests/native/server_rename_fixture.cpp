// 联机改名原生执行夹具：替身函数检查异步等待、栈平衡、完成通知与回滚。
#include <windows.h>
#include <cstdio>
#include <cstring>
#include "server_rename_data.h"

extern "C" {
DWORD saved_stack, before_stack, after_stack, stack_padding, has_argument;
DWORD parameters = 0x12340000, saved_registers[7], returned_flags;
DWORD scenario, completion_count, setter_count, request_count, applied_count;
DWORD rollback_count, poll_count, error_count, setter_object;
void* entry_point;
char current_name[64];
}
static DWORD* state_words = reinterpret_cast<DWORD*>(0x21000000);
static char* staged_name = reinterpret_cast<char*>(0x21000008);
static char* previous_name = reinterpret_cast<char*>(0x21000048);

extern "C" void apply_name(const char* value) {
    ++setter_count;
    if (setter_object != 0x25000000) ++error_count;
    if (scenario != 3 || setter_count > 1) strcpy(current_name, value);
}
extern "C" __attribute__((naked)) void native_setter() {
    __asm__ __volatile__("movl %esi,_setter_object\n\tjmp _apply_name\n\t");
}
extern "C" void completed(DWORD success, const char* error, DWORD interrupted) {
    ++completion_count;
    if (success != DWORD(scenario == 0) || interrupted) ++error_count;
    if (scenario == 0 ? *error != 0 : strcmp(error, "failure") != 0) ++error_count;
}
extern "C" void __stdcall original(DWORD) { ++error_count; }
extern "C" void refresh() {}
extern "C" void initialize() {}

static LONG WINAPI on_trap(EXCEPTION_POINTERS* exception) {
    if (exception->ExceptionRecord->ExceptionCode != EXCEPTION_BREAKPOINT)
        return EXCEPTION_CONTINUE_SEARCH;
    DWORD address = reinterpret_cast<DWORD>(exception->ExceptionRecord->ExceptionAddress);
    CONTEXT* registers = exception->ContextRecord;
    if (registers->Ebp != registers->Esp) ++error_count;
    if (address == TRAP_REQUEST) {
        ++request_count;
        if (*reinterpret_cast<DWORD*>(registers->Ebp + 40) != parameters) ++error_count;
    } else if (address == TRAP_POLL) {
        ++poll_count;
        if (poll_count == 3 && scenario != 5) {
            strcpy(staged_name, "NewName");
            strcpy(previous_name, current_name);
            state_words[0] = 1;
            state_words[34] = scenario == 1 ? 2 : 1;
            state_words[35] = scenario < 4;
            strcpy(reinterpret_cast<char*>(0x21000090), "failure");
        }
    } else if (address == TRAP_APPLIED) {
        ++applied_count;
        if (scenario != 3 && strcmp(current_name, "NewName")) ++error_count;
        state_words[1] = scenario != 2 && scenario != 3;
    } else if (address == TRAP_ROLLED_BACK) {
        ++rollback_count;
        if (strcmp(current_name, "OldName")) ++error_count;
    } else {
        return EXCEPTION_CONTINUE_SEARCH;
    }
    registers->Eip = address + 1;
    return EXCEPTION_CONTINUE_EXECUTION;
}

extern "C" __attribute__((naked)) void exercise() {
    __asm__ __volatile__(
      "pushl %ebp\n\tpushl %ebx\n\tpushl %esi\n\tpushl %edi\n\tmovl %esp,_saved_stack\n\t"
      "subl _stack_padding,%esp\n\tmovl %esp,_before_stack\n\t"
      "cmpl $0,_has_argument\n\tje 1f\n\tpushl _parameters\n\t1:\n\t"
      "movl $0x11111111,%eax\n\tmovl $0x22222222,%ecx\n\tmovl $0x33333333,%edx\n\t"
      "movl $0xbbbbbbbb,%ebp\n\tmovl $0xeeeeeeee,%ebx\n\tmovl $0x55555555,%esi\n\tmovl $0x66666666,%edi\n\t"
      "pushl $0x246\n\tpopfl\n\tcall *_entry_point\n\tmovl %esp,_after_stack\n\t"
      "pushfl\n\tpopl _returned_flags\n\t"
      "movl %eax,_saved_registers\n\tmovl %ecx,_saved_registers+4\n\tmovl %edx,_saved_registers+8\n\tmovl %ebp,_saved_registers+12\n\t"
      "movl %ebx,_saved_registers+16\n\tmovl %esi,_saved_registers+20\n\tmovl %edi,_saved_registers+24\n\t"
      "movl _saved_stack,%esp\n\tpopl %edi\n\tpopl %esi\n\tpopl %ebx\n\tpopl %ebp\n\tret\n\t");
}

static void invoke(DWORD address, bool argument) {
    entry_point = reinterpret_cast<void*>(address);
    has_argument = argument;
    exercise();
    const DWORD expected_registers[] = {0x11111111,0x22222222,0x33333333,0xbbbbbbbb,0xeeeeeeee,0x55555555,0x66666666};
    if (before_stack != after_stack || (returned_flags & 0xcd5) != 0x44 ||
        memcmp(saved_registers, expected_registers, sizeof expected_registers)) ++error_count;
}
static void allocate_page(DWORD address) {
    if (VirtualAlloc(reinterpret_cast<void*>(address),0x10000,MEM_RESERVE|MEM_COMMIT,PAGE_EXECUTE_READWRITE)
            != reinterpret_cast<void*>(address)) {
        printf("allocation failed: %lx\n", address); ExitProcess(2);
    }
}
static void redirect(DWORD address, void* function) {
    auto instruction = reinterpret_cast<BYTE*>(address);
    instruction[0] = 0xe9;
    *reinterpret_cast<DWORD*>(instruction+1) = reinterpret_cast<DWORD>(function)-address-5;
}
int main() {
    const DWORD pages[] = {0x20000000,0x21000000,0x22000000,0x23000000,0x25000000,
        0x48490000,0x48260000,0x40690000,0x40770000,0x40dc0000,0x40da0000,0x405e0000};
    for (DWORD address : pages) allocate_page(address);
    memcpy(reinterpret_cast<void*>(0x20000000), helper_code, sizeof helper_code);
    redirect(N_SETTER,reinterpret_cast<void*>(&native_setter));
    redirect(N_COMPLETE,reinterpret_cast<void*>(&completed));
    redirect(N_HANDLER,reinterpret_cast<void*>(&original));
    redirect(N_REFRESH_INVENTORY,reinterpret_cast<void*>(&refresh));
    redirect(N_REFRESH_PREVIEW,reinterpret_cast<void*>(&refresh));
    redirect(0x23000000,reinterpret_cast<void*>(&initialize));
    *reinterpret_cast<DWORD*>(N_USERNAME)=0x25000000;
    *reinterpret_cast<DWORD*>(N_DW)=0x25000100;
    *reinterpret_cast<DWORD*>(0x22000000)=1;
    AddVectoredExceptionHandler(1,on_trap);
    unsigned case_count=0;
    for (scenario=0;scenario<7;++scenario) for (stack_padding=0;stack_padding<16;stack_padding+=4) {
        completion_count=setter_count=request_count=applied_count=rollback_count=poll_count=error_count=0;
        memset(state_words,0,4096); strcpy(current_name,"OldName");
        if (scenario<4) invoke(0x20000000,true);
        if (setter_count || completion_count) ++error_count;
        invoke(POLL_ENTRY,false); invoke(POLL_ENTRY,false);
        if (setter_count || completion_count) ++error_count;
        invoke(POLL_ENTRY,false); invoke(POLL_ENTRY,false);
        bool applies=scenario!=1 && scenario!=5;
        bool rolls_back=scenario==2 || scenario==3;
        if (completion_count!=DWORD(scenario<4) || applied_count!=DWORD(applies) ||
            rollback_count!=DWORD(rolls_back) || setter_count!=DWORD(applies)+DWORD(rolls_back) ||
            request_count!=DWORD(scenario<4) ||
            strcmp(current_name, applies&&!rolls_back?"NewName":"OldName")) ++error_count;
        if (error_count) {
            printf("{\"passed\":false,\"scenario\":%lu,\"padding\":%lu,\"errors\":%lu}\n",
                scenario,stack_padding,error_count);return 1;
        }
        ++case_count;
    }
    printf("{\"passed\":true,\"cases\":%u}\n",case_count);return 0;
}
