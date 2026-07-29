# -*- coding: utf-8 -*-
"""M5：QMT session 共享队列/锁残留清理（交互式，防误删活跃队列）。

物理背景（[[qmt-connect-1-rootcause]]）：
    xtquant 用共享内存文件 down_queue_win_{sid}/up_queue_win_*/lock_* 与客户端通信，
    同一 sid 同一时刻只能被一个进程独占。老进程崩溃/断线后残留的锁文件会让新进程
    connect 返回 -1（疑似被占用）。本脚本列出残留并交互式清理【非当前 sid 且 >1h 未动】
    的文件——当前 sid / 近期活跃的一律拒绝删除（红线：删活跃队列=弄坏在跑的 engine）。

用法：.venv310/Scripts/python.exe scripts/qmt_clear_session_lock.py
"""
import glob, os, re, sys, time

def _env_userdata():
    """从 .env 读 QMT_USERDATA_PATH（脚本可能被独立调用，不依赖 config 包）。"""
    from dotenv import load_dotenv
    load_dotenv()
    return os.environ.get("QMT_USERDATA_PATH", "")

def _env_sid():
    """从 .env 读 QMT_SESSION_ID（默认 123456），与 engine 当前 sid 对齐。"""
    from dotenv import load_dotenv
    load_dotenv()
    return int(os.environ.get("QMT_SESSION_ID", "123456"))

# 扫描的锁文件 glob 模式：
# - down_queue_win_*         主下行队列（按 sid 分桶）
# - lock_*queue_win_*        各队列的 lock 文件
# - *_queue_win_*__mutex     跨进程互斥量（双下划线后缀）
_LOCK_PATTERNS = ["down_queue_win_*", "lock_*queue_win_*", "*_queue_win_*__mutex"]

def list_session_locks(userdata_path):
    """扫 userdata 下所有 session 相关文件，归 sid + mtime。返回 [{sid,path,mtime,name}]。

    sid 提取：从文件名 down_queue_win_{sid} / lock_down_queue_win_{sid} 正则取末尾数字。
    非 sid 文件（如 up_queue_win_xtquant 不带 sid）sid=None，单独归「共享」不自动清。
    """
    if not userdata_path or not os.path.isdir(userdata_path):
        return []
    out = []
    for pat in _LOCK_PATTERNS:
        for f in glob.glob(os.path.join(userdata_path, pat)):
            name = os.path.basename(f)
            # 取末尾数字（去 mutex 后缀）：down_queue_win_123456__mutex → 取 down_queue_win_123456 → 123456
            m = re.search(r"(\d+)\s*$", name.split("__")[0])
            sid = int(m.group(1)) if m else None
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue  # 文件被并发删除等异常，跳过不中断扫描
            out.append({"sid": sid, "path": f, "mtime": mtime, "name": name})
    return out

def is_clearable(lock, current_sid, now, max_age_sec=3600):
    """可清判定：非当前 sid 且 mtime 超过 max_age_sec（默认 1h）。

    红线：当前 sid 的文件绝不清（可能正被 engine 使用）；近期活跃的不清（可能刚用）。
    sid=None（共享文件，未归属某 sid）也保守不清——需人工介入判断。
    """
    sid = lock.get("sid")
    if sid is None or sid == current_sid:
        return False
    return (now - lock.get("mtime", 0)) > max_age_sec

def main():
    """列锁 + 交互式清理（逐文件确认，默认不删）。"""
    userdata = _env_userdata()
    current_sid = _env_sid()
    print(f"=== QMT session 锁清理（当前 .env sid={current_sid}）===")
    print(f"userdata: {userdata}")
    locks = list_session_locks(userdata)
    now = time.time()
    clearable = [l for l in locks if is_clearable(l, current_sid, now)]
    protected = [l for l in locks if not is_clearable(l, current_sid, now)]
    print(f"\n[保护·不动] {len(protected)} 个（当前 sid 或近1h活跃）：")
    for l in protected[:10]:
        print(f"  sid={l['sid']} {l['name']} mtime={time.ctime(l['mtime'])}")
    print(f"\n[可清·残留] {len(clearable)} 个（非当前 sid 且 >1h 未动）：")
    for l in clearable:
        print(f"  sid={l['sid']} {l['name']} mtime={time.ctime(l['mtime'])}")
    if not clearable:
        print("无可清残留，退出。"); return
    ans = input("\n逐文件确认删除？输入 'yes' 删除全部可清 / 单独 sid 数字 / 回车取消：").strip()
    if ans == "yes":
        for l in clearable:
            try: os.remove(l["path"]); print(f"  已删 {l['name']}")
            except OSError as e: print(f"  删除失败 {l['name']}: {e}")
    elif ans.isdigit():
        target = int(ans)
        for l in clearable:
            if l["sid"] == target:
                try: os.remove(l["path"]); print(f"  已删 {l['name']}")
                except OSError as e: print(f"  删除失败: {e}")
    else:
        print("取消，未删除任何文件。")

if __name__ == "__main__":
    main()
