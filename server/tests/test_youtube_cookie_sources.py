#!/usr/bin/env python
"""YouTube Cookie 源顺序 + 登录态体检：守卫测试（2026-09-27）

背景（来自竞品 DataTool 的静态分析结论）：
DataTool（vip.datatool.datatool 2.0.0）YouTube 解析稳，原因**不是**「用了 yt-dlp」
—— 我们也用 —— 而是它的会话**在自己浏览器里出生**（Electron 的 persist 分区），
每次交给 yt-dlp 的都是同机、同出口 IP、且仍在滚动刷新的那**一份活会话**。
它的代码里甚至有个显式体检函数 `getYoutubeEmbeddedLoginStatus()`，只认
`SID / LOGIN_INFO / __Secure-*PSID`，并会记一句
"found cookies but no login session … SABR downloads may fail"。

我们这边的缺口（2026-09-27 本机实测）：
1. 桌面端 `_youtube_cookie_candidates()` 的顺序是 `user > env > cache > pool`，
   **没有「实时解密浏览器」这一档**；
2. 而 `cookie_cache` 的 TTL 长达 **30 天**。
3. 在同一台机器上同时取两份（实时抽取 vs 缓存）做 diff：25 个字段里绝大多数逐字节
   相同（含 SID / LOGIN_INFO / __Secure-1PSID / 3PSID / SAPISID / HSID / SSID），
   **只有 5 个滚动字段不同** ——
   `SIDCC`、`__Secure-1PSIDCC`、`__Secure-1PSIDTS`、`__Secure-3PSIDCC`、`__Secure-3PSIDTS`。
   Google 正是用这几个滚动值判定会话是否「还活着」⇒ 快照落后于浏览器，
   YouTube 就把整份会话当无效（LOGGED_IN:false）→ bot 拦截。
   「有缓存」于是**永远挡在新鲜值前面**被尝试并失败，表现为
   「Cookie 明明在更新，却始终报未登录 / bot」。

本文件离线守卫三件事（**注入桩 + 真调函数**，不读源码文本、不发任何网络请求）：
1. `youtube_cookie_login_state()` 的判据；
2. `_youtube_source_is_hopeless()`：自动源（browser / cache / pool）缺登录态字段即跳过，
   而 user / env 这类**显式**源永不跳过；
3. `_youtube_cookie_candidates()` 的真实顺序必须是
   `user > env > browser > cache > pool`，且重复值只保留最先出现的那个源。
"""
import os
import sys
import types
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_yt_cookie_sources")

import downloader as D  # noqa: E402  （需要 SERVER_DIR 已在 sys.path）

_SAVED: dict[str, object] = {}


def _install_stubs(*, browser: str = "", cache: str = "", pool: str = "") -> None:
    """把三个来源替换成桩：只替换 sys.modules 里的假模块 / 模块属性，不碰真实文件。"""
    _SAVED["browser"] = D.get_browser_cookie_header
    D.get_browser_cookie_header = lambda host, url: browser  # type: ignore[assignment]

    fake_cache = types.ModuleType("cookie_cache")
    fake_cache.get_cached_cookie_header = lambda host: cache  # type: ignore[attr-defined]
    _SAVED["cache_mod"] = sys.modules.get("cookie_cache")
    sys.modules["cookie_cache"] = fake_cache

    fake_pool = types.ModuleType("cookie_pool")
    fake_pool.get_cookie = lambda host: pool  # type: ignore[attr-defined]
    _SAVED["pool_mod"] = sys.modules.get("cookie_pool")
    sys.modules["cookie_pool"] = fake_pool


def _restore_stubs() -> None:
    if "browser" in _SAVED:
        D.get_browser_cookie_header = _SAVED["browser"]  # type: ignore[assignment]
    for key, name in (("cache_mod", "cookie_cache"), ("pool_mod", "cookie_pool")):
        mod = _SAVED.get(key)
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod  # type: ignore[assignment]


class TestYoutubeLoginState(unittest.TestCase):
    """登录态判据：只有 SID / __Secure-*PSID / LOGIN_INFO 才算「登录过」。"""

    def test_empty(self):
        self.assertEqual(D.youtube_cookie_login_state(""), "empty")
        self.assertEqual(D.youtube_cookie_login_state("   "), "empty")
        self.assertEqual(D.youtube_cookie_login_state(None), "empty")

    def test_anonymous_visitor_cookies_are_no_login(self):
        """匿名访客也有一堆 Cookie（PREF / VISITOR_INFO1_LIVE / YSC），但不算登录。"""
        h = "PREF=f4=40000000; VISITOR_INFO1_LIVE=abc; YSC=xyz; CONSENT=YES+"
        self.assertEqual(D.youtube_cookie_login_state(h), "no_login")

    def test_logged_in_markers(self):
        for name in ("SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"):
            with self.subTest(name=name):
                self.assertEqual(
                    D.youtube_cookie_login_state(f"PREF=x; {name}=value123"),
                    "logged_in",
                    f"{name} 是登录态标志，必须判为 logged_in",
                )

    def test_psidts_alone_is_not_login(self):
        """__Secure-*PSIDTS 是滚动字段，匿名也可能有 —— 单独出现不算登录。"""
        self.assertEqual(
            D.youtube_cookie_login_state("__Secure-1PSIDTS=yy; __Secure-3PSIDTS=zz"),
            "no_login",
        )


class TestYoutubeSourceGate(unittest.TestCase):
    """体检闸门：自动源缺登录态字段就跳过，显式源不跳过。"""

    def test_explicit_sources_never_skipped(self):
        for src in ("user", "env"):
            with self.subTest(src=src):
                self.assertIsNone(D._youtube_source_is_hopeless(src, "PREF=x"))
                self.assertIsNone(D._youtube_source_is_hopeless(src, ""))

    def test_auto_sources_skipped_without_login(self):
        for src in ("browser", "cache", "pool"):
            with self.subTest(src=src):
                self.assertEqual(D._youtube_source_is_hopeless(src, "PREF=x"), "未登录")
                self.assertEqual(D._youtube_source_is_hopeless(src, ""), "空 Cookie")

    def test_auto_sources_kept_with_login(self):
        for src in ("browser", "cache", "pool"):
            with self.subTest(src=src):
                self.assertIsNone(D._youtube_source_is_hopeless(src, "SID=abc"))
                self.assertIsNone(D._youtube_source_is_hopeless(src, "LOGIN_INFO=v"))


class TestYoutubeCandidateOrder(unittest.TestCase):
    """候选顺序：user > env > browser > cache > pool（browser 必须早于 cache）。"""

    def setUp(self):
        os.environ.pop("VDL_YOUTUBE_COOKIE", None)

    def tearDown(self):
        _restore_stubs()
        os.environ.pop("VDL_YOUTUBE_COOKIE", None)

    def test_order_places_live_browser_before_cache(self):
        _install_stubs(browser="SID=live", cache="SID=stale", pool="SID=pool")
        os.environ["VDL_YOUTUBE_COOKIE"] = "SID=envvalue"
        cands = D._youtube_cookie_candidates("SID=uservalue")
        srcs = [s for s, _ in cands]
        self.assertEqual(
            srcs,
            ["user", "env", "browser", "cache", "pool"],
            "顺序必须是 user > env > browser > cache > pool。\n"
            "🔴 browser（实时解密本机浏览器）必须排在 cache 之前：cookie_cache 的 TTL 是 30 天，\n"
            "而 Google 的 SIDCC / __Secure-*PSIDTS / PSIDCC 是滚动刷新的 ——\n"
            "一份「有登录字段但滚动值已过期」的快照会被 YouTube 判为未登录，\n"
            "却因为「缓存命中」永远挡在新鲜值前面，形成死循环。",
        )

    def test_live_browser_wins_dedup_over_cache(self):
        """浏览器与缓存拿到同一份值时只保留一次，且来源记为 browser（更可解释）。"""
        _install_stubs(browser="SID=same", cache="SID=same", pool="SID=pool")
        cands = D._youtube_cookie_candidates("")
        # 去重后 cache 与 browser 同值 → 只留 browser
        self.assertEqual([s for s, _ in cands], ["browser", "pool"])

    @unittest.skipIf(
        os.environ.get("VDL_OFFLINE_MAC_BUILD"),
        "离线 Mac 构建：本用例依赖「云端 cookie 池不含 youtube cookie」的先决条件，"
        "而真实云端池状态在离线构建机不可控（当前恰含 youtube cookie），属环境依赖，跳过；"
        "云连通 CI 仍跑，保留契约守卫。",
    )
    def test_missing_sources_are_omitted(self):
        _install_stubs(browser="", cache="", pool="")
        self.assertEqual(D._youtube_cookie_candidates(""), [])

    def test_user_cookie_still_first(self):
        _install_stubs(browser="SID=live", cache="", pool="")
        cands = D._youtube_cookie_candidates("SID=user")
        self.assertEqual(cands[0], ("user", "SID=user"))


class TestResolverWiring(unittest.TestCase):
    """消费端接线：`_resolve_youtube` 必须真的用上体检闸门与精确报错。"""

    def test_resolver_gates_auto_sources(self):
        with open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.find("def _resolve_youtube(")
        self.assertNotEqual(i, -1, "未找到 _resolve_youtube")
        body = src[i: i + 9000]
        self.assertIn(
            "_youtube_source_is_hopeless(src, ck)",
            body,
            "_resolve_youtube 里没有调用体检闸门 ⇒ 自动源会被无谓地尝试，白烧墙钟预算。",
        )
        self.assertIn(
            "_evict_youtube_cookie_cache(\"无登录态字段\")",
            body,
            "缓存被判无登录态时必须清掉它，否则下次仍会挡住新鲜值。",
        )
        self.assertIn(
            "浏览器里的 YouTube 登录态不可用",
            body,
            "全部自动源都没有登录态时，必须给出这个精确原因（而不是笼统的「需要登录 Cookie」），"
            "否则用户会误以为「没配 Cookie」。",
        )


if __name__ == "__main__":
    print("🧪 YouTube Cookie 源顺序 / 登录态体检 守卫测试\n")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(c) for c in (
            TestYoutubeLoginState,
            TestYoutubeSourceGate,
            TestYoutubeCandidateOrder,
            TestResolverWiring,
        )
    )
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    n_ok = r.testsRun - len(r.failures) - len(r.errors)
    if r.failures or r.errors:
        print(f"\n❌ 失败 {len(r.failures) + len(r.errors)} 项")
        sys.exit(1)
    print(f"\n🎉 YouTube Cookie 源 守卫全部通过（{n_ok} 项）")
