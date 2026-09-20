# -*- coding: utf-8 -*-
"""
超星图书馆座位预约 — main.py (GitHub Actions 备用通道版)
=============================================================
本文件是「GitHub Actions 备用抢座通道」。

设计定位
--------
服务器(阿里云)是**主力通道**; 本 GitHub 版本作为**第二路备份**,
用在服务器挂掉 / 被限流 / 单 IP 暴露时的兜底。

⚠️⚠️ 使用前必读: 本通道抢到座位后【不会自动签到】⚠️⚠️
  _sign.py / _schedule_sign.py 不在本仓库, 且签到调度机制硬绑定本机
  (复制脚本到本地 _scheduled/ 目录 + 注册本机 at job)。
  GitHub runner 用完即焚, 11 小时后的 at job 随 VM 一起消失。

  ⇒ **本通道抢到座位后, 你必须自己去签到。**
     签到窗口 = 到馆时段开始后 30 分钟内(preSignDuration=0 / signDuration=30)。
     超时 = 记 1 次违约。违约累计 3 次触发限制(禁 7 天),
     且该校为管理员人工拉黑(App 显示"非法预约")。

     建议: 仅在你确定当天能按时签到的情况下启用本通道;
           启用后请把对应时段的闹钟一起定上。

与服务器版的差异(逐项)
----------------------
1. RESERVE_NEXT_DAY = False
   GitHub runner 是 UTC, 必须用 `--action`(+8h 位移); 服务器本地即北京时间,
   用 无`--action`。两边日期算法各自配套, 最终都抢"明天"。
   · GitHub:  action=True  + NEXT_DAY=False → day = today(UTC)+1 = 明天 ✅
   · 服务器:  action=False + NEXT_DAY=True  → day = today+1        = 明天 ✅
2. LOGIN_LEAD 缩短 + 迟到自愈
   GitHub Actions 的 cron 触发有数分钟~十几分钟排队延迟, 原 180s 预热会
   直接踩空。本版改为 90s, 且起跑已过 20:00 时自动进入宽限抢座窗口。
3. 移除签到调度
   本文件**不含任何签到代码**(不 import _schedule_sign, 无 _try_schedule_sign)。
   仓库里也没有 _sign.py / _schedule_sign.py, 签到机制硬绑定运行机器,
   GitHub runner 无法承载。抢到后仅打印提醒, 由你自行到馆签到。
4. 新增 --offset / --profile 参数
   首击时刻可命令行覆盖, 默认 8.8s(与服务器版一致)。

拟人化(与服务器版同步)
----------------------
· 首击时刻 8.8s + 双向抖动 ±0.35s
· 多账号错峰 0.15~0.45s 随机(非等距)
· 心跳间隔 60±25s 双向抖动
· HUMAN_PROFILE 档位一键切换(human / fast)
"""

import json
import time
import argparse
import os
import logging
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


from utils import reserve, get_user_credentials
from utils.reserve import SeatTakenError

# ============================================================
#  拟人档位
# ============================================================
USE_PROFILE = True
HUMAN_PROFILE = "human"     # "human" = 更拟人(8.8s) | "fast" = 更激进(4.0s)

# (首击秒, 首击抖动, 错峰下限, 错峰上限, 心跳基准, 心跳抖动)
_PROFILES = {
    "human": (8.8, 0.35, 0.15, 0.45, 60.0, 25.0),
    "fast":  (4.0, 0.20, 0.15, 0.30, 60.0, 15.0),
}

if USE_PROFILE:
    _pf = _PROFILES.get(HUMAN_PROFILE, _PROFILES["human"])
    PACED_FIRE_OFFSET, FIRE_JITTER, STAGGER_LO, STAGGER_HI, PING_BASE, PING_JIT = _pf
else:
    PACED_FIRE_OFFSET = 8.8    # ★ 首击时刻(秒), 开抢后多久发出第一枪
    FIRE_JITTER = 0.35
    STAGGER_LO, STAGGER_HI = 0.15, 0.45
    PING_BASE, PING_JIT = 60.0, 25.0

PACE_ANCHOR_MODE = "staggered"   # "absolute" 全部锚 8.8s | "staggered" 逐个递增

# ★ 双层抖动补偿
# utils/reserve.py 的 _pace() 会在 pace_target_ts 上再叠 uniform(0, FIRE_JITTER_SEC=0.2),
# 若不补偿, 请求 8.8s 实际落在 8.8~9.0s(右偏 0.1)。这里预减内层中位数使分布以 8.8 为中心。
# 若把 reserve.py 的 FIRE_JITTER_SEC 改成双向, 请把此值设为 0.0。
INNER_JITTER_MEDIAN = 0.1

FAST_MODE = True
CONCURRENT_SEATS = False
LOGIN_LEAD = 90              # GitHub cron 有排队延迟, 缩短预热窗口
OPEN_HOUR, OPEN_MIN, OPEN_SEC = 20, 0, 0
ENDTIME = "20:05:00"
GRACE_SECONDS = 300          # 迟到宽限延长(应对 runner 排队)

ENABLE_SLIDER = True
CAPTCHA_TYPE = "auto"
FIRE_NO_CAPTCHA = False
MAX_ATTEMPT = 3
RESERVE_NEXT_DAY = False     # ★ GitHub/UTC 模式必须 False (配合 --action)
SLEEPTIME = 0.0

PLACEHOLDER = "*****"

HUMAN_RANDOM_STAGGER = True

# ============================================================
#  关于签到 (本通道【不做】签到调度)
# ============================================================
# 服务器的签到链路是 _schedule_sign.py → 冻结脚本 → 本机 at job,
# **硬绑定运行机器**(把脚本复制到本机 _scheduled/ 并注册本机 at)。
# GitHub runner 用完即焚, 几小时后的 at job 会随 VM 消失 ⇒ 本通道无法签到。
#
# 因此本文件**不包含任何签到调度代码**:
#   · 不 import _schedule_sign (仓库里也没有该文件)
#   · 没有 _try_schedule_sign 之类的入口
#   · 抢到座位后只打印提醒, 由你自己到馆签到
# 这样代码与真实行为一致, 不会让人误以为"这里有签到"。
#
# ⚠️ 签到窗口 = 到馆时段开始后 30 分钟内, 超时记 1 次违约。
#    违约累计 3 次触发限制(禁 7 天), 且该校为管理员人工审核。
# ============================================================


def _tz_offset(action):
    return 8 if action else 0


def bj_now_ts(action):
    return time.time() + _tz_offset(action) * 3600


def bj_str(action):
    return time.strftime("%H:%M:%S", time.localtime(bj_now_ts(action)))


def bj_dayofweek(action):
    return time.strftime("%A", time.localtime(bj_now_ts(action)))


def target_epoch(h, m, s, action):
    st = time.localtime(bj_now_ts(action))
    cand = time.struct_time(
        (st.tm_year, st.tm_mon, st.tm_mday, h, m, s, 0, 0, -1)
    )
    return time.mktime(cand) - _tz_offset(action) * 3600


def _get_creds(user, index, action, usernames, passwords):
    if action:
        return usernames.split(",")[index], passwords.split(",")[index]
    return user.get("username"), user.get("password")


def _make_reserve():
    return reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
        captcha_type=CAPTCHA_TYPE,
        fast_mode=FAST_MODE,
        fire_no_captcha=FIRE_NO_CAPTCHA,
    )


def _warmup_login(user, index, action, usernames, passwords):
    username, password = _get_creds(user, index, action, usernames, passwords)
    if not username or username == PLACEHOLDER:
        logging.warning("跳过未填充账号 (占位符 %s)" % PLACEHOLDER)
        return None
    s = _make_reserve()
    try:
        s._init_device_identity(username)
    except Exception:
        pass
    s.get_login_status()
    ok, msg = s.login(username, password)
    if not ok:
        logging.error("预热登录失败 %s: %s" % (username, msg))
        return None
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    return s


def _notify_sign_reminder(username):
    """抢到座位后的**提醒** (不是调度)。

    本通道不做签到调度 (原因见文件头「关于签到」注释)。
    这里只提醒你自行到馆签到, 避免抢到了却因忘签到而记违约。
    """
    logging.warning(
        "⚠️ 抢座成功: %s —— 本通道无签到能力, 请【自行到馆签到】! "
        "窗口 = 时段开始后 30 分钟内, 超时记 1 次违约 (累计 3 次限 7 天)。"
        % username
    )


def main(users, action=False):
    logging.info(
        "[GitHub备用通道] profile=%s FAST_MODE=%s OPEN=%02d:%02d:%02d "
        "首击=%.1fs±%.2fs 错峰=%.2f~%.2fs 锚点=%s ENDTIME=%s"
        % (HUMAN_PROFILE if USE_PROFILE else "manual", FAST_MODE,
           OPEN_HOUR, OPEN_MIN, OPEN_SEC,
           PACED_FIRE_OFFSET, FIRE_JITTER, STAGGER_LO, STAGGER_HI,
           PACE_ANCHOR_MODE, ENDTIME)
    )
    usernames, passwords = (get_user_credentials(action) if action else (None, None))
    current_dow = bj_dayofweek(action)

    def _has_seats(u):
        sid = u.get("seatid")
        if isinstance(sid, str):
            sid = [sid]
        return bool(sid)

    today = [(i, u) for i, u in enumerate(users)
             if current_dow in u.get("daysofweek", [])
             and u.get("username") and u["username"] != PLACEHOLDER
             and _has_seats(u)]
    if not today:
        logging.info("今天没有已填充的有效账号需要抢座")
        return

    today_num = len(today)
    success_list = [False] * len(users)
    seat_taken = set()
    attempt_times = 0
    logging.info("今日有效账号 %d 个: %s" % (today_num, [u["username"] for _, u in today]))

    open_e = target_epoch(OPEN_HOUR, OPEN_MIN, OPEN_SEC, action)
    warmup_at = open_e - LOGIN_LEAD
    pre = time.time()
    if pre < warmup_at:
        wait_pre = warmup_at - pre
        logging.info("距放票尚早, 先休眠 %.0fs 后再登录预热..." % wait_pre)
        time.sleep(wait_pre)
    elif pre > open_e:
        # ★ GitHub 特有: runner 排队可能让我们 20:0X 才起床
        _late = pre - open_e
        logging.warning(
            "启动已过放票时刻 %.1fs (runner 排队延迟), 立即进入宽限抢座 (%.0fs)"
            % (_late, GRACE_SECONDS)
        )

    logging.info("开始登录预热, 共 %d 个账号..." % today_num)
    warmed = {}
    for i, user in today:
        s = _warmup_login(user, i, action, usernames, passwords)
        if s is not None:
            warmed[i] = s
            logging.info("  预热成功: %s" % user.get("username", "?"))
        else:
            logging.warning("  预热失败: %s" % user.get("username", "?"))

    # 选座链预热
    for i, s in warmed.items():
        try:
            u = users[i]
            rid = u.get("roomid", "13485")
            sid = u.get("seatid")
            sid = sid[0] if isinstance(sid, list) else sid
            s.prewarm_fire(rid, sid)
        except Exception:
            pass

    logging.info("等待到 %02d:%02d:%02d (北京) 再开抢..." % (OPEN_HOUR, OPEN_MIN, OPEN_SEC))
    _last_ping = time.time()
    _ping_interval = PING_BASE * 0.5
    while True:
        remaining = open_e - time.time()
        if remaining <= 0:
            break
        if remaining > 2.0 and time.time() - _last_ping >= _ping_interval:
            with ThreadPoolExecutor(max_workers=max(1, len(warmed))) as _ex:
                list(_ex.map(lambda p: p[1].keepalive_ping(), warmed.items()))
            _last_ping = time.time()
            _ping_interval = PING_BASE + random.uniform(-PING_JIT, PING_JIT)
        if remaining > 0.1:
            time.sleep(0.03)
    logging.info("时间到! 当前 %s 开始抢座!" % bj_str(action))

    for i, user in today:
        if i not in warmed:
            s = _warmup_login(user, i, action, usernames, passwords)
            if s is not None:
                warmed[i] = s

    # ---- 首击时刻分配 ----
    offsets = []
    _acc = 0.0
    for _k, (i, _user) in enumerate(today):
        s = warmed.get(i)
        _fo = _user.get("fire_offset")
        _base = PACED_FIRE_OFFSET if _fo is None else float(_fo)
        if _k == 0:
            actual = _base
        else:
            if HUMAN_RANDOM_STAGGER:
                _acc += random.uniform(STAGGER_LO, STAGGER_HI)
            else:
                _acc += (STAGGER_LO + STAGGER_HI) / 2.0
            actual = _base + _acc if PACE_ANCHOR_MODE == "staggered" else _base
        offsets.append(actual)
        if s is not None:
            s.pace_target_ts = open_e + actual - INNER_JITTER_MEDIAN
            s._pace_used = False
            s._token_delay = max(0.0, actual - PACED_FIRE_OFFSET) * 0.5
    logging.info("[节奏] 锚点=%s 各账号首击偏移 %s"
                 % (PACE_ANCHOR_MODE, [round(o, 2) for o in offsets]))

    end_e = target_epoch(*map(int, ENDTIME.split(":")), action)
    if time.time() >= end_e:
        logging.warning("启动已过 ENDTIME(%s), 改为宽限 %ds" % (ENDTIME, GRACE_SECONDS))
        end_e = time.time() + GRACE_SECONDS

    while time.time() < end_e:
        attempt_times += 1
        logging.info("=== 第 %d 轮 (%s) ===" % (attempt_times, bj_str(action)))
        pending = [(i, u) for i, u in today
                   if not success_list[i] and i not in seat_taken]
        if not pending:
            break

        with ThreadPoolExecutor(max_workers=len(pending)) as ex:
            futs = {}
            for i, user in pending:
                s = warmed.get(i)
                if s is None:
                    s = _warmup_login(user, i, action, usernames, passwords)
                    if s is None:
                        continue
                    s.pace_target_ts = None
                    s._pace_used = False
                    warmed[i] = s
                username, _ = _get_creds(user, i, action, usernames, passwords)
                times = user["time"]
                roomid = user["roomid"]
                seatid = user["seatid"]
                if isinstance(seatid, str):
                    seatid = [seatid]
                futs[ex.submit(_grab_account, s, username, times, roomid, seatid, action)] = i

            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    success_list[i] = fut.result()
                except SeatTakenError:
                    success_list[i] = False
                    seat_taken.add(i)
                    logging.warning("账号 %s 座位已被他人预约, 永久跳过"
                                    % users[i].get("username"))
                except Exception as e:
                    success_list[i] = False
                    _m = str(e)
                    if "限制使用" in _m or "blackReason" in _m:
                        seat_taken.add(i)
                        logging.warning("账号 %s 已被限制使用, 永久跳过"
                                        % users[i].get("username"))
                    elif "违约" in _m:
                        seat_taken.add(i)
                        logging.warning("账号 %s 违约上限, 永久跳过"
                                        % users[i].get("username"))
                    else:
                        logging.error("账号 %s 抢座异常: %s"
                                      % (users[i].get("username"), e))
                else:
                    if success_list[i]:
                        logging.info("账号 %s 抢座成功" % users[i].get("username"))
                        # 只提醒, 不调度 (本通道无签到能力, 详见文件头注释)
                        _notify_sign_reminder(users[i].get("username"))

        if sum(1 for i, _ in today if success_list[i]) == today_num:
            logging.info("全部预约成功!")
            return

    logging.warning("到达截止时间仍未全部成功: %s" % success_list)
    logging.warning("⚠️ 若部分成功, 请立即到馆签到(窗口=时段开始后30分钟内), 否则记违约!")


def _grab_account(s, username, times, roomid, seatid, action):
    td = getattr(s, "_token_delay", 0)
    if td > 0:
        time.sleep(td)
        s._token_delay = 0
    _t0 = time.perf_counter()
    if CONCURRENT_SEATS and len(seatid) > 1:
        suc = _grab_concurrent(s, seatid, times, roomid, action)
    else:
        suc = s.submit(times, roomid, seatid, action)
    _ms = (time.perf_counter() - _t0) * 1000
    logging.info("[计时] 账号 %s 开抢总耗时 %.1f ms" % (username, _ms))
    return suc


def _grab_concurrent(template, seatid, times, roomid, action):
    logging.info("并发提交 %d 个座位..." % len(seatid))
    instances = []
    for seat in seatid:
        s = _make_reserve()
        s.get_login_status()
        ok, msg = s.login(template._username, template._password)
        if not ok:
            logging.warning("座位 %s 实例登录失败: %s" % (seat, msg))
            continue
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        instances.append((seat, s))

    if not instances:
        return False
    if len(instances) == 1:
        seat, s = instances[0]
        return s.submit_single(seat, times, roomid, action)

    stop = threading.Event()
    ok = False
    with ThreadPoolExecutor(max_workers=len(instances)) as ex:
        futs = {
            ex.submit(s.submit_single, seat, times, roomid, action, stop): (seat, s)
            for seat, s in instances
        }
        taken = 0
        total = len(instances)
        for fut in as_completed(futs):
            seat, s = futs[fut]
            try:
                res = fut.result()
            except SeatTakenError:
                taken += 1
                logging.warning("座位 %s 已被他人预约, 跳过该备选" % seat)
                continue
            if res:
                ok = True
                stop.set()
                logging.info("座位 %s 成功, 停止其余并发" % seat)
                break
        if ok:
            return True
        if total and taken == total:
            raise SeatTakenError("全部备选座位均被他人预约")
        return ok


def debug(users, action=False):
    logging.info("[Debug] RESERVE_NEXT_DAY=%s" % RESERVE_NEXT_DAY)
    current_dow = bj_dayofweek(action)
    for index, user in enumerate(users):
        username = user["username"]
        if not username or username == PLACEHOLDER:
            logging.info("跳过未填充账号 (index %d)" % index)
            continue
        password = user["password"]
        times = user["time"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        daysofweek = user["daysofweek"]
        if type(seatid) == str:
            seatid = [seatid]
        if current_dow not in daysofweek:
            logging.info("%s: Today not set to reserve" % username)
            continue
        logging.info("----------- %s -- %s -- %s try -----------" % (username, times, seatid))
        s = reserve(sleep_time=SLEEPTIME, max_attempt=MAX_ATTEMPT,
                    enable_slider=ENABLE_SLIDER, reserve_next_day=RESERVE_NEXT_DAY,
                    captcha_type=CAPTCHA_TYPE)
        s.get_login_status()
        s.login(username=username, password=password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        try:
            suc = s.submit(times, roomid, seatid, action)
        except Exception as e:
            logging.error("Submit 崩溃: %s" % e)
            suc = False
        if suc:
            # 本通道不做签到调度; 顺带提醒 (debug 下单不算正式抢座, 但时段的
            # 签到义务仍按超星实际记录走, 提示一句没坏处)
            logging.info("[%s] debug 模式: 完成 (本通道不做签到调度)" % username)
            _notify_sign_reminder(username)
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(sleep_time=SLEEPTIME, max_attempt=MAX_ATTEMPT,
                enable_slider=ENABLE_SLIDER, reserve_next_day=RESERVE_NEXT_DAY,
                captcha_type=CAPTCHA_TYPE)
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve (GitHub)")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument("-m", "--method", default="reserve",
                        choices=["reserve", "debug", "room"], help="for debug")
    parser.add_argument("-a", "--action", action="store_true",
                        help="use --action if server is UTC (GitHub runner 必须加)")
    parser.add_argument("--offset", type=float, default=None,
                        help="首击时刻(秒), 覆盖 PACED_FIRE_OFFSET; 默认 8.8")
    parser.add_argument("--profile", default=None,
                        choices=["human", "fast"],
                        help="拟人档位: human=8.8s / fast=4.0s")
    args = parser.parse_args()

    if args.profile:
        HUMAN_PROFILE = args.profile
        _pf = _PROFILES[HUMAN_PROFILE]
        PACED_FIRE_OFFSET, FIRE_JITTER, STAGGER_LO, STAGGER_HI, PING_BASE, PING_JIT = _pf
    if args.offset is not None:
        PACED_FIRE_OFFSET = args.offset

    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
