// 守卫路径边界回归：只新增项目内完整、固定层级的程序槽位。
#define wmain guard_entry_for_test
#include "../../native/offline_guard.cpp"
#undef wmain
#include <cassert>

int main() {
    const fs::path root = L"D:\\Games\\CODOL_Final\\client";
    const fs::path run = root / L"workspaces/offline-runs/session-1";
    const fs::path revision = root / L"workspaces/runtime-programs" / std::wstring(64, L'a');
    const fs::path target = revision / L"slot-0001/runtime/codol1649370-offline.exe";
    assert(is_reusable_target(root, run, target));
    assert(!is_reusable_target(root, run / L"nested", target));
    assert(!is_reusable_target(root, root / L"other/session-1", target));
    assert(!is_reusable_target(root, run, revision / L"slot-0001/runtime/other.exe"));
    assert(!is_reusable_target(root, run, revision / L"slot-0001/other/codol1649370-offline.exe"));
    assert(!is_reusable_target(root, run, revision / L"slot-evil/runtime/codol1649370-offline.exe"));
    assert(!is_reusable_target(root, run, root / L"workspaces/runtime-programs/short/slot-0001/runtime/codol1649370-offline.exe"));
    assert(!is_reusable_target(root, run, root / L"other" / target.filename()));
    assert(is_descendant(run, run / L"runtime/codol1649370-offline.exe"));
    return 0;
}
