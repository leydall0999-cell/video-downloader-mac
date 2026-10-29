"""harvest_baidu_browser_cookies 多 profile 抓取单元测试（离线，全程 mock）。

钉死 2026-10-29 用户根因修复的契约：
- Chrome 多个 profile（Default / Profile 1 / Profile 2）里**每个**含 baidu.com 登录态的
  都要被抓到并 add_cookie 入池（原先只抓「单个最优 profile」，3 个账号只剩 1 个）；
- 没有 baidu.com 登录态的 profile 必须跳过，不污染候选；
- Safari 单独走一条路径也要抓到（Safari 不在 browser_cookies 来源表里）；
- 返回的 header 列表含每个含 BDUSS 的 profile 的 header，且 source 标记为 browser。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


class _FakeCookie:
    __slots__ = ("domain", "name", "value")

    def __init__(self, domain, name, value):
        self.domain = domain
        self.name = name
        self.value = value


class _FakeJar:
    def __init__(self, cookies):
        self._c = list(cookies)

    def __iter__(self):
        return iter(self._c)


def _bc_fake():
    """只含 chrome 来源的假 browser_cookies：3 个 profile。"""
    profiles = [
        ("Default", "/x/Default/Network/Cookies"),
        ("Profile 1", "/x/Profile 1/Network/Cookies"),
        ("Profile 2", "/x/Profile 2/Network/Cookies"),
    ]
    return mock.MagicMock(
        _BROWSER_SOURCES=({"key": "chrome", "kind": "chromium", "roots": ("/x",)},),
        iter_profiles=lambda src: list(profiles),
    )


def _extract_fake(browser, profile=None, **kw):
    if browser == "safari":
        return _FakeJar([_FakeCookie(".baidu.com", "BDUSS", "safari_bduss")])
    if browser == "chrome":
        if profile == "Default":
            return _FakeJar([_FakeCookie(".baidu.com", "BDUSS", "chrome_default_bduss")])
        if profile == "Profile 1":
            return _FakeJar([
                _FakeCookie(".baidu.com", "BDUSS", "chrome_p1_bduss"),
                _FakeCookie(".baidu.com", "STOKEN", "s1"),
            ])
        if profile == "Profile 2":
            # 这个 profile 只登录了 example.com，没有百度登录态 —— 必须跳过
            return _FakeJar([_FakeCookie(".example.com", "foo", "bar")])
    return _FakeJar([])


def test_harvest_enumerates_all_chrome_profiles_with_baidu():
    import downloader

    added = []

    def fake_add(domain, header, source="browser"):
        added.append((domain, header, source))
        return True

    with mock.patch.object(downloader, "_browser_cookies", _bc_fake()), \
         mock.patch("yt_dlp.cookies.extract_cookies_from_browser", side_effect=_extract_fake), \
         mock.patch("cookie_pool.add_cookie", side_effect=fake_add):
        headers = downloader.harvest_baidu_browser_cookies()

    bodies = [h for _, h, _ in added]
    assert any("chrome_default_bduss" in h for h in bodies), "Default profile 没被抓"
    assert any("chrome_p1_bduss" in h for h in bodies), "Profile 1 没被抓"
    assert any("safari_bduss" in h for h in bodies), "Safari 没被抓"
    assert not any("example.com" in h for h in bodies), "无 baidu 的 profile 不应入池"

    # 返回列表 = 3 份（Default / Profile1 / Safari），且每条都带 BDUSS
    assert len(headers) == 3, f"期望 3 份 header，实际 {len(headers)}"
    assert all("bduss=" in h.lower() for h in headers)
    # 入池 source 必须是 browser
    assert all(src == "browser" for _, _, src in added)


def test_harvest_skips_when_browser_cookies_unavailable():
    import downloader

    with mock.patch.object(downloader, "_browser_cookies", None):
        assert downloader.harvest_baidu_browser_cookies() == []


def test_harvest_skips_when_yt_dlp_missing():
    import downloader

    with mock.patch.object(downloader, "_browser_cookies", _bc_fake()), \
         mock.patch("yt_dlp.cookies.extract_cookies_from_browser",
                    side_effect=ImportError("no yt_dlp")):
        # 单 profile 抛 ImportError 被逐个吞掉，整体返回空而不是崩
        assert downloader.harvest_baidu_browser_cookies() == []
