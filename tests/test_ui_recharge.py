"""充值 UI 字节码、原生余额和真实 SQLite 提交的独立验证。"""
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest

from codol_rebuild.adapters.game.v1649370 import ui_movie, ui_recharge
from codol_rebuild.adapters.game.v1649370.profile import currency_snapshot, publish_currency
from codol_rebuild.adapters.persistence.sqlite_accounts import AccountRepository, RevisionConflict
from codol_rebuild.application.accounts.purchase import RECHARGE_AMOUNT, credit_currency
from codol_rebuild.application.accounts.service import AccountSession

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"research"))
from trace_matchmaking_debugline import Reader, abc_parse, disassemble


def account_methods(movie):
    r = Reader(movie)
    r.p = 8 + (5 + 4*(movie[8] >> 3) + 7)//8 + 4
    while r.p < len(movie):
        h = struct.unpack("<H", r.raw(2))[0]
        kind, size = h >> 6, h & 63
        if size == 63:
            size = struct.unpack("<I", r.raw(4))[0]
        p = r.raw(size)
        if kind != 82:
            continue
        a = Reader(p)
        a.raw(4)
        if a.cstr() == "codo/component/PlayerAccountInfo":
            return abc_parse(p[a.p:])
    raise AssertionError("movie has no PlayerAccountInfo")


class Memory:
    def __init__(self, cp=100000):
        self.data = bytearray(11*32)
        publish_currency(self.read, self.write, {"gp": 2000000000, "cp": cp})

    def read(self, a, n):
        return bytes(self.data[a-0xC8E4078:a-0xC8E4078+n])

    def write(self, a, b):
        self.data[a-0xC8E4078:a-0xC8E4078+len(b)] = b


class RechargeTests(unittest.TestCase):
    def test_native_identity_and_complete_movie_compression(self):
        import pefile
        import zlib
        pe = pefile.PE(str(ROOT/"client/bin/v1649370/codol1649370-offline.exe"))
        ui_recharge.validate_contract(lambda a, n: pe.get_data(a-0x400000, n))
        with self.assertRaises(ValueError):
            ui_recharge.validate_contract(lambda a, n: bytes(n))
        movie = (ROOT/"client/workspaces/ui-investigation/codo.gfx").read_bytes()
        self.assertLessEqual(len(zlib.compress(ui_movie.patch_movie(movie), 9)), ui_movie.COMPRESSED_SIZE)

    def test_patched_click_execution_success_failure_and_repeat(self):
        movie = (ROOT/"client/workspaces/ui-investigation/codo.gfx").read_bytes()
        methods, names, strings = account_methods(ui_movie.patch_movie(movie))
        click = next(m for m in methods if m.get("trait", "").endswith("::onReplenishCPClick"))
        rows = disassemble(click, names, strings)
        offsets = {r["offset"]: i for i, r in enumerate(rows)}
        for returned in (101000, 102000, 2000001000, -1):
            stack, calls, displayed, position, peak = [], [], [], 0, 0
            obj = {"myCP": {"setData": lambda x: displayed.append(x)},
                   "positionAssets": lambda: calls.append("layout")}
            scope = {"CFunctionCommonContainer": {"OpenWebPage": lambda x: calls.append(x) or returned}}
            for _ in range(100):
                row = rows[position]
                position += 1
                op, args = row["instruction"], row["args"]
                if op == "getlocal0":
                    stack.append(obj)
                elif op in ("pushscope", "pop"):
                    stack.pop()
                elif op == "getlex":
                    stack.append(scope[args[0].rsplit("::", 1)[-1]])
                elif op == "getproperty":
                    stack.append(stack.pop()[args[0].rsplit("::", 1)[-1]])
                elif op == "pushbyte":
                    stack.append(args[0])
                elif op == "dup":
                    stack.append(stack[-1])
                elif op == "swap":
                    stack[-1], stack[-2] = stack[-2], stack[-1]
                elif op == "iflt":
                    right, left = stack.pop(), stack.pop()
                    if left < right:
                        position = offsets[args[0]]
                elif op in ("callproperty", "callpropvoid"):
                    name, count = args
                    values = stack[-count:] if count else []
                    if count:
                        del stack[-count:]
                    receiver = stack.pop()
                    key = name.rsplit("::", 1)[-1]
                    value = str(receiver) if key == "toString" else receiver[key](*values)
                    if op == "callproperty":
                        stack.append(value)
                elif op == "returnvoid":
                    self.assertFalse(stack)
                    break
                else:
                    self.fail(op)
                peak = max(peak, len(stack))
            else:
                self.fail("click never returned")
            self.assertLessEqual(peak, click["limits"][0])
            self.assertEqual(calls, [1, "layout"] if returned >= 0 else [1])
            self.assertEqual(displayed, [str(returned)] if returned >= 0 else [])
        # Crafting uses its original callback and remains byte-for-byte intact.
        original, _, _ = account_methods(movie)
        for name in ("onCraftingClicked", "switchShowCurrencyMode"):
            before = next(m for m in original if m.get("trait", "").endswith("::"+name))
            after = next(m for m in methods if m.get("trait", "").endswith("::"+name))
            self.assertEqual(before["code"], after["code"])
        # No URL check in the patched enabled expression.
        bind = next(m for m in methods if m.get("trait") == "::bindAccountInfo")
        self.assertEqual(bytes.fromhex(bind["code"])[129:149], b"\x26"+b"\x02"*19)

    def test_twenty_clicks_durable_reload_and_sqlite_conflict_rollback(self):
        class Codec:
            def migrate(self, profiles):
                """充值用例的格式替身不需要迁移档案。"""
                return dict(profiles)
            identities = {"public": {}, "secure": {}}
            def validate_snapshot(self, blobs):
                return 570
        folder = ROOT/"verification/client/workspaces"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            repo = AccountRepository(Path(tmp)/"accounts.sqlite3")
            blobs = {"public": b"public", "secure": b"secure"}
            state = {"version": 1, "owned_ids": [570], "gp": 2000000000, "cp": 100000}
            repo.identity("local")
            repo.save("local", 0, blobs, Codec.identities, initialization=state)
            session = AccountSession(repo, Codec())
            session.initialize()
            mem = Memory()
            # 充值数量与上限规则由应用层会话计算，UI 适配只做原生发布与补偿。
            def session_plan(before):
                return {**before, "cp": session.credit_state("cp", RECHARGE_AMOUNT, before["cp"])["cp"]}
            for index in range(20):
                result = ui_recharge.credit(mem.read, mem.write, session_plan,
                    lambda b: session.save(blobs, balances=b))
                self.assertEqual(result, {"gp": 2000000000, "cp": 101000+1000*index})
            restarted = AccountSession(AccountRepository(Path(tmp)/"accounts.sqlite3"), Codec())
            self.assertEqual(restarted.initialize()["initialization"]["cp"], 120000)
            repo.save("local", session.account["revision"], blobs, Codec.identities,
                      initialization=session.account["initialization"])
            before = bytes(mem.data)
            with self.assertRaises(RevisionConflict):
                ui_recharge.credit(mem.read, mem.write, session_plan,
                    lambda b: session.save(blobs, balances=b))
            self.assertEqual(bytes(mem.data), before)
            self.assertEqual(repo.load("local", Codec.identities)["initialization"]["cp"], 120000)

    def test_limit_and_commit_failure_preserve_every_native_byte(self):
        def plan_for(cp):
            state = {"version": 1, "owned_ids": [570], "gp": 2000000000, "cp": cp}
            return lambda before: {**before, "cp": credit_currency(state, "cp", RECHARGE_AMOUNT, before["cp"])["cp"]}
        for cp in (2147482648, 2147483647):
            mem = Memory(cp)
            before = bytes(mem.data)
            with self.assertRaises(ValueError):
                ui_recharge.credit(mem.read, mem.write, plan_for(cp), lambda b: self.fail("must not save"))
            self.assertEqual(bytes(mem.data), before)
        mem = Memory(2147482647)
        self.assertEqual(ui_recharge.credit(mem.read, mem.write, plan_for(2147482647), lambda b: None)["cp"], 2147483647)
        # 应用层充值规则：上限拒绝与边界恰好加满。
        state = {"version": 1, "owned_ids": [570], "gp": 0, "cp": 100000}
        self.assertEqual(credit_currency(state, "cp", RECHARGE_AMOUNT, 2147482647)["cp"], 2147483647)
        with self.assertRaises(ValueError):
            credit_currency(state, "cp", RECHARGE_AMOUNT, 2147482648)
        with self.assertRaises(ValueError):
            credit_currency(state, "gp", RECHARGE_AMOUNT, 100000)
        mem = Memory()
        before = bytes(mem.data)
        def fail(b):
            raise sqlite3.OperationalError("disk full")
        with self.assertRaises(sqlite3.OperationalError):
            ui_recharge.credit(mem.read, mem.write, plan_for(100000), fail)
        self.assertEqual(bytes(mem.data), before)


if __name__ == "__main__":
    unittest.main()
