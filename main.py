import json
import time
import argparse
import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


from utils import reserve, get_user_credentials
from utils.reserve import SeatTakenError

# ============================================================
#  极速版配置
# ============================================================
FAST_MODE = True           # 抢座期关闭"拟人延迟"，近 0 延迟狂点
CONCURRENT_SEATS = False   # 多座位串行兜底(当前被占→约下一个备用); 设 True 则并发抢(可能同时约中多个)
LOGIN_LEAD = 180           # 放票前多少秒开始登录预热(秒)，默认 3 分钟
OPEN_HOUR, OPEN_MIN, OPEN_SEC = 20, 0, 0    # 正式抢座放票时刻(北京时间) = 20:00:00 准时开抢
ENDTIME = "20:05:00"       # 放票后截止(北京时间) 5 分钟窗口
GRACE_SECONDS = 180       # 启动时已过 ENDTIME 的宽限尝试时长(秒): 用于过晚触发/手动测试, 避免一次都不抢就放弃

ENABLE_SLIDER = True       # 是否启用验证码
CAPTCHA_TYPE = "auto"      # 验证码类型: "slide" | "click" | "auto"
MAX_ATTEMPT = 3            # 单座位最大尝试次数(极速版可略多)
RESERVE_NEXT_DAY = False   # 预约明天而不是今天的
SLEEPTIME = 0.0            # 预留参数(已不生效，延迟由 FAST_MODE 控制)


# ============================================================
#  时间工具: 所有目标时间以"北京时间"表达
#  action=True  → 服务器为 UTC，需在本地时间上 +8 得到北京时间
#  action=False → 服务器已是中国时区，本地时间即北京时间
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
    """今天北京时间 (h,m,s) 对应的 epoch 秒；若已过期则返回过去的时间(立即抢)"""
    st = time.localtime(bj_now_ts(action))  # 字段已是北京时间
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
    )


def _warmup_login(user, index, action, usernames, passwords):
    """预热: 建立 session + 登录，返回已登录的 reserve 实例(失败返回 None)"""
    username, password = _get_creds(user, index, action, usernames, passwords)
    s = _make_reserve()
    s.get_login_status()
    ok, msg = s.login(username, password)
    if not ok:
        logging.error(f"预热登录失败 {username}: {msg}")
        return None
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    return s


# ============================================================
#  主流程
# ============================================================
def main(users, action=False):
    logging.info(
        f"极速版配置: FAST_MODE={FAST_MODE} CONCURRENT_SEATS={CONCURRENT_SEATS} "
        f"OPEN={OPEN_HOUR:02d}:{OPEN_MIN:02d}:{OPEN_SEC:02d} ENDTIME={ENDTIME} "
        f"LOGIN_LEAD={LOGIN_LEAD}s"
    )
    usernames, passwords = (get_user_credentials(action) if action else (None, None))
    current_dow = bj_dayofweek(action)
    today = [(i, u) for i, u in enumerate(users) if current_dow in u.get("daysofweek")]
    if not today:
        logging.info("今天没有安排抢座")
        return

    today_reservation_num = len(today)
    success_list = [False] * len(users)
    attempt_times = 0

    # ---- 2) 计算放票时刻, 并把登录预热控制在放票前 LOGIN_LEAD 秒内 ----
    open_e = target_epoch(OPEN_HOUR, OPEN_MIN, OPEN_SEC, action)
    warmup_at = open_e - LOGIN_LEAD
    pre = time.time()
    if pre < warmup_at:
        wait_pre = warmup_at - pre
        logging.info(f"距放票尚早, 先休眠 {wait_pre:.0f}s 后再登录预热(避免 session 过早过期)...")
        time.sleep(wait_pre)
    elif pre > open_e:
        logging.warning("启动已过放票时刻, 立即开抢!")
    else:
        logging.info("已临近放票, 直接登录预热")

    # ---- 1) 登录预热 ----
    logging.info(f"开始登录预热, 今日共 {today_reservation_num} 个账号...")
    warmed = {}  # index -> reserve 实例 (已登录)
    for i, user in today:
        s = _warmup_login(user, i, action, usernames, passwords)
        if s is not None:
            warmed[i] = s
            logging.info(f"  预热成功: {user.get('username', '?')}")
        else:
            logging.warning(f"  预热失败: {user.get('username', '?')}, 将在开抢前重试")

    # ---- 3) 精确等到放票时刻(准点提交) ----
    logging.info(f"等待到 {OPEN_HOUR:02d}:{OPEN_MIN:02d}:{OPEN_SEC:02d} (北京) 再开抢...")
    while True:
        remaining = open_e - time.time()
        if remaining <= 0:
            break
        if remaining > 0.1:
            time.sleep(0.03)
        # 最后 100ms 内忙等, 保证卡点精度
    logging.info(f"时间到! 当前 {bj_str(action)} 开始抢座!")

    # 开抢前再补一次未成功的登录
    for i, user in today:
        if i not in warmed:
            s = _warmup_login(user, i, action, usernames, passwords)
            if s is not None:
                warmed[i] = s

    end_e = target_epoch(*map(int, ENDTIME.split(":")), action)
    # 宽限: 若启动时已过 ENDTIME(手动触发过晚/调度延迟), 仍给予一段时间尝试,
    # 避免"一次都不抢就直接放弃"。正常 20:00 前触发的场景 end_e 在未来, 不受影响。
    if time.time() >= end_e:
        logging.warning(f"启动已过 ENDTIME({ENDTIME}), 改为宽限 {GRACE_SECONDS}s 内尝试抢座")
        end_e = time.time() + GRACE_SECONDS

    # ---- 3) 抢座循环, 直到全部成功或超时 ----
    while time.time() < end_e:
        attempt_times += 1
        logging.info(f"=== 第 {attempt_times} 轮 ({bj_str(action)}) ===")
        all_done = True
        for i, user in today:
            if success_list[i]:
                continue
            all_done = False
            username, password = _get_creds(user, i, action, usernames, passwords)
            _, times, roomid, seatid, daysofweek = (
                user.get("username"), user["time"], user["roomid"],
                user["seatid"], user["daysofweek"]
            )
            if isinstance(seatid, str):
                seatid = [seatid]

            s = warmed.get(i)
            if s is None:
                s = _warmup_login(user, i, action, usernames, passwords)
                if s is None:
                    logging.error(f"{username} 登录始终失败, 跳过")
                    continue
                warmed[i] = s

            try:
                if CONCURRENT_SEATS and len(seatid) > 1:
                    success_list[i] = _grab_concurrent(s, seatid, times, roomid, action)
                else:
                    success_list[i] = s.submit(times, roomid, seatid, action)
            except SeatTakenError:
                logging.warning(f"{username} 座位 {seatid} 已被他人预约, 终止任务!")
                return

            logging.info(f"{username} 本轮结果: {success_list[i]}")

        if all_done or sum(success_list) == today_reservation_num:
            logging.info("全部预约成功!")
            return

    logging.warning(f"到达截止时间仍未全部成功: {success_list}")


def _grab_concurrent(template, seatid, times, roomid, action):
    """多座位并发: 每个座位独立 session + 独立登录, 任一成功即停止其余"""
    logging.info(f"并发提交 {len(seatid)} 个座位...")
    instances = []
    for seat in seatid:
        s = _make_reserve()
        s.get_login_status()
        ok, msg = s.login(template._username, template._password)
        if not ok:
            logging.warning(f"座位 {seat} 实例登录失败: {msg}")
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
            ex.submit(s.submit_single, seat, times, roomid, action, stop): seat
            for seat, s in instances
        }
        taken = 0
        total = len(instances)
        for fut in as_completed(futs):
            try:
                res = fut.result()
            except SeatTakenError:
                taken += 1
                logging.warning(f"座位 {futs[fut]} 已被他人预约, 跳过该备选")
                continue
            if res:
                ok = True
                stop.set()
                logging.info(f"座位 {futs[fut]} 成功, 停止其余并发")
                break
        if ok:
            return True
        if total and taken == total:
            # 并发的所有座位都被他人占了 → 上层应终止
            raise SeatTakenError("全部备选座位均被他人预约")
        return ok


# ============================================================
#  Debug / Room 模式(保留, 不受极速模式影响)
# ============================================================
def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\n"
        f"ENABLE_SLIDER: {ENABLE_SLIDER}\nCAPTCHA_TYPE: {CAPTCHA_TYPE}\n"
        f"RESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = bj_dayofweek(action)
    for index, user in enumerate(users):
        username, password, times, roomid, seatid, daysofweek = user.values()
        if type(seatid) == str:
            seatid = [seatid]
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
            captcha_type=CAPTCHA_TYPE,
        )
        s.get_login_status()
        s.login(username=username, password=password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        try:
            suc = s.submit(times, roomid, seatid, action)
        except Exception as e:
            logging.error(f"Submit 崩溃: {e}")
            suc = False
        if suc:
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
        captcha_type=CAPTCHA_TYPE,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action if server is UTC (adds +8h to interpret times as Beijing); "
             "if server already uses China timezone, run WITHOUT --action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
