# -*- coding: utf-8 -*-
"""
启动兼容性回归测试
==================

守两件事（都是真实踩过的坑）：

1. **GBK 控制台下的 emoji 崩溃**
   程序日志里到处是 emoji（✅ ⚠ 🔎…）。如果进程带着 GBK 编码的 stdout
   （从 .bat / cmd 启动，或输出被重定向就会这样），`print("✅")` 会抛
   UnicodeEncodeError；异常处理里再 print 一个 emoji 又抛一次，
   直接把启动流程打断，弹「程序启动时发生错误」。

2. **中文路径 / 非 ASCII 输出的编码一致性**
   修复方案是把 stdout/stderr 切到 UTF-8 + errors="replace"。

用法
----
    python tests/test_startup_encoding.py
"""

import io
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EMOJI_SAMPLES = [
    "✅ 已把本进程降为低优先级",
    "⚠ 降优先级步骤跳过",
    "🔎 正在自检官方海克斯库…",
    "⏹ 已停止",
    "■ ▶ ⏳ ⭐ 混合符号",
]


def _with_stream(enc, errors):
    """把 stdout 换成指定编码的真实文件流，返回 (stream, path)。"""
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    raw = open(path, "wb")
    return io.TextIOWrapper(raw, encoding=enc, errors=errors, line_buffering=True), path


def main() -> int:
    import gui_launcher as g

    failures = []

    # ---- 1) 复现：GBK 严格模式 + 直接 print emoji 必须失败 ----
    stream, path = _with_stream("gbk", "strict")
    saved = sys.stdout
    sys.stdout = stream
    try:
        print(EMOJI_SAMPLES[0])
        failures.append("预期 GBK 严格模式会抛 UnicodeEncodeError，但没有抛")
    except UnicodeEncodeError:
        pass  # 符合预期
    finally:
        try:
            stream.flush()
        except Exception:
            pass
        sys.stdout = saved
    print("[1/3] GBK 严格模式下 print(emoji) 确实会抛异常 —— 复现 OK")

    # ---- 2) 修复：调用 make_std_streams_safe 后必须全部可打印 ----
    stream, path2 = _with_stream("gbk", "strict")
    saved = sys.stdout
    sys.stdout = stream
    ok = True
    try:
        g.make_std_streams_safe()
        for s in EMOJI_SAMPLES:
            print(s)
    except Exception as e:
        ok = False
        failures.append(f"make_std_streams_safe 之后仍然失败: {e!r}")
    finally:
        try:
            stream.flush()
        except Exception:
            pass
        sys.stdout = saved
    if ok:
        print("[2/3] make_std_streams_safe 之后 emoji 全部可打印 —— 修复 OK")

    # 校验写出的内容确实是 UTF-8（被改写了编码）
    try:
        with open(path2, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
        assert "✅" in text, "写出的内容里找不到 ✅"
        print("[3/3] 输出已按 UTF-8 落盘，内容完整 —— 校验 OK")
    except Exception as e:
        failures.append(f"输出编码校验失败: {e!r}")

    for p in (path, path2):
        try:
            os.remove(p)
        except OSError:
            pass

    # ---- 4) safe_print 在流不可写时必须吞掉异常 ----
    class Broken:
        def write(self, *_a):
            raise OSError("控制台已关闭")

        def flush(self):
            raise OSError("控制台已关闭")

    saved = sys.stdout
    sys.stdout = Broken()
    raised = None
    try:
        g.safe_print("这行不应该让程序崩")
    except Exception as e:          # 只有 safe_print 自己抛才算失败
        raised = e
    sys.stdout = saved
    if raised is None:
        print("[附加] safe_print 在控制台失效时未抛异常 —— OK")
    else:
        failures.append(f"safe_print 未吞掉异常: {raised!r}")

    if failures:
        print("\n❌ 失败项:")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ 启动兼容性测试全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
