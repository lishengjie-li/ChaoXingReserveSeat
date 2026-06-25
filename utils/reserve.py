# -*- coding: utf-8 -*-
"""
超星图书馆座位预约
接口匹配 GitHub 原版 API (reserve, login, submit, get_submit, roomid)
增强: 三Session隔离 / 柔性Token提取 / 域名预热 / Session自动刷新
"""

from utils import AES_Encrypt, generate_captcha_key, verify_param
import json
from curl_cffi import requests
import re
import time
import logging
import datetime
import random


def get_date(day_offset: int = 0):
    today = datetime.datetime.now().date()
    return (today + datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")


class reserve:
    _CHROME_POOL = [120, 120, 110]  # GitHub Actions 老 Linux 不支持 124, 120/110 最稳定

    def __init__(self, sleep_time=2, max_attempt=6, enable_slider=False,
                 reserve_next_day=False, captcha_type="auto"):
        self.sleep_time = sleep_time
        self.max_attempt = max_attempt
        self.enable_slider = enable_slider
        self.reserve_next_day = reserve_next_day
        self.captcha_type = captcha_type

        # ---- 动态指纹 ----
        ver = random.choice(self._CHROME_POOL)
        self._chrome_ver = ver
        self._chrome_full = f"{ver}.0.0.0"
        self._accept_lang = random.choice([
            "zh-CN,zh;q=0.9,en;q=0.8", "zh-CN,zh-Hans;q=0.9,en-US;q=0.8",
        ])
        self._sec_ch_ua = f'"Chromium";v="{ver}", "Google Chrome";v="{ver}", "Not.A/Brand";v="99"'
        self._base_ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self._chrome_full} Safari/537.36")
        self._platform_literal = '"Windows"'

        # ---- 三 Session 隔离 ----
        self._sess_login = requests.Session(impersonate=f"chrome{ver}")
        self._sess_op = requests.Session(impersonate=f"chrome{ver}")
        self._sess_captcha = requests.Session(impersonate=f"chrome{ver}")
        for s in (self._sess_login, self._sess_op, self._sess_captcha):
            s.timeout = 15
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

        self.login_headers = self._mk_login_headers()
        self.captcha_headers = self._mk_captcha_headers()

    # ================================================================
    #  Headers
    # ================================================================

    def _mk_login_headers(self):
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": self._accept_lang,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "User-Agent": self._base_ua,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Host": "passport2.chaoxing.com",
            "Origin": "https://passport2.chaoxing.com",
            "Sec-Ch-Ua": self._sec_ch_ua,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": self._platform_literal,
        }

    def _mk_captcha_headers(self):
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": self._accept_lang,
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "Host": "captcha.chaoxing.com", "Pragma": "no-cache",
            "Referer": "https://office.chaoxing.com/",
            "Sec-Ch-Ua": self._sec_ch_ua,
            "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": self._platform_literal,
            "Sec-Fetch-Dest": "script", "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site", "User-Agent": self._base_ua,
        }

    def _mk_op_headers(self, host="office.chaoxing.com", referer=None):
        h = {
            "Accept": random.choice([
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            ]),
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": self._accept_lang,
            "Cache-Control": random.choice(["no-cache", "max-age=0"]),
            "Connection": "keep-alive",
            "Host": host, "User-Agent": self._base_ua,
            "Sec-Ch-Ua": self._sec_ch_ua,
            "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": self._platform_literal,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": random.choice(["none", "same-origin", "cross-site"]),
            "Sec-Fetch-User": "?1", "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            h["Referer"] = referer
        return h

    # ================================================================
    #  Delay
    # ================================================================

    def _human_delay(self, lo=0.3, hi=2.0):
        time.sleep(lo + (hi - lo) * random.betavariate(2, 5))

    def _human_long_delay(self, lo=0.5, hi=3.5):
        time.sleep(lo + (hi - lo) * random.betavariate(2, 4))

    def _backoff(self, attempt):
        wait = min(2 ** attempt, 60)
        time.sleep(wait + random.uniform(0, wait * 0.5))

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
        resp = self._sess_op.get(url=url, headers=headers)
        html = resp.content.decode("utf-8", errors="replace")
        token = self._extract_token(html)
        value = ""

        if require_value:
            vm = re.findall(r'value="(.*?)"', html)
            value = vm[0] if vm else ""
            if not token:
                logging.error(f"Token missing from {url}")
                logging.error(f"  HTTP {resp.status_code}, final URL: {resp.url}")
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

    def login(self, username: str, password: str):
        self._username = username
        self._password = password
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
                resp = self._sess_login.post(url=self.login_url, params=parm)
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
                    return (True, "")
                else:
                    logging.warning(f"Login fail ({attempt + 1}/3): {obj.get('msg2', '?')}")
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
        ver = random.choice(self._CHROME_POOL)
        self._chrome_ver = ver
        self._chrome_full = f"{ver}.0.0.0"
        self._sec_ch_ua = f'"Chromium";v="{ver}", "Google Chrome";v="{ver}", "Not.A/Brand";v="99"'
        self._base_ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self._chrome_full} Safari/537.36")
        self._sess_login = requests.Session(impersonate=f"chrome{ver}")
        self._sess_op = requests.Session(impersonate=f"chrome{ver}")
        self._sess_captcha = requests.Session(impersonate=f"chrome{ver}")
        for s in (self._sess_login, self._sess_op, self._sess_captcha):
            s.timeout = 15
        self.requests = self._sess_op
        # 刷新 headers 缓存, 避免 _warmup 用旧 UA
        self.login_headers = self._mk_login_headers()
        self.captcha_headers = self._mk_captcha_headers()
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
        x += random.randint(-2, 2)
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
        self._human_delay(0.5, 2.0)
        consecutive_token_fails = 0
        session_refresh_count = 0

        for seat in seatid:
            for attempt in range(self.max_attempt):
                # token 持续为空 → 立即全量刷新 Session
                if consecutive_token_fails >= 1 and session_refresh_count < 2:
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

                token, value = self._get_page_token(
                    self.url.format(roomid, seat), require_value=True
                )
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
                    captcha = self.resolve_captcha()
                    if captcha:
                        logging.info(f"Captcha token {captcha}")
                    else:
                        logging.warning("Captcha fail, 尝试无验证码提交")

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

    def _do_submit(self, times, token, roomid, seatid,
                    captcha="", action=False, value=""):
        dd = 1 if self.reserve_next_day else 0
        day = datetime.date.today() + datetime.timedelta(days=dd)
        if action:
            day = datetime.date.today() + datetime.timedelta(days=1 + dd)

        parm = {
            "roomId": roomid, "startTime": times[0],
            "endTime": times[1], "day": str(day),
            "seatNum": seatid, "captcha": captcha,
            "token": token, "type": "1", "verifyData": "1",
        }
        parm["enc"] = verify_param(parm, value)

        logging.info(f"submit parameter {json.dumps(parm, ensure_ascii=False)}")

        ref = self.url.format(roomid, seatid)
        hdrs = self._mk_op_headers(referer=ref)
        hdrs.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        self._human_delay(0.08, 0.35)
        try:
            raw = self._sess_op.post(
                url=self.submit_url, params=parm, headers=hdrs
            ).content.decode("utf-8")
        except Exception as e:
            logging.error(f"Submit POST 失败: {e}")
            return False
        try:
            result = json.loads(raw)
            self.submit_msg.append(f"{times[0]}~{times[1]}: {result}")
            logging.info(f"Result: {result}")
        except json.JSONDecodeError:
            logging.error(f"Bad JSON: {raw[:200]}")
            return False
        return result.get("success", False)