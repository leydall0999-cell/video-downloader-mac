# -*- coding: utf-8 -*-
"""baidu_contribute 离线测试：显式开关自动贡献共享池（2026-10-30）。

全离线：不联网（_post_inject 一律打桩）、不动真实 HOME（状态/池/配置/日志
全部重定向到 tmp）。只测状态机、分组、推送编排、占位符兜底与「绝不泄露明文」。
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SERVER = Path(__file__).resolve().parent.parent
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import baidu_contribute as bc  # noqa: E402

# 推送类测试共用的测试凭据（env 注入，避免碰真实令牌/真实配置）
ENV_TOKEN = {"VDL_BAIDU_CONTRIBUTION_URL": "http://pool.test",
             "VDL_BAIDU_CONTRIBUTION_TOKEN": "testtok"}


def _hdr(bduss: str, extra: int = 0) -> str:
    """造一条假百度 header：BDUSS=xxx; STOKEN=dummy; 再补 extra 个伪字段。"""
    segs = ["BDUSS=" + bduss, "STOKEN=dummy"] + ["K%d=v%d" % (i, i) for i in range(extra)]
    return "; ".join(segs)


class BaiduContributeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vdl_bc_test_"))
        self.pool = self.tmp / "cookie_pool"
        self.pool.mkdir()
        self._patchers = [
            mock.patch.object(bc, "_STATE", self.tmp / "baidu_contribute.json"),
            mock.patch.object(bc, "_CFG", self.tmp / "cloud_sync.json"),
            mock.patch.object(bc, "_POOL", self.pool / "baidu.com.json"),
            mock.patch.object(bc, "_LOG", self.tmp / "baidu_contribute.log"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        # 环境变量一律清掉，测「内置默认」分支（需要凭据的测试自行 patch.dict）
        os.environ.pop("VDL_BAIDU_CONTRIBUTION_URL", None)
        os.environ.pop("VDL_BAIDU_CONTRIBUTION_TOKEN", None)

    # -- 配置与占位符兜底 --------------------------------------------------

    def test_cfg_placeholder_means_no_credentials(self):
        self.assertEqual(bc._DEFAULT_TOKEN, "__CONTRIB_TOKEN__",
                         "仓库源码必须保持占位符（真实令牌只在构建 staging 注入）")
        url, tok = bc.contribution_cfg()
        self.assertEqual(tok, "", "未注入的占位符必须视为无凭据")
        self.assertTrue(url.startswith("http"))

    def test_cfg_env_overrides(self):
        with mock.patch.dict("os.environ", {"VDL_BAIDU_CONTRIBUTION_URL": "http://example.test/",
                                            "VDL_BAIDU_CONTRIBUTION_TOKEN": "envtok"}):
            url, tok = bc.contribution_cfg()
        self.assertEqual((url, tok), ("http://example.test", "envtok"))

    def test_cfg_local_file_override(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("VDL_BAIDU_CONTRIBUTION_URL", None)
            os.environ.pop("VDL_BAIDU_CONTRIBUTION_TOKEN", None)
            self.tmp.joinpath("cloud_sync.json").write_text(json.dumps(
                {"contribution_url": "http://local.test", "contribution_token": "loctok"}))
            url, tok = bc.contribution_cfg()
        self.assertEqual((url, tok), ("http://local.test", "loctok"))

    # -- 开关状态机 --------------------------------------------------------

    def test_default_on_and_revoke(self):
        self.assertTrue(bc.is_enabled(), "默认开启（2026-10-30 用户拍板）")
        bc.set_enabled(False)
        self.assertFalse(bc.is_enabled(), "显式取消勾选必须持久生效")
        bc.set_enabled(True)
        self.assertTrue(bc.is_enabled())
        st = bc.revoke()
        self.assertFalse(st["enabled"])
        self.assertFalse(bc.is_enabled())

    # -- 账号分组 ----------------------------------------------------------

    def _seed_pool(self):
        """两个账号 × 3 份快照（字段数 p,p+1,p）+ 1 条无 BDUSS 噪声。"""
        accs = [("A" * 32, 6, True), ("B" * 32, 10, False)]
        cookies = []
        for bd, parts, member in accs:
            for snap in range(3):
                cookies.append({"header": _hdr(bd, extra=parts - 2 + (snap % 2)),
                                "member": member, "ts": 1700000000 + snap})
        cookies.append({"header": "STOKEN=no_bduss", "ts": 1})
        self.pool.joinpath("baidu.com.json").write_text(json.dumps({"cookies": cookies}))
        return accs

    def test_group_accounts_dedup_and_pick_best(self):
        accs = self._seed_pool()
        g = bc.group_accounts()
        self.assertEqual(len(g), 2, "同 BDUSS 的多份快照必须合并为一个账号")
        by_key = {a["key"]: a for a in g}
        for bd, parts, member in accs:
            key = hashlib.sha256(bd.encode()).hexdigest()[:12]
            self.assertIn(key, by_key)
            self.assertEqual(by_key[key]["member"], member)
            # 3 份快照字段数是 p,p+1,p -> 最全一份 = parts+1
            self.assertEqual(by_key[key]["parts"], parts + 1)
        # 会员排前（仅推送顺序）
        self.assertTrue(g[0]["member"])

    # -- 推送编排 ----------------------------------------------------------

    def test_push_accounts_success_and_sig_skip(self):
        self._seed_pool()
        calls = []

        def fake_inject(url, token, header):
            calls.append(header)
            return {"ok": True, "added": True, "verified": True}

        with mock.patch.dict("os.environ", ENV_TOKEN), \
                mock.patch.object(bc, "_post_inject", side_effect=fake_inject):
            r1 = bc.push_accounts(force=True)
        self.assertEqual((r1["pushed"], r1["failed"], r1["skipped"]), (2, 0, False))
        self.assertEqual(len(calls), 2)
        # 集合未变 + 全成功 -> 自动推送必须跳过
        with mock.patch.dict("os.environ", ENV_TOKEN), \
                mock.patch.object(bc, "_post_inject", side_effect=fake_inject):
            r2 = bc.push_accounts(force=False)
        self.assertTrue(r2["skipped"], "账号集合未变化必须跳过")
        self.assertEqual(r2["pushed"], 0)
        # 开关关闭时 auto_push 不推（默认开，先显式关闭再验证旁路被闸）
        bc.set_enabled(False)
        with mock.patch.object(bc, "push_accounts", wraps=bc.push_accounts) as wp:
            bc.auto_push_if_enabled()
        wp.assert_not_called()

    def test_push_failure_keeps_sig_unset_for_retry(self):
        self._seed_pool()

        def failing(url, token, header):
            return {"ok": False, "error": "HTTP 503"}

        with mock.patch.dict("os.environ", ENV_TOKEN), \
                mock.patch.object(bc, "_post_inject", side_effect=failing):
            r = bc.push_accounts(force=True)
        self.assertEqual((r["pushed"], r["failed"]), (0, 2))
        st = bc._load_state()
        self.assertFalse(st.get("sig"), "有失败不得刷 sig（下一轮要重试）")
        # 恢复后重试成功
        with mock.patch.dict("os.environ", ENV_TOKEN), \
                mock.patch.object(bc, "_post_inject", side_effect=lambda *a: {"ok": True}):
            r2 = bc.push_accounts(force=False)
        self.assertEqual(r2["pushed"], 2)

    def test_push_no_credentials_skips(self):
        self._seed_pool()
        with mock.patch.object(bc, "contribution_cfg", return_value=("http://x.test", "")):
            r = bc.push_accounts(force=True)
        self.assertTrue(r["skipped"])
        self.assertEqual(r["reason"], "无凭据")

    # -- 状态出口绝无明文 --------------------------------------------------

    def test_status_has_no_plaintext(self):
        accs = self._seed_pool()
        bc.set_enabled(True)
        with mock.patch.dict("os.environ", ENV_TOKEN), \
                mock.patch.object(bc, "_post_inject", side_effect=lambda *a: {"ok": True}):
            bc.push_accounts(force=True)
        s = bc.status()
        blob = json.dumps(s, ensure_ascii=False)
        self.assertNotIn("BDUSS", blob)
        for bd, _, _ in accs:
            self.assertNotIn(bd[:8], blob, "状态出口不得出现 BDUSS 片段")
        self.assertEqual(len(s["accounts"]), 2)
        for a in s["accounts"]:
            self.assertEqual(len(a["key"]), 12)
            self.assertTrue(a["pushed_ok"], "推送成功后状态应回 pushed_ok=True")
        self.assertNotIn("testtok", blob, "令牌任何形式不得进状态出口")
        # 日志文件也不落明文
        log = self.tmp.joinpath("baidu_contribute.log").read_text()
        self.assertNotIn("BDUSS", log)
        for bd, _, _ in accs:
            self.assertNotIn(bd[:8], log)


if __name__ == "__main__":
    unittest.main()
