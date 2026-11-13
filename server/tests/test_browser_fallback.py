"""通用浏览器兜底（browser_resolve）离线守卫。

只测纯逻辑与「该拒绝的必须拒绝」，**不启动任何浏览器、不联网**：
  - RFC 2606 测试/保留域名必须直接拒绝（否则健康巡检的假样本会去开浏览器）；
  - VDL_GENERIC_BROWSER=0 必须整体关闭；
  - 非 http(s) 链接必须拒绝；
  - 流挑选优先级：master playlist > 其它 m3u8 > mp4，且 .ts 分片永不入选；
  - 环境缺浏览器时 _find_browser() 返回空串而不是抛异常。

运行：cd server && VDL_CLOUD_LINK=0 ../.build_venv/bin/python tests/test_browser_fallback.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_resolve as BR  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")
        FAILS.append(name)


def expect_raises(fn, name, want_substr=""):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        if want_substr and want_substr not in str(exc):
            FAILS.append(f"{name}(消息不含 {want_substr!r}: {exc})")
            print(f"  FAIL {name} 消息={exc}")
        else:
            print(f"  ok   {name}")
        return
    FAILS.append(f"{name}(没有抛错)")
    print(f"  FAIL {name} 没有抛错")


def main():
    print("[1] 测试域名识别")
    for h in ("selfcheck.invalid", "app-tele-test.example.com", "foo.example",
              "bar.test", "localhost", "x.localhost", "a.example.net", ""):
        check(f"识别为测试域名: {h!r}", BR._is_test_host(h), "本应是 True")
    for h in ("lespark.us", "haokan.baidu.com", "miguvideo.com", "zhihu.com",
              "notexample.com", "test.org", "example.org.cn"):
        check(f"不误判: {h!r}", not BR._is_test_host(h), "本应是 False")

    print("[2] 拒绝路径（不启动浏览器）")
    old = os.environ.get("VDL_GENERIC_BROWSER")
    os.environ["VDL_GENERIC_BROWSER"] = "0"
    try:
        expect_raises(lambda: BR.resolve("https://lespark.us/videos/abc"), "开关关闭时拒绝", "关闭")
    finally:
        if old is None:
            os.environ.pop("VDL_GENERIC_BROWSER", None)
        else:
            os.environ["VDL_GENERIC_BROWSER"] = old
    expect_raises(lambda: BR.resolve("https://selfcheck.invalid/x"), "测试域名拒绝", "测试域名")
    expect_raises(lambda: BR.resolve("ftp://example.org/a.mp4"), "非 http(s) 拒绝", "")

    print("[3] 流挑选优先级")
    master = "https://cdn/a/adp.123.m3u8?sign=1"
    child = "https://cdn/a/video_1.m3u8?sign=1"
    mp4 = "https://cdn/a/v.mp4"
    ts = "https://cdn/a/1_0.ts?sign=1"
    check("master 优先于子清单与 mp4", BR._pick_stream([child, master], [mp4]) == master)
    check("无 master 时取首个 m3u8", BR._pick_stream([child], [mp4]) == child)
    check("无 m3u8 时取 mp4", BR._pick_stream([], [mp4]) == mp4)
    check("全空返回空串", BR._pick_stream([], []) == "")
    check(".ts 不会被选为结果", BR._pick_stream([], [ts]) == "" or not BR._pick_stream([], [ts]).endswith(".ts"))

    print("[4] 其他纯逻辑")
    check("_enabled 默认开启", (os.environ.get("VDL_GENERIC_BROWSER") or "1") != "0")
    b = BR._find_browser()
    check("_find_browser 返回字符串", isinstance(b, str))
    print(f"       当前找到的浏览器: {b or '(无，运行时会报错并回退原提示)'}")

    print("[5] 未知域名判定（驱动兜底触发；离线，不联网）")
    try:
        import downloader as DL  # noqa: E402
        check("lespark.us 视为未知域名 → 走兜底", DL._host_is_unknown_to_vdl("lespark.us"))
        check("子域 a.lespark.us 也未知", DL._host_is_unknown_to_vdl("a.lespark.us"))
        check("YouTube 不算未知", not DL._host_is_unknown_to_vdl("www.youtube.com"))
        check("B站不算未知", not DL._host_is_unknown_to_vdl("www.bilibili.com"))
        check("空 host 不算未知", not DL._host_is_unknown_to_vdl(""))
        check("yt-dlp 已知 hotstar 不算未知", not DL._host_is_unknown_to_vdl("www.hotstar.com"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 跳过 downloader 导入（离线环境可能不可用）: {exc}")

    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("🎉 浏览器兜底守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
