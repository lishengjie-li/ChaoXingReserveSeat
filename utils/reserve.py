

from utils import AES_Encrypt, generate_captcha_key, verify_param
import json
from curl_cffi import requests
import re
import time
import logging
import datetime
import random
import hashlib
from urllib.parse import urlencode, unquote


# ================================================================
#  座位被他人预约 → 终止任务
# ================================================================
# 命中以下任一提示词即视为"被别人占了"，不再无意义狂刷，由上层终止任务
STOP_ON_SEAT_TAKEN = True

# 首击提交时刻随机抖动上限(秒): 实际提交 = fire_offset + random(0, FIRE_JITTER_SEC)
# 避免多账号呈现固定整数偏移(如清一色 4.0s), 也省去手动微调 fire_offset 尾数
FIRE_JITTER_SEC = 0.2

# 单次提交内验证码识别的最大重试次数 (2026-09-02 晚调高)。
# 正确拿到 captcha token 即跳出提交; 拿不到交空只会被服务端拒(无意义), 故识别连续失败时放弃本轮、重新走 token+验证码。
_CAPTCHA_RETRY_MAX = 8


class SeatTakenError(Exception):
    """座位已被他人预约，上层应据此终止任务"""
    pass


class CredentialError(Exception):
    """账号或密码错误：重试无意义，抢座应直接终止"""
    pass


class ViolationLimitError(Exception):
    """违约次数上限，上层应据此终止任务"""
    pass


class AccountBannedError(Exception):
    """账号被限制使用(封禁)，上层应永久跳过该账号，不再无意义重试"""
    pass


SEAT_TAKEN_HINTS = (
    "被预约", "被占", "被别人", "被占用",
    "已满", "没有空位", "无空位", "已约满",
    "该座位已", "此座位已", "座位已被",
)


def get_date(day_offset: int = 0):
    today = datetime.datetime.now().date()
    return (today + datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")


# ================================================================
#  真实化改造 (2026-09-01, 依据抓包 + 服务器实测)
#  - submit 改 form 表单体 + 新参数集 (无 token/type/verifyData) + 新 Referer
#  - 补 12 步真实调用链 (entrance/config → ... → risk/check/config → getusedseatnums)
#  - 每账号固定一套"浏览器身份": 机型/schild/Accept-Language, 每天一致
#  - UA = App WebView 样式 (2026-09-01 下午已上线, 抓包逐字对齐);
#    旧注释"App UA 会切移动鉴权域"经 A/B/C 实测证伪, 作废
# ================================================================

_FID_ENC_DEFAULT = "4a18e12602b24c8c"   # deptId=22640(郑东校区) 的加密值, 抓包实测为固定值
_MAPP_ID = "19774897"                   # 座位应用 mappId (抓包 OAuth redirect_uri)
_APP_FID = "22640"                      # 郑东校区 fid (captcha/type 等接口的 appId 参数)

# curl_cffi 0.15.0 支持的桌面 Chrome 指纹 (TLS/JA3/HTTP2 与 UA 版本严格一致)
# 真实机型池 (model, android_ver, build) — 每账号确定性取一个
_DEVICE_POOL = (
    ("SM-G9910", "13", "TP1A.220624.014"),
    ("2210132C", "13", "TKQ1.220829.002"),
    ("PFJM10", "13", "TP1A.220905.001"),
    ("V2270A", "13", "TP1A.220624.014"),
    ("RMX3771", "14", "UP1A.231005.007"),
    ("SM-S9110", "14", "UP1A.231005.007"),
)
_CHROME_VERSION_POOL = (110,)   # TLS 指纹与 UA 的 Chrome/110 严格一致
_APP_UA_APPVER = "ChaoXingStudy_3_6.3.7_android_phone_10822_249"

# ================================================================
#  三层出口 (if 手机 elseif 电脑 else 服务器直连) — 2026-09-02
#  ⚠️ 2026-09-02(晚) 用户决策: 手机/PC 代理前两层暂时注释停用,
#    恢复正常服务器直连 (最快最稳, 不依赖手机在线)。代码保留不删,
#    日后需恢复手机住宅出口时, 取消下方注释并还原 PROXY_TIERS_R 即可。
#  原三层注释块(保留备查):
#    手机 gost SOCKS (经 WG 隧道) / PC 反向 SOCKS / 服务器直连。
#  用 socks5:// (服务器端解析 DNS), 不是 socks5h:// (手机 gost 无本地 DNS)。
# ================================================================
import os as _os
import socket as _socket
# ---- 2026-09-02(晚) 注释停用: 三层出口前两层 (恢复时取消下面注释) ----
# _TIER_PHONE_R = _os.environ.get("CX_SOCKS_PHONE", "socks5://10.7.0.2:1080")
# _TIER_PC_R    = _os.environ.get("CX_SOCKS_PC",    "socks5://127.0.0.1:1080")
# if _os.environ.get("HTTP_PROXY"):
#     PROXY_TIERS_R = [_os.environ["HTTP_PROXY"], None]
# else:
#     PROXY_TIERS_R = [_TIER_PHONE_R, _TIER_PC_R, None]   # None = 服务器直连(被拉黑时失败)
PROXY_TIERS_R = [None]   # 2026-09-02(晚): 仅服务器直连; 恢复三层出口时改回上方列表
_PICKED_PROXY_R = None   # 进程级缓存: probe 一次

def _pick_proxy_r():
    """probe 出口层, 返回第一个 TCP 可达的代理 URL (None=直连)。进程级缓存。
    当前 PROXY_TIERS_R=[None] -> 恒直连, 不走任何代理。"""
    global _PICKED_PROXY_R
    if _PICKED_PROXY_R is not None or _PICKED_PROXY_R == "":
        return _PICKED_PROXY_R if _PICKED_PROXY_R != "" else None
    for p in PROXY_TIERS_R:
        if p is None:
            _PICKED_PROXY_R = ""
            break
        try:
            hp = p.split("://")[-1]
            host = hp.split(":")[0]
            port = int(hp.split(":")[1])
            _s = _socket.create_connection((host, port), timeout=2)
            _s.close()
            _PICKED_PROXY_R = p
            break
        except Exception:
            continue
    logging.info(f"[proxy] picked exit: {_PICKED_PROXY_R or '(直连)'}")
    return _PICKED_PROXY_R if _PICKED_PROXY_R != "" else None


def _device_identity(seed: str):
    """从账号名确定性派生 App 设备身份: 机型/安卓版本/schild。同账号每天相同。"""
    h = hashlib.md5(("cx-client::" + seed).encode("utf-8")).hexdigest()
    n = int(h[:8], 16)
    model, av, build = _DEVICE_POOL[(n // 7) % len(_DEVICE_POOL)]
    return {"chrome": 110,
            "lang": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "model": model, "android": av, "build": build,
            "schild": hashlib.md5(("schild::" + seed).encode("utf-8")).hexdigest(),
            "schild2": hashlib.md5(("k::" + seed).encode("utf-8")).hexdigest()}

class reserve:
    # 兼容旧引用: 版本池已模块化为 _CHROME_VERSION_POOL (每账号固定取一个)
    _CHROME_POOL = (110,)

    def __init__(self, sleep_time=2, max_attempt=10, enable_slider=False,
                 reserve_next_day=False, captcha_type="auto", fast_mode=False,
                 fire_no_captcha=False):
        self.sleep_time = sleep_time
        self.max_attempt = max_attempt
        self.enable_slider = enable_slider
        self.reserve_next_day = reserve_next_day
        self.captcha_type = captcha_type
        # 极速模式: 抢座期关闭"拟人延迟"，改为近 0 延迟狂点
        self.fast_mode = fast_mode
        # 开抢首击跳过验证码(实时性优先): 仅在 20:00 首击不解析滑块, 重试时按需补
        self.fire_no_captcha = fire_no_captcha

        # ---- 浏览器身份 (登录后按账号名重派生并重建 Session; 默认先用池首个) ----
        self._fid_enc = _FID_ENC_DEFAULT
        self._mapp_id = _MAPP_ID
        self._device = _device_identity("")
        self._accept_lang = self._device["lang"]
        self._chrome_ver = self._device["chrome"]
        self._chrome_full = "110.0.5481.154"
        self._sec_ch_ua = self._mk_sec_ch_ua()

        # ---- 三 Session 隔离 (TLS 指纹版本 = 该账号固定 Chrome 版本) ----
        self._sess_login = requests.Session(impersonate=f"chrome{self._chrome_ver}")
        self._sess_op = requests.Session(impersonate=f"chrome{self._chrome_ver}")
        self._sess_captcha = requests.Session(impersonate=f"chrome{self._chrome_ver}")
        for s in (self._sess_login, self._sess_op, self._sess_captcha):
            s.timeout = 15
        # 三层出口: 给三个 Session 都设上 probe 出的最佳代理
        _p = _pick_proxy_r()
        if _p:
            for s in (self._sess_login, self._sess_op, self._sess_captcha):
                s.proxies = {"http": _p, "https": _p}
        self.requests = self._sess_op  # 兼容外部 main.py 的 self.requests.headers 设置

        # ---- URL ----
        self.login_page = "https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid="
        self.url = "https://office.chaoxing.com/front/third/apps/seat/code?id={}&seatNum={}"
        self.submit_url = "https://office.chaoxing.com/data/apps/seat/submit"
        self.seat_url = "https://office.chaoxing.com/data/apps/seat/getusedtimes"
        self.login_url = "https://passport2.chaoxing.com/fanyalogin"
        self.home_url = "https://office.chaoxing.com/front/third/apps/seat/index"

        # ---- 状态 ----
        self.token = ""
        self.success_times = 0
        self.fail_dict = []
        self.submit_msg = []
        self._stale_session = False
        self._username = ""
        self._password = ""
        # 最近一次成功的 Result dict (含 data.seatReserve.seatNum/startTime/id).
        # 供 main.py 调度签到用真实值; 失败时为 None.
        self.last_result = None

        # ---- 首击节奏控制(reserve 主流程设置; None=不卡点, debug/fill_morning 不受影响) ----
        self.pace_target_ts = None   # submit POST 目标发出的绝对时刻(epoch)
        self._pace_used = False      # 首次卡点用完即置 True, 后续重试/兜底座位不再卡

        self.login_headers = self._mk_login_headers()
        self.captcha_headers = self._mk_captcha_headers()

    # ================================================================
    #  Headers — App WebView 样式 (导航/XHR 分开, 抓包逐项对齐)
    # ================================================================

    def _ua(self):
        d = self._device
        return (f"Mozilla/5.0 (Linux; Android {d['android']}; {d['model']} Build/{d['build']}; wv) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                f"Chrome/{self._chrome_full} Safari/537.36 "
                f"(schild:{d['schild']}) (device:{d['model']}) Language/zh_CN "
                f"com.chaoxing.mobile/{_APP_UA_APPVER} (@Kalimdor)_{d['schild2']}")

    def _mk_sec_ch_ua(self):
        return None   # App WebView 不发客户端提示头

    def _init_device_identity(self, seed: str):
        """按账号名派生浏览器身份 (固定 Chrome 版本/语言), 重建 Session + headers 缓存。
        种子未变化时只刷 headers, 不重建 Session (保留预热 cookie)。"""
        if getattr(self, "_device_seed", None) == seed:
            self.login_headers = self._mk_login_headers()
            self.captcha_headers = self._mk_captcha_headers()
            return
        self._device_seed = seed
        self._device = _device_identity(seed)
        self._accept_lang = self._device["lang"]
        self._chrome_ver = self._device["chrome"]
        self._chrome_full = "110.0.5481.154"
        self._sec_ch_ua = self._mk_sec_ch_ua()
        ver = self._chrome_ver
        self._sess_login = requests.Session(impersonate=f"chrome{ver}")
        self._sess_op = requests.Session(impersonate=f"chrome{ver}")
        self._sess_captcha = requests.Session(impersonate=f"chrome{ver}")
        for s in (self._sess_login, self._sess_op, self._sess_captcha):
            s.timeout = 15
        _p = _pick_proxy_r()
        if _p:
            for s in (self._sess_login, self._sess_op, self._sess_captcha):
                s.proxies = {"http": _p, "https": _p}
        self.requests = self._sess_op
        self.login_headers = self._mk_login_headers()
        self.captcha_headers = self._mk_captcha_headers()

    def _mk_login_headers(self):
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": self._accept_lang,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "User-Agent": self._ua(),
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Host": "passport2.chaoxing.com",
            "Origin": "https://passport2.chaoxing.com",
        }

    def _mk_captcha_headers(self):
        # App WebView 样式 (抓包 11:15:20): X-Requested-With: com.chaoxing.mobile,
        # Sec-Fetch-Site: same-site, 无 Cache-Control/Pragma, Referer office 根
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": self._accept_lang,
            "Connection": "keep-alive",
            "Host": "captcha.chaoxing.com",
            "User-Agent": self._ua(),
            "X-Requested-With": "com.chaoxing.mobile",
            "Referer": "https://office.chaoxing.com/",
            "Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
        }

    def _mk_op_headers(self, host="office.chaoxing.com", referer=None):
        # 页面导航请求 (App WebView 样式, 抓包 select 页导航):
        # accept-language: zh_CN + X-Requested-With: com.chaoxing.mobile, 无 Cache-Control
        h = {
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                       "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh_CN",
            "Connection": "keep-alive",
            "Host": host, "User-Agent": self._ua(),
            "X-Requested-With": "com.chaoxing.mobile",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            h["Referer"] = referer
        return h

    def _mk_app_xhr_headers(self, referer=None, jquery=False):
        # XHR 请求 (真实 H5 页面同款)。jquery=True → jQuery 风格 Accept:
        # 真实客户端 GET XHR + risk/check/config + captcha/type 用该串; submit 等 action POST 用 */*
        h = {
            "Accept": ("application/json, text/javascript, */*; q=0.01"
                       if jquery else "*/*"),
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": self._accept_lang,
            "Connection": "keep-alive",
            "User-Agent": self._ua(),
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if referer:
            h["Referer"] = referer
        return h

    # ================================================================
    #  Delay
    # ================================================================

    def _human_delay(self, lo=0.3, hi=2.0):
        # 已按需求关闭全部"拟人随机延迟": 不再 sleep, 直接返回以压低提交延迟
        return

    def _human_long_delay(self, lo=0.5, hi=3.5):
        # 已按需求关闭全部"拟人随机延迟"
        return

    def _backoff(self, attempt):
        # 仅保留极小固定退避(非随机、非拟人)避免登录/令牌失败时被服务器限流或锁号。
        # 此 50ms 对"首击 <1s"目标无影响; 若坚持完全归零, 把下面这行改成 pass 即可。
        time.sleep(0.05)
        return

    def _pace(self):
        """First-shot pacing: sleep until pace_target_ts just before submit POST.
        Only effective when pace_target_ts is set and not yet used (reserve first-round first-shot).
        debug / fill_morning do not set pace_target_ts, so they are unaffected.
        If current time >= target -> submit immediately, mark used, full speed for retries."""
        if not self.pace_target_ts or self._pace_used:
            return
        # 首击时刻叠加随机抖动: 同一账号每天、每次首击都略有不同, 不再暴露固定整数偏移
        _jit_target = self.pace_target_ts + random.uniform(0, FIRE_JITTER_SEC)
        rem = _jit_target - time.time()
        if rem <= 0:
            logging.info(f"[pacing] past target {-rem:.2f}s, submit immediately")
            self._pace_used = True
            return
        logging.info(f"[pacing] sleep {rem:.2f}s to target "
                     f"{time.strftime('%H:%M:%S', time.localtime(_jit_target))}"
                     f" (+jitter {(_jit_target - self.pace_target_ts) * 1000:.0f}ms)")
        time.sleep(rem)
        self._pace_used = True
    # ================================================================
    #  Token — 柔性匹配, 不硬编码后缀
    # ================================================================

    def _extract_token(self, html: str) -> str:
        """多模式 token 提取, fallback 到宽松正则"""
        # 模式1: JS 赋值 token = '...'
        m = re.search(r"token\s*=\s*'([^']+)'", html)
        if m: return m.group(1)
        # 模式2: JSON "token":"..."
        m = re.search(r'"token"\s*:\s*"([^"]+)"', html)
        if m: return m.group(1)
        # 模式3: 开头全hex + _ + 数字 (限6-12位)
        m = re.search(r'([a-f0-9]{32}_\d{6,12})', html)
        if m: return m.group(1)
        # 模式4: 宽松版 (不限数字位数)
        m = re.search(r'([a-f0-9]{32}_\d+)', html)
        if m: return m.group(1)
        return ""

    def _get_page_token(self, url: str, require_value: bool = False):
        headers = self._mk_op_headers(referer=self.home_url)
        _t0 = time.perf_counter()
        resp = self._sess_op.get(url=url, headers=headers)
        _cost = (time.perf_counter() - _t0) * 1000
        logging.info(f"[计时] token GET 耗时 {_cost:.1f} ms")
        html = resp.content.decode("utf-8", errors="replace")
        token = self._extract_token(html)
        value = ""

        if require_value:
            vm = re.findall(r'value="(.*?)"', html)
            value = vm[0] if vm else ""
            if not token:
                logging.error(f"Token missing from {unquote(url)}")
                logging.error(f"  HTTP {resp.status_code}, final URL: {unquote(resp.url)}")
                # cookie 诊断（防御性：curl_cffi 的 cookies 可能混入非标准元素）
                try:
                    op_c = {}
                    for c in self._sess_op.cookies:
                        try:
                            if c.name.upper() in ('JSESSIONID', 'UID', 'UNAME'):
                                v = str(c.value)
                                op_c[c.name] = v[:30] + '...' if len(v) > 30 else v
                        except Exception:
                            continue
                    logging.error(f"  Op cookies: {op_c}")
                except Exception as e:
                    logging.error(f"  Cookie 诊断失败: {e}")
                logging.error(f"  HTML preview: {html[:500]}")
                # 检测 Session 过期/被重定向
                if "passport" in str(resp.url) or "login" in html[:500].lower() or len(html) < 200:
                    logging.error("  >>> Session 已过期! 标记刷新")
                    self._stale_session = True
                return "", ""
            if not value:
                logging.error(f"Submit value missing from {url}")
                return token, ""
        return token, value

    # ================================================================
    #  Login
    # ================================================================

    def _warmup(self):
        """预访问建立 cookie 上下文"""
        try:
            self._sess_op.get(self.home_url,
                              headers=self._mk_op_headers(referer="https://www.chaoxing.com/"))
        except Exception:
            pass
        self._human_long_delay(0.8, 2.0)
        try:
            self._sess_login.get(self.login_page, headers=self.login_headers)
        except Exception:
            pass
        self._human_delay(0.3, 1.0)

    def get_login_status(self):
        """外部接口, 等价于预热"""
        self._warmup()

    def _sync_cookies(self):
        """同步登录 cookie → 操作 session + 域名预热"""
        count = 0
        try:
            for c in self._sess_login.cookies:
                try:
                    domain = c.domain if c.domain else ".chaoxing.com"
                    self._sess_op.cookies.set(c.name, c.value, domain=domain, path=c.path)
                    # 真实客户端对 captcha.chaoxing.com 也带登录 cookie (抓包 11:15:20)
                    try:
                        self._sess_captcha.cookies.set(c.name, c.value, domain=domain, path=c.path)
                    except Exception:
                        pass
                    count += 1
                except Exception:
                    continue
        except Exception as e:
            logging.warning(f"[Cookie] 遍历出错: {e}")
        if count == 0:
            logging.warning("[Cookie] 未复制到任何 cookie, 尝试 fallback 方式...")
            # fallback: 直接从 response 的 Set-Cookie 头手动提取
            try:
                if hasattr(self._sess_login, 'cookies'):
                    jar = self._sess_login.cookies
                    for cookie_name in jar.keys():
                        try:
                            domain = ".chaoxing.com"
                            self._sess_op.cookies.set(cookie_name, jar.get(cookie_name),
                                                      domain=domain, path="/")
                            try:
                                self._sess_captcha.cookies.set(cookie_name, jar.get(cookie_name),
                                                               domain=domain, path="/")
                            except Exception:
                                pass
                            count += 1
                        except Exception:
                            continue
            except Exception as e2:
                logging.warning(f"[Cookie] fallback 也失败: {e2}")
        logging.info(f"[Cookie] 同步完成: {count} 个")
        # 域名预热: 让 office.chaoxing.com 设置 domain 级 cookie
        try:
            self._sess_op.get(self.home_url,
                              headers=self._mk_op_headers(referer="https://www.chaoxing.com/"),
                              timeout=10)
        except Exception:
            pass
        self._human_long_delay(0.5, 1.5)
        return count  # 返回同步数量，0=失败

    # ========== 2026-09-02: SSO 补链 + 学校常量注入 (登录态向 App 原生链对齐) ==========
    # 实证: 同校多账号间恒定不变的学校级配置 cookie (可注入, 与账号/设备无关); 来源见 cookie补全方案_20260902.md
    _SCHOOL_COOKIE_CONSTS = {
        "H5schoolId": "1399",
        "H5siteId": "8056",
        "sNameH5": "%E6%B2%B3%E5%8D%97%E8%B4%A2%E7%BB%8F%E6%94%BF%E6%B3%95%E5%A4%A7%E5%AD%A6",  # 河南财经政法大学
        "fidEncH5": "8866114b68a0fbe9",
        "spaceFid": "22640",
        "wfwfid": "22640",
        "source": "num99",
        "fidsCount": "2",
        "sso_role": "3",
        "_industry": "5",
        "wfwIncode": "zp27303",
        "manageCxid": "",
    }
    _SSO_URL = "https://sso.chaoxing.com/apis/login/userLogin4Uname.do?_from=passport"
    # App 原生层 Dalvik UA (抓包 SM-N9006 同款)。⚠️ SSO 接口按 UA 分流, Chrome/WebView UA 一律 500
    _UA_DALVIK = ("Dalvik/2.1.0 (Linux; U; Android 12; SM-N9006 Build/889bef5.2) "
                  "(schild:e396fd5f2c5d60595e03359206bdaec6) (device:SM-N9006) Language/zh_CN "
                  "com.chaoxing.mobile/ChaoXingStudy_3_6.3.7_android_phone_10822_249 (@Kalimdor)_"
                  "896b8253f21a4db59128c12d627ab2a8")

    def _sso_bridge(self):
        """fanyalogin 成功后补一发 App 原生 SSO 接口(空 body + Dalvik UA):
        增量下发 KI4SO_SERVER_EC/_industry/_tid/fidsCount/sso_puid/sso_role 等原生链 cookie。
        纯增量; 失败仅告警, 不影响登录主流程。"""
        try:
            # ⚠️ login() 会给 _sess_login 挂 passport2 域头(Host/Origin=passport2),
            #   直接打 sso 域会 Host 错乱 -> 500。此处临时清空 session 头, 只带 SSO 原生头。
            saved = self._sess_login.headers
            self._sess_login.headers = {}
            try:
                resp = self._sess_login.post(
                    self._SSO_URL, data=None,
                    headers={"User-Agent": self._UA_DALVIK,
                             "Accept-Language": "zh_CN",
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Host": "sso.chaoxing.com"},
                    timeout=10)
            finally:
                self._sess_login.headers = saved
            if resp.status_code == 200:
                # 将 sso 域新 set 的 cookie 同步到 op/captcha session (含增量键)
                # curl_cffi 0.15 jar 遍历 yield str 而非 Cookie 对象 -> 用 keys()/get() 兼容
                n = 0
                try:
                    for name in self._sess_login.cookies.keys():
                        val = self._sess_login.cookies.get(name)
                        for tgt in (self._sess_op, self._sess_captcha):
                            try:
                                tgt.cookies.set(name, val, domain=".chaoxing.com", path="/")
                                n += 1
                            except Exception:
                                pass
                except Exception:
                    pass
                logging.info(f"[SSO] 补链成功, 同步 cookie {n} 项")
            else:
                logging.warning(f"[SSO] 补链返回 HTTP {resp.status_code}, 跳过")
        except Exception as e:
            logging.warning(f"[SSO] 补链跳过(不影响登录): {e}")
        return

    _UC_HOME = "https://uc.chaoxing.com/mobile/homePage?fid=22640&incode=zp27303"  # 河南财经门户首页
    _OAUTH_URL = ("https://auth.chaoxing.com/connect/oauth2/authorize?appid=f587c8f1e8ec4516a61e53f752209495"
                  "&redirect_uri=https%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fapps%2Fseat%2Findex"
                  "%3Fuid%3D0%26fidEnc%3D4a18e12602b24c8c%26mappId%3D19774897"
                  "&response_type=code&scope=snsapi_base&state=22640")

    def _uc_bridge(self):
        """GET 学校门户首页(uc, WebView UA) -> 服务端直接 set wfwEnc/wfwfid/wfwIncode/spaceFid/jrose/source。
        wfwEnc 账号级真值, 实测 fanyalogin 后这一步即得, 无需复刻 wfw 门户跳转链。失败仅告警。"""
        try:
            resp = self._sess_op.get(self._UC_HOME,
                                     headers=self._mk_op_headers(host="uc.chaoxing.com",
                                                                 referer="https://www.chaoxing.com/"),
                                     timeout=10)
            logging.info(f"[UC] 门户首页 HTTP {resp.status_code}, wfw/space cookie 下发")
        except Exception as e:
            logging.warning(f"[UC] 门户首页跳过(不影响登录): {e}")
        return

    def _oauth_bridge(self):
        """auth OAuth authorize -> 302 回 office seat/index (带 code) -> set oa_uid/oa_name/oa_enc/oa_deptid
        与 INGRESSCOOKIE。洁癖项, 吞错。"""
        try:
            resp = self._sess_op.get(self._OAUTH_URL,
                                     headers=self._mk_op_headers(host="auth.chaoxing.com",
                                                                 referer=self._app_ref_index()),
                                     timeout=12, allow_redirects=True)
            logging.info(f"[OAUTH] authorize 链 HTTP {resp.status_code}")
        except Exception as e:
            logging.warning(f"[OAUTH] authorize 跳过: {e}")
        return

    def _inject_school_cookies(self):
        """注入实证的同校恒定学校级 cookie (.chaoxing.com 域, 纯配置, 幂等)"""
        try:
            n = 0
            for name, value in self._SCHOOL_COOKIE_CONSTS.items():
                for s in (self._sess_op, self._sess_captcha):
                    try:
                        s.cookies.set(name, value, domain=".chaoxing.com", path="/")
                        n += 1
                    except Exception:
                        pass
            logging.info(f"[Cookie] 注入学校常量 {n} 项")
        except Exception as e:
            logging.warning(f"[Cookie] 学校常量注入跳过: {e}")
        return

    def prewarm_fire(self, roomid, seat):
        """开抢前 1 秒调用: 对 office.chaoxing.com 的某个座位页做一次轻量 GET,
        让 TCP+TLS 连接保持温热, 避免 20:00 当场握手拖慢首击。
        同时补打真实客户端"选座页链" (room/info + risk/check/config + captcha/type),
        使 20:00 的 submit 前已有完整调用链, 对齐抓包顺序。"""
        self.app_select_chain(roomid, seat)
        try:
            # 热身 GET 也换成 App 同款 select 页 (响应内容无关紧要, 只要 TCP+TLS 热)
            self._sess_op.get(
                self._app_ref_select(roomid, seat, self._app_day()),
                headers=self._mk_op_headers(referer=self._app_ref_index()),
                timeout=5,
            )
            logging.info(f"[预热] 连接已热身 room={roomid} seat={seat}")
        except Exception as e:
            logging.warning(f"[预热] 连接热身失败(忽略, 首击会自动重连): {e}")

    def keepalive_ping(self):
        """[2026-09-06 α] 空等期心跳保活: GET office.chaoxing.com 首页,
        让 TCP keep-alive 连接不被 NAT/超星回收。配合 main.py 空等循环每 45-75s 抖动调用。
        - 打 office 首页 (20:00 前可访问, 不碰预约页, 不依赖开放窗口)
        - 全吞错 (心跳失败无害, 抢座时 _get_page_token 会自重建连接)
        - 目的: 避免 lc 类 token GET 4.27s 现场重握手 (冷连接 + 高峰慢)"""
        try:
            self._sess_op.get(
                "https://office.chaoxing.com/",
                headers=self._mk_op_headers(),
                timeout=5,
            )
        except Exception:
            pass

    # ================================================================
    #  拟真调用链 — 复刻学习通 App 预约流程 (全部吞错, 任何失败不影响抢座)
    #  真实顺序: 入口链(登录后) → 选座链(prewarm) → 验证码 → getusedseatnums → submit
    # ================================================================

    def _app_day(self):
        """预约目标日 (与 _do_submit 的 day 一致)"""
        dd = 1 if self.reserve_next_day else 0
        return str(datetime.date.today() + datetime.timedelta(days=dd))

    def _submit_day(self, action=False):
        """submit 实际预约日 (action=再顺延一天), 与 _do_submit 同一算法"""
        dd = 1 if self.reserve_next_day else 0
        return str(datetime.date.today()
                   + datetime.timedelta(days=(1 + dd) if action else dd))

    def _app_ref_index(self):
        return f"https://office.chaoxing.com/front/apps/seat/index?fidEnc={self._fid_enc}"

    def _app_ref_list(self):
        return f"https://office.chaoxing.com/front/apps/seat/list?deptIdEnc={self._fid_enc}"

    def _app_ref_select(self, roomid, seat, day):
        return (f"https://office.chaoxing.com/front/apps/seat/select"
                f"?id={roomid}&day={day}&seatNum={seat}&backLevel=1&fidEnc={self._fid_enc}")

    def app_entry_chain(self):
        """入口链 (登录后调用, 抓包 11:12:07~20): 完整复刻真实客户端打开预约页的行为序列。
        index 页→reserve/link/domain/rules+entrance/config+seat/index+identity/verify
        list 页→seat/config+curusedshow+levels+person/role+room/list
        只读接口, 注册一次正常进入路径, 全吞错。2026-09-02 行为拟合补全(原仅 3 个)。"""
        fe = self._fid_enc
        day = self._app_day()
        H = self._mk_app_xhr_headers
        s = self._sess_op
        ref_idx = self._app_ref_index()
        ref_lst = self._app_ref_list()
        # index 页 XHR (抓包: 进入预约首页后自动触发)
        for url, params in (
            ("https://office.chaoxing.com/data/apps/reserve/link/domain/rules", None),
            ("https://office.chaoxing.com/data/apps/seat/entrance/config",
             {"appType": "0", "fidEnc": fe}),
            ("https://office.chaoxing.com/data/apps/seat/index", {"fidEnc": fe}),
            ("https://office.chaoxing.com/data/apps/seat/identity/verify",
             {"mappId": self._mapp_id, "fidEnc": fe}),
        ):
            try:
                s.get(url, params=params, headers=H(ref_idx, jquery=True), timeout=8)
            except Exception:
                pass
        # list 页 XHR (抓包: 进入房间列表页后自动触发)
        for url, params in (
            ("https://office.chaoxing.com/data/apps/seat/config", {"fidEnc": fe}),
            ("https://office.chaoxing.com/data/apps/seat/curusedshow", {"fidEnc": fe}),
            ("https://office.chaoxing.com/data/apps/seat/levels",
             {"deptIdEnc": fe, "type": "0"}),
            ("https://office.chaoxing.com/data/apps/seat/person/role", {"fidEnc": fe}),
            ("https://office.chaoxing.com/data/apps/seat/room/list",
             {"cpage": "1", "pageSize": "100", "firstLevelName": "",
              "secondLevelName": "", "thirdLevelName": "",
              "day": day, "deptIdEnc": fe}),
        ):
            try:
                s.get(url, params=params, headers=H(ref_lst, jquery=True), timeout=8)
            except Exception:
                pass

    def app_select_chain(self, roomid, seat):
        """选座页链 (开抢前 1s 随 prewarm 调用, 抓包 11:15:16~17):
        room/info GET+POST → risk/check/config POST → captcha/type POST。"""
        fe = self._fid_enc
        day = self._app_day()
        ref_sel = self._app_ref_select(roomid, seat, day)
        H = self._mk_app_xhr_headers
        s = self._sess_op
        try:
            s.get("https://office.chaoxing.com/data/apps/seat/room/info",
                  params={"id": roomid, "fidEnc": fe},
                  headers=H(self._app_ref_index(), jquery=True), timeout=5)
        except Exception:
            pass
        try:
            s.post("https://office.chaoxing.com/data/apps/seat/room/info",
                   data=urlencode({"id": roomid, "toDay": day, "fidEnc": fe,
                                   "queryReserve": "true"}),
                   headers={**H(ref_sel),
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "Origin": "https://office.chaoxing.com"},
                   timeout=5)
        except Exception:
            pass
        # 真实顺序(抓包 11:15:17): risk/check/config → address → captcha/type
        # address 为 2026-09-02 比对抓包后补的缺失接口 (Accept */*, 与 risk/captcha 的 jquery 不同)
        for ep in ("risk/check/config", "captcha/type"):
            try:
                s.post(f"https://office.chaoxing.com/data/apps/seat/{ep}",
                       data=urlencode({"appType": "0", "appId": _APP_FID}),
                       headers={**H(ref_sel, jquery=True),
                                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                "Origin": "https://office.chaoxing.com"},
                       timeout=5)
            except Exception:
                pass
            if ep == "risk/check/config":
                try:
                    s.post("https://office.chaoxing.com/data/apps/seat/address",
                           data=urlencode({"deptId": _APP_FID, "roomId": roomid}),
                           headers={**H(ref_sel),
                                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                    "Origin": "https://office.chaoxing.com"},
                           timeout=5)
                except Exception:
                    pass
        # 补 seatgrid/roomid + reserve-window/check (抓包选座页加载自动触发, 2026-09-02 行为拟合)
        for url, params in (
            ("https://office.chaoxing.com/data/apps/seat/seatgrid/roomid",
             {"roomId": roomid, "fidEnc": fe}),
            ("https://office.chaoxing.com/data/apps/seat/room/reserve-window/check",
             {"roomId": roomid, "day": day, "deptIdEnc": fe, "fidEnc": fe}),
        ):
            try:
                s.get(url, params=params, headers=H(ref_sel, jquery=True), timeout=5)
            except Exception:
                pass

    def app_getusedseatnums(self, roomid, seat, day):
        """提交前最后一查 (抓包 11:15:17, 位于验证码之前、submit 之前)。
        2026-09-02 比对抓包修正: 真实端点为 getusedtimes(带 seatNum),
        而非 getusedseatnums(选座页加载时调, 参数不同)。"""
        ref_sel = self._app_ref_select(roomid, seat, day)
        try:
            self._sess_op.post(
                "https://office.chaoxing.com/data/apps/seat/getusedtimes",
                data=urlencode({"roomId": roomid, "seatNum": seat,
                                "day": day, "fidEnc": self._fid_enc}),
                headers={**self._mk_app_xhr_headers(ref_sel),
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                         "Origin": "https://office.chaoxing.com"},
                timeout=5)
        except Exception:
            pass

    def login(self, username: str, password: str):
        self._username = username
        self._password = password
        # 按账号派生浏览器身份 (固定 Chrome 版本/语言, 每天一致), 重建 Session + headers
        self._init_device_identity(username)
        parm = {
            "fid": -1,
            "uname": AES_Encrypt(username),
            "password": AES_Encrypt(password),
            "refer": "http%3A%2F%2Foffice.chaoxing.com%2Ffront%2Fthird%2Fapps%2Fseat%2Fcode%3Fid%3D4219%26seatNum%3D380",
            "t": True,
        }
        # 每次登录前重新生成 headers, 确保 UA 和指纹一致（刷新 Session 后可能变了）
        self.login_headers = self._mk_login_headers()
        for attempt in range(3):
            self._sess_login.headers = self.login_headers
            try:
                # 真实客户端 fanyalogin 为 form 表单体提交 (抓包), 不再用查询串
                resp = self._sess_login.post(url=self.login_url, data=parm)
                obj = resp.json()
                if obj.get("status"):
                    logging.info(f"User {username} login successfully")
                    cookies_synced = self._sync_cookies()
                    if cookies_synced == 0:
                        logging.warning(f"Login OK 但 cookie 同步 0 个! ({attempt + 1}/3)")
                        if attempt < 2:
                            self._backoff(attempt)
                        continue
                    self._stale_session = False
                    # 2026-09-02: SSO 补链 + 门户链 + 学校常量注入 (向 App 原生链对齐, 均吞错不影响登录)
                    try:
                        self._sso_bridge()
                        self._uc_bridge()
                        self._oauth_bridge()
                        self._inject_school_cookies()
                    except Exception:
                        pass
                    # 拟真: 登录成功后立即走一遍真实客户端"入口链" (只读接口, 吞错)
                    try:
                        self.app_entry_chain()
                    except Exception:
                        pass
                    return (True, "")
                else:
                    msg2 = obj.get("msg2", "?")
                    logging.warning(f"Login fail ({attempt + 1}/3): {msg2}")
                    if any(k in str(msg2) for k in ("密码", "用户名", "账号")):
                        raise CredentialError(str(msg2))
            except CredentialError:
                raise
            except Exception as e:
                logging.warning(f"Login err ({attempt + 1}/3): {e}")
            if attempt < 2:
                self._backoff(attempt)
        return (False, "max retries exceeded")

    def _refresh_session(self):
        """全量重建 Session + 重新登录, 用于 token 持续为空时自动恢复"""
        if not self._username or not self._password:
            logging.error("[Session] 无缓存凭证, 无法刷新")
            return False
        logging.warning("[Session] 重建所有 Session 并重新登录...")
        # 重建 Session/headers 全部收口在 _init_device_identity (按账号固定版本)
        self._init_device_identity(self._username)
        self._warmup()
        ok, msg = self.login(self._username, self._password)
        if ok:
            logging.info("[Session] 刷新成功")
        else:
            logging.error(f"[Session] 刷新失败: {msg}")
        return ok

    # ================================================================
    #  Room Query
    # ================================================================

    def roomid(self, encode: str):
        url = (f"https://office.chaoxing.com/data/apps/seat/room/list"
               f"?cpage=1&pageSize=100&firstLevelName=&secondLevelName="
               f"&thirdLevelName=&deptIdEnc={encode}")
        data = self._sess_op.get(url=url, headers=self._mk_op_headers()).content.decode("utf-8")
        obj = json.loads(data)
        for i in obj.get("data", {}).get("seatRoomList", []):
            print(f'{i["firstLevelName"]}-{i["secondLevelName"]}-{i["thirdLevelName"]} id={i["id"]}')

    # ================================================================
    #  Captcha — Slider
    # ================================================================

    def _fetch_captcha_raw(self, captcha_type="slide"):
        ts = int(time.time() * 1000)
        key, tok = generate_captcha_key(ts)
        rid, sid = random.randint(1000, 9999), f"{random.randint(1, 999):04d}"
        referer = f"https://office.chaoxing.com/front/third/apps/seat/code?id={rid}&seatNum={sid}"
        cb = f"jQuery{random.randint(111111111, 999999999)}_{ts}"
        params = {
            "callback": cb, "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": captcha_type, "version": "1.1.18",
            "captchaKey": key, "token": tok, "referer": referer,
            "_": ts, "d": "a", "b": "a",
        }
        try:
            resp = self._sess_captcha.get(
                "https://captcha.chaoxing.com/captcha/get/verification/image",
                params=params, headers=self._mk_captcha_headers())
            raw = resp.text
            return json.loads(raw.replace(cb + "(", "").replace(")", ""))
        except Exception as e:
            logging.warning(f"[Captcha] raw fetch fail: {e}")
            return None

    def resolve_captcha(self):
        """外部入口, 解滑块验证码, 返回 validate token"""
        logging.info("Start to resolve captcha token")
        data = self._fetch_captcha_raw("slide")
        if not data:
            return ""
        try:
            captcha_token = data["token"]
            bg_url = data["imageVerificationVo"]["shadeImage"]
            tp_url = data["imageVerificationVo"]["cutoutImage"]
        except KeyError:
            logging.error("[Captcha] parse fail")
            return ""
        logging.info(f"Successfully get prepared captcha_token {captcha_token}")
        logging.info(f"[Captcha] bg={bg_url}")
        logging.info(f"[Captcha] tp={tp_url}")

        x = self._calc_slide_distance(bg_url, tp_url)
        
        logging.info(f"Successfully calculate the captcha distance {x}")

        return self._submit_captcha_result(captcha_token, x)

    def _calc_slide_distance(self, bg_url: str, tp_url: str) -> int:
        import numpy as np
        import cv2

        def _cut_slide(data):
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            mask = img[:, :, 3].copy()
            mask[mask != 0] = 255
            x, y, w, h = cv2.boundingRect(mask)
            return img[y:y + h, x:x + w, :3]

        ch = self._mk_captcha_headers()
        ch["Host"] = "captcha-b.chaoxing.com"
        bgc = self._sess_captcha.get(bg_url, headers=ch)
        tpc = self._sess_captcha.get(tp_url, headers=ch)
        bg_img = cv2.imdecode(np.frombuffer(bgc.content, np.uint8), cv2.IMREAD_COLOR)
        tp_img = _cut_slide(tpc.content)

        # 直接模板匹配 (Canny 边缘检测在低对比度图上不可靠)
        res = cv2.matchTemplate(bg_img, tp_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        logging.info(f"[Captcha] raw match max_val={max_val:.4f}, x={max_loc[0]}")
        if max_val < 0.3:
            logging.warning(f"[Captcha] 匹配置信度过低 ({max_val:.3f}), 尝试边缘匹配")
            bg_e = cv2.Canny(bg_img, 100, 200)
            tp_e = cv2.Canny(tp_img, 100, 200)
            res = cv2.matchTemplate(cv2.cvtColor(bg_e, cv2.COLOR_GRAY2RGB),
                                    cv2.cvtColor(tp_e, cv2.COLOR_GRAY2RGB),
                                    cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            logging.info(f"[Captcha] edge match max_val={max_val:.4f}, x={max_loc[0]}")
        return max_loc[0]

    def _submit_captcha_result(self, captcha_token: str, x: int) -> str:
        """提交验证码结果, 使用简单 [{"x": x}] 格式 (超星不接受复杂轨迹)"""
        cb = f"jQuery{random.randint(100000000, 999999999)}_{int(time.time() * 1000)}"
        params = {
            "callback": cb, "captchaId": "42sxgHoTPTKbt0uZxPJ7ssOvtXr3ZgZ1",
            "type": "slide", "token": captcha_token,
            "textClickArr": json.dumps([{"x": x}]),
            "coordinate": json.dumps([]), "runEnv": "10", "version": "1.1.18",
            "_": int(time.time() * 1000),
        }
        resp = self._sess_captcha.get(
            "https://captcha.chaoxing.com/captcha/check/verification/result",
            params=params, headers=self._mk_captcha_headers())
        text = resp.text.replace(cb + "(", "").replace(")", "")
        data = json.loads(text)
        try:
            validate = json.loads(data.get("extraData", "{}")).get("validate", "")
        except Exception:
            validate = ""
        logging.info(f"[Captcha] validate={validate}, result={data.get('result')}")
        return validate

    # ================================================================
    #  Submit
    # ================================================================

    def submit(self, times, roomid, seatid, action):
        """多座位串行兜底(备选座位): 当前座位被他人占 → 立即试下一个;
        仅当所有备选都被他人预约时才抛 SeatTakenError 交由上层终止"""
        if isinstance(seatid, str):
            seatid = [seatid]
        all_taken = True
        tried = False
        for seat in seatid:
            tried = True
            try:
                if self.submit_single(seat, times, roomid, action):
                    return True
            except SeatTakenError:
                logging.warning(f"座位 {seat} 被他人预约, 尝试下一个备选...")
                continue
            # submit_single 返回 False = 瞬错/未成, 不算"被占"
            all_taken = False
        if tried and all_taken:
            raise SeatTakenError("全部备选座位均被他人预约")
        return False

    def submit_single(self, seat, times, roomid, action, stop_event=None):
        """单个座位的提交尝试 (含 token 获取 + 验证码 + 重试)"""
        consecutive_token_fails = 0
        session_refresh_count = 0

        for attempt in range(self.max_attempt):
            if stop_event is not None and stop_event.is_set():
                logging.info(f"Seat {seat}: 已有其它座位成功, 停止")
                return False
            # token 持续为空 → 立即全量刷新 Session
            if consecutive_token_fails >= 8 and session_refresh_count < 2:
                logging.warning(f"Token 连续 {consecutive_token_fails} 次为空, "
                                f"刷新 Session (第 {session_refresh_count + 1}/2 次)")
                if self._refresh_session():
                    consecutive_token_fails = 0
                    session_refresh_count += 1
                    self._human_long_delay(1.0, 3.0)
                    continue
                else:
                    logging.error("Session 刷新失败, 放弃本轮")
                    break

            # Token 页 = App 同款 H5 select 页 (抓包+JS 反解: enc 盐 = 页内 id=submit_enc,
            # 提取逻辑与 code 页完全兼容)。
            # [2026-09-09 关闭回退] code 页(front/third)回退从未补位成功: 触发场景=select 无 token
            # (典型: 未开放窗口), 此时 code 页同样无 token; 已约时 code 反而拿空(12148B)。
            # 实测 select 覆盖 未约/已约/未开放 各状态, token 仅取自此页。原回退代码注释保留供审。
            token, value = self._get_page_token(
                self._app_ref_select(roomid, seat, self._submit_day(action)),
                require_value=True,
            )
            # if not token:
            #     token, value = self._get_page_token(
            #         self.url.format(roomid, seat), require_value=True
            #     )
            if not token:
                consecutive_token_fails += 1
                logging.warning(f"No token (try {attempt + 1}, consecutive: {consecutive_token_fails})")
                if self._stale_session:
                    logging.warning("Session 已过期, 立即刷新")
                    self._stale_session = False
                    if self._refresh_session():
                        consecutive_token_fails = 0
                        session_refresh_count += 1
                        self._human_long_delay(1.0, 2.5)
                    continue
                self._backoff(attempt)
                continue

            consecutive_token_fails = 0
            logging.info(f"Get token: {token}")

            captcha = ""
            if self.enable_slider:
                # 每次提交都必须带验证码: 识别失败重试 _CAPTCHA_RETRY_MAX 次;
                # 拿不到 token 交空只会被服务端拒(无意义), 改为放弃本轮、重新走 token+验证码。
                for _c in range(_CAPTCHA_RETRY_MAX):
                    captcha = self.resolve_captcha()
                    if captcha:
                        break
                    logging.warning(f"Captcha 识别失败(第 {_c+1}/{_CAPTCHA_RETRY_MAX} 次), 重试...")
                if captcha:
                    logging.info(f"Captcha token {captcha}")
                else:
                    logging.warning(f"Captcha 连续 {_CAPTCHA_RETRY_MAX} 次识别失败, 放弃本轮提交(不交空)")
                    self._human_delay(1.0, 2.0)
                    continue

            # 拟真: 提交前最后一查 (真实客户端在验证码之后、submit 之前必调)
            if captcha:
                self.app_getusedseatnums(roomid, seat, self._app_day())

            self._pace()  # sleep until target, past target = submit immediately
            if self._do_submit(times, token, roomid, seat,
                               captcha, action, value):
                return True
            self._human_delay(1.0, 3.0)

        logging.warning(f"Seat {seat}: all attempts exhausted")
        return False

    def get_submit(self, url, times, token, roomid, seatid,
                   captcha="", action=False, value=""):
        """兼容旧接口"""
        return self._do_submit(times, token, roomid, seatid,
                               captcha, action, value)

    def _submit_legacy(self, times, token, roomid, seatid, captcha, day, value):
        """旧版提交参数集 (token/type/verifyData + 查询串) — 新参数集被拒时的兜底"""
        parm = {
            "roomId": roomid, "startTime": times[0], "endTime": times[1],
            "day": str(day), "seatNum": seatid, "captcha": captcha,
            "token": token, "type": "1", "verifyData": "1",
        }
        parm["enc"] = verify_param(parm, value)
        ref = self.url.format(roomid, seatid)
        hdrs = self._mk_app_xhr_headers(referer=ref)
        hdrs.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://office.chaoxing.com",
        })
        try:
            return self._sess_op.post(url=self.submit_url, params=parm,
                                      headers=hdrs).content.decode("utf-8")
        except Exception as e:
            logging.error(f"[Submit] legacy 回退失败: {e}")
            return None

    def _do_submit(self, times, token, roomid, seatid,
                    captcha="", action=False, value=""):
        day = self._submit_day(action)

        # ---- 新版参数集 (2026-09-01 抓包): form 表单体, 无 token/type/verifyData ----
        parm = {
            "deptIdEnc": "", "roomId": str(roomid),
            "startTime": times[0], "endTime": times[1],
            "day": day, "seatNum": str(seatid),
            "captcha": captcha, "wyToken": "",
        }
        parm["enc"] = verify_param(parm, value)

        logging.info(f"submit parameter {json.dumps(parm, ensure_ascii=False)}")

        # Referer 与真实客户端一致: 新版 H5 选座页 (含 fidEnc)
        ref = self._app_ref_select(roomid, seatid, day)
        hdrs = self._mk_app_xhr_headers(referer=ref)
        hdrs.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://office.chaoxing.com",
        })
        # 提交前不再插入任何故意延迟(拟人随机延迟已全部关闭)
        try:
            _t0 = time.perf_counter()
            raw = self._sess_op.post(
                url=self.submit_url, data=urlencode(parm), headers=hdrs
            ).content.decode("utf-8")
            _cost = (time.perf_counter() - _t0) * 1000
            logging.info(f"[计时] submit POST 耗时 {_cost:.1f} ms")
        except Exception as e:
            logging.error(f"Submit POST 失败: {e}")
            return False
        # 被限制使用页 (封禁重定向): 不回退、不重试解析, 直接失败
        if "blackReason" in raw or "限制使用" in raw:
            logging.error(f">>> 账号被限制使用! {raw[:200]}")
            raise AccountBannedError(raw[:120])
        # 保险丝: 新参数集若被服务端拒绝(返回非 JSON/HTML), 自动回退旧版查询串参数集一次
        if not raw.lstrip().startswith("{"):
            logging.warning("[Submit] 新参数集响应异常, 回退旧版参数集重试一次")
            raw = self._submit_legacy(times, token, roomid, seatid, captcha, day, value)
            if raw is None:
                return False
        try:
            result = json.loads(raw)
            self.submit_msg.append(f"{times[0]}~{times[1]}: {result}")
            logging.info(f"Result: {result}")
        except json.JSONDecodeError:
            logging.error(f"Bad JSON: {raw[:200]}")
            return False
        if result.get("success", False):
            self.last_result = result
            return True
        # 已有预约 = 本账号已占位, 视为达成目标, 停止重试
        msg = result.get("msg", "") or ""
        if ("已有预约" in msg) or ("已预约" in msg) or ("已经预约" in msg):
            logging.info(f"该时段已被本账号预约, 视为成功, 停止重试: {msg}")
            self.last_result = result   # 已有预约, 没有新 seatReserve 但至少记下 msg 供排查
            return True
        # 座位被他人预约 → 抛异常, 上层据此终止任务(不再无意义狂刷)
        if STOP_ON_SEAT_TAKEN and any(hint in msg for hint in SEAT_TAKEN_HINTS):
            logging.warning(f"[终止] 座位被他人预约: {msg}")
            raise SeatTakenError(msg)
        # 违约次数上限 -> 永久跳过
        if "违约" in msg:
            logging.warning(f"[终止] 违约次数上限: {msg}")
            raise ViolationLimitError(msg)
        return False
