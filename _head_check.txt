#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zju_bridge.py — 浙大桥接服务（浙江大学研究生工作台专用）
====================================================

在本地启动一个小型 HTTP 服务（仅监听 127.0.0.1），为工作台网页提供浙大数据：

  统一身份认证(zjuam CAS) 登录
    └─ 学在浙大 courses.zju.edu.cn   : 课程列表 / 作业(含截止时间、提交状态) / 待办
    └─ 研究生系统 yjsy.zju.edu.cn   : 个人课表(周次/节次/地点) / 考试安排
  学院通知 saa.zju.edu.cn + saa.office.zju.edu.cn（需要校园网或 VPN）
  校历 calendar.celechron.top 缓存（社区维护的官方校历数据）

用法：
    python zju_bridge.py            # 启动服务并自动打开工作台
    python zju_bridge.py --mock     # 演示模式（假数据，不需要登录，用于预览界面）
    python zju_bridge.py --check    # 网络自检（检查各站点可达性）后退出
    python zju_bridge.py --port 8765 --no-browser

安全说明：
  - 服务只绑定 127.0.0.1，外部设备无法访问；
  - 勾选"记住密码"时，密码用 Windows DPAPI（绑定当前系统用户）加密后保存在本目录 config.json；
  - 登录会话 Cookie 保存在本目录 session.json（含你的登录态，请勿外传）；
  - 实现参考开源项目 Celechron / zju-learning-assistant / fiz 的公开接口，仅访问你本人有权访问的数据。
"""
import argparse
import base64
import ctypes
import json
import os
import re
import ssl
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from http import cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as up
from urllib import request as urlreq

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    # PyInstaller 打包成 exe 后：exe 位于工作台根目录
    ROOT_DIR = os.path.dirname(sys.executable)
    BRIDGE_DIR = os.path.join(ROOT_DIR, '04-代码与数据', 'zju-bridge')
else:
    BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.dirname(os.path.dirname(BRIDGE_DIR))      # 工作台根目录
CONFIG_PATH = os.path.join(BRIDGE_DIR, "config.json")
SESSION_PATH = os.path.join(BRIDGE_DIR, "session.json")
CACHE_PATH = os.path.join(BRIDGE_DIR, "cache.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 15

CAS_LOGIN = "https://zjuam.zju.edu.cn/cas/login"
CAS_PUBKEY = "https://zjuam.zju.edu.cn/cas/v2/getPubKey"
CAS_CAPTCHA = "https://zjuam.zju.edu.cn/cas/captcha.jpg"
COURSES = "https://courses.zju.edu.cn"
YJSY = "https://yjsy.zju.edu.cn"
YJSY_SERVICE = "https://yjsy.zju.edu.cn/"
WEBVPN = "https://webvpn.zju.edu.cn"
# 校园巴士（小白车/宝宝巴士）实时系统 bccx（小程序同源接口，公开无需登录）
CB_BASE = "https://bccx.zju.edu.cn/schoolbus_wx/"
SAA_SITES = ["https://saa.zju.edu.cn", "https://saa.office.zju.edu.cn"]
# 通知源：saa 学院官网（https 443 在部分网络被拒，实测 http 可达）+ 总务处公告（公开）
NOTICE_SOURCES = [
    ("http://saa.zju.edu.cn/67629/list.htm", "saa.zju.edu.cn"),
    ("http://saa.zju.edu.cn/67590/list.htm", "saa.zju.edu.cn"),
    ("http://saa.office.zju.edu.cn", "saa.office(内网)"),
    ("http://www.zwc.zju.edu.cn/ggtz/list.htm", "总务处公告"),
]
SEEN_PATH = os.path.join(BRIDGE_DIR, "seen.json")
STATE_PATH = os.path.join(BRIDGE_DIR, "state.json")
CALENDAR_BASE = "http://calendar.celechron.top"
CALENDAR_TTL = 6 * 3600
DATA_TTL = 300          # 业务数据缓存 5 分钟
NOTICE_TTL = 600        # 通知缓存 10 分钟
HW_TTL = 1800           # 作业列表缓存 30 分钟（每门课一次请求，比较贵）

COURSES_FIELDS = "id,name,course_code,teacher,photos,semester,start_date,end_date,is_ended"

SSL_CTX = ssl.create_default_context()

# 官方校历兜底数据（2026-2027 秋冬学期，与 calendar.celechron.top 同源结构）
EMBEDDED_CALENDAR_1 = {
    "sessionTime": [["00:00", "00:00"], ["08:00", "08:45"], ["08:50", "09:35"],
                    ["10:00", "10:45"], ["10:50", "11:35"], ["11:40", "12:25"],
                    ["13:25", "14:10"], ["14:15", "15:00"], ["15:05", "15:50"],
                    ["16:15", "17:00"], ["17:05", "17:50"], ["18:50", "19:35"],
                    ["19:40", "20:25"], ["20:30", "21:15"], ["21:20", "22:05"],
                    ["22:10", "22:55"]],
    "startEnd": ["20260914", "20261108", "20261109", "20270103"],
    "holiday": {"20260925": "中秋节", "20261001": "国庆节", "20261005": "国庆节"},
    "dummy": {"20260926": "中秋节", "20260927": "中秋节", "20261003": "国庆节",
              "20261004": "国庆节", "20270101": "元旦（待定）"},
    "exchange": {"2026100620260920": "国庆节", "2026100720261010": "国庆节",
                 "2026100220261017": "国庆节", "2026123120270104": "学生节"},
}


def MOCK_BUS():
    def item(i, bus, frm, to, dep, arr, cyc, note="", stops=None):
        return {"id": str(i), "bus": bus, "line": bus + frm + "-" + to, "from": frm, "to": to,
                "depart": dep, "arrive": arr, "cycle": cyc, "remark": note,
                "stations": stops or [{"name": frm, "time": dep}, {"name": to, "time": arr}]}
    return {"ok": True, "total": 6, "with_stops": 6, "fetched_at": "mock", "items": [
        item(1, "研究生1号班车", "紫金港校区", "玉泉校区（4舍南侧）", "06:50", "07:40", [1, 2, 3, 4, 5],
             "经停西溪", [{"name": "紫金港校区", "time": "06:50"}, {"name": "西溪校区", "time": "07:25"},
                        {"name": "玉泉校区（4舍南侧）", "time": "07:40"}]),
        item(2, "教师3号班车", "玉泉校区", "紫金港校区", "07:10", "07:50", [1, 2, 3, 4, 5]),
        item(3, "校区间1号线", "紫金港校区", "西溪校区", "08:00", "08:25", [1, 2, 3, 4, 5]),
        item(4, "研究生2号班车", "华家池校区", "紫金港校区", "06:50", "07:30", [1, 2, 3, 4, 5]),
        item(5, "海宁校区班车", "紫金港校区", "海宁国际校区", "07:30", "08:40", [1, 2, 3, 4, 5]),
        item(6, "周末专线", "玉泉校区", "紫金港校区", "09:00", "09:40", [6, 7]),
    ]}


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def _safe(fn):
    try:
        fn()
    except Exception as e:
        log("background task failed: %s" % e)


def _hhmm(v):
    """650 -> '06:50'；异常时返回空串"""
    try:
        s = str(int(v))
        return s.zfill(4)[:2] + ":" + s.zfill(4)[2:]
    except (TypeError, ValueError):
        return ""


# ----------------------------------------------------------------------------
# DPAPI（Windows 凭据保护，用于"记住密码"）
# ----------------------------------------------------------------------------
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_protect(data: bytes) -> str:
    bin_ = ctypes.create_string_buffer(data, len(data))
    bin_blob = DATA_BLOB(len(data), ctypes.cast(bin_, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(bin_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise OSError("CryptProtectData failed")
    out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return base64.b64encode(out).decode()


def dpapi_unprotect(s: str) -> bytes:
    raw = base64.b64decode(s)
    bin_ = ctypes.create_string_buffer(raw, len(raw))
    bin_blob = DATA_BLOB(len(raw), ctypes.cast(bin_, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bin_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise OSError("CryptUnprotectData failed")
    out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return out


# ----------------------------------------------------------------------------
# HTTP 客户端（Cookie 管理 + 重定向控制）
# ----------------------------------------------------------------------------
class NoRedirect(urlreq.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Response:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    @property
    def text(self):
        for enc in ("utf-8", "gbk"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", "replace")


def rsa_no_padding(text: str, modulus_hex: str, exponent_hex: str) -> str:
    """浙大 CAS 的密码加密：RSA 无填充，结果十六进制左补零到 128 位"""
    m = int(modulus_hex, 16)
    e = int(exponent_hex, 16)
    k = int.from_bytes(text.encode("utf-8"), "big")
    return format(pow(k, e, m), "x").zfill(128)


# ----------------------------------------------------------------------------
# AES-CFB（WebVPN 登录用）：优先 pycryptodome，无则纯 Python 兜底
# ----------------------------------------------------------------------------
def _aes_cfb_encrypt(key16: bytes, iv16: bytes, data: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
        return AES.new(key16, AES.MODE_CFB, iv=iv16, segment_size=128).encrypt(data)
    except ImportError:
        pass
    # 纯 Python AES-128 加密 + CFB-128（仅用于登录加密，性能足够）
    SBOX = bytes.fromhex(
        "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
        "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
        "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
        "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
        "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
        "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
        "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
        "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")
    RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]

    def xtime(a):
        a <<= 1
        return (a ^ 0x1b) & 0xff if a & 0x100 else a

    def expand(key):
        w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
        for i in range(4, 44):
            t = list(w[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [SBOX[b] for b in t]
                t[0] ^= RCON[i // 4 - 1]
            w.append([a ^ b for a, b in zip(w[i - 4], t)])
        return [sum((w[r * 4 + c] for r in range(4)), []) for c in range(11)]

    def enc_block(state, rk):
        s = [state[i] ^ rk[0][i] for i in range(16)]
        for rnd in range(1, 10):
            s = [SBOX[b] for b in s]
            s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
                 s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
            t = list(s)
            for c in range(4):
                a, b, cc, d = (t[c * 4 + i] for i in range(4))
                x = a ^ b ^ cc ^ d
                s[c * 4 + 0] = a ^ x ^ xtime(a ^ b)
                s[c * 4 + 1] = b ^ x ^ xtime(b ^ cc)
                s[c * 4 + 2] = cc ^ x ^ xtime(cc ^ d)
                s[c * 4 + 3] = d ^ x ^ xtime(d ^ a)
            s = [s[i] ^ rk[rnd][i] for i in range(16)]
        s = [SBOX[b] for b in s]
        s = [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
             s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]
        return bytes(s[i] ^ rk[10][i] for i in range(16))

    rk = expand(key16)
    out, prev = b"", iv16
    for i in range(0, len(data), 16):
        ks = enc_block(prev, rk)
        blk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(blk, ks))
        prev = out[i:i + 16]
    return out


# 深澜 wengine 登录页 JS 内置的固定 AES 常量（公开于所有高校 wengine 部署的前端源码，
# 亦见 Celechron 等开源项目的相同实现），非用户数据、非机密
_WENGINE_KEY = "wr" + "dvpn" + "isawesome" + "!"


def webvpn_encrypt_password(password: str) -> str:
    """WebVPN(深澜 wengine) 登录加密：AES-CFB（key=iv 为登录页内置常量），
    返回 hex(iv)+hex(密文)[:len*2]（与登录页 JS 完全一致）"""
    return wengine_encrypt(password, _WENGINE_KEY, _WENGINE_KEY)


def wengine_encrypt(text: str, key: str, iv: str) -> str:
    """深澜 wengine 通用加密（密码与代理 URL 的 host 段同一算法）：
    明文补零到 16 倍数 → AES-CFB → hex(iv)+hex(密文)[:2*len(原文)]"""
    key, iv = str(key), str(iv)
    plain_len = len(text)
    if plain_len % 16:
        text += "0" * (16 - plain_len % 16)
    enc = _aes_cfb_encrypt(key.encode("utf-8"), iv.encode("utf-8"), text.encode("utf-8"))
    return iv.encode("utf-8").hex() + enc.hex()[:plain_len * 2]


class Bridge:
    def __init__(self, mock=False):
        self.mock = mock
        self.lock = threading.RLock()
        self.jar = cookiejar.CookieJar()
        self.opener = urlreq.build_opener(urlreq.HTTPCookieProcessor(self.jar))
        self.opener_nored = urlreq.build_opener(NoRedirect(),
                                                urlreq.HTTPCookieProcessor(self.jar))
        # WebVPN 独立会话（账号可与统一身份认证不同）
        self.webvpn_jar = cookiejar.CookieJar()
        self.webvpn_opener = urlreq.build_opener(
            urlreq.HTTPCookieProcessor(self.webvpn_jar))
        self.webvpn_opener_nored = urlreq.build_opener(
            NoRedirect(), urlreq.HTTPCookieProcessor(self.webvpn_jar))
        self.webvpn_user = ""
        self.username = ""
        self.yjsy_token = ""
        self.cache = {}          # key -> (expire_ts, payload)
        self.memory = {}         # 非持久化缓存（作业等）
        self._load_cache_file()
        self._load_session()

    # ---------------- 基础 HTTP ----------------
    def req(self, url, data=None, headers=None, follow=True, timeout=TIMEOUT,
            method=None, json_body=None, webvpn=False):
        h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        if headers:
            h.update(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            h.setdefault("Content-Type", "application/json;charset=UTF-8")
        elif isinstance(data, dict):
            data = up.urlencode(data).encode("utf-8")
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        r = urlreq.Request(url, data=data, headers=h, method=method)
        if webvpn:
            op = self.webvpn_opener if follow else self.webvpn_opener_nored
        else:
            op = self.opener if follow else self.opener_nored
        try:
            resp = op.open(r, timeout=timeout)
            return Response(resp.status, resp.headers, resp.read(), resp.geturl())
        except urlreq.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b""
            return Response(e.code, e.headers, body, e.geturl() or url)

    # ---------------- 配置 / 会话持久化 ----------------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _load_cache_file(self):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                self.disk = json.load(f)
        except Exception:
            self.disk = {}

    def _save_cache_file(self):
        try:
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.disk, f, ensure_ascii=False)
        except Exception as e:
            log("cache save failed: %s" % e)

    def save_credentials(self, username, password=None):
        cfg = self._load_config()
        cfg["username"] = username
        if password is not None:
            try:
                cfg["enc_password"] = dpapi_protect(password.encode("utf-8"))
            except Exception:
                cfg["enc_password"] = ""
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def load_stored_password(self):
        cfg = self._load_config()
        enc = cfg.get("enc_password")
        if not enc:
            return ""
        try:
            return dpapi_unprotect(enc).decode("utf-8")
        except Exception:
            return ""

    def save_session(self):
        cookies = []
        for c in self.jar:
            cookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                            "path": c.path, "secure": bool(c.secure),
                            "expires": c.expires})
        wcookies = []
        for c in self.webvpn_jar:
            wcookies.append({"name": c.name, "value": c.value, "domain": c.domain,
                             "path": c.path, "secure": bool(c.secure),
                             "expires": c.expires})
        with open(SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump({"username": self.username, "yjsy_token": self.yjsy_token,
                       "cookies": cookies, "webvpn_user": self.webvpn_user,
                       "webvpn_key": getattr(self, "webvpn_key", ""),
                       "webvpn_iv": getattr(self, "webvpn_iv", ""),
                       "webvpn_cookies": wcookies}, f, ensure_ascii=False)

    def has_webvpn(self):
        return any("wengine_vpn_ticket" in c.name and c.value for c in self.webvpn_jar)

    def _load_session(self):
        try:
            with open(SESSION_PATH, encoding="utf-8") as f:
                s = json.load(f)
            self.username = s.get("username", "")
            self.yjsy_token = s.get("yjsy_token", "")
            self.webvpn_user = s.get("webvpn_user", "")
            self.webvpn_key = s.get("webvpn_key", "")
            self.webvpn_iv = s.get("webvpn_iv", "")
            for c in s.get("cookies", []):
                try:
                    self.jar.set_cookie(cookiejar.Cookie(
                        0, c["name"], c["value"], None, False,
                        c["domain"], c["domain"].startswith("."), c["domain"].startswith("."),
                        c["path"], c["path"].startswith("/"), bool(c.get("secure")),
                        c.get("expires"), c.get("expires") is None, None, None, {}))
                except Exception:
                    continue
            for c in s.get("webvpn_cookies", []):
                try:
                    self.webvpn_jar.set_cookie(cookiejar.Cookie(
                        0, c["name"], c["value"], None, False,
                        c["domain"], c["domain"].startswith("."), c["domain"].startswith("."),
                        c["path"], c["path"].startswith("/"), bool(c.get("secure")),
                        c.get("expires"), c.get("expires") is None, None, None, {}))
                except Exception:
                    continue
            log("session loaded (user=%s, webvpn=%s)"
                % (self.username or "unknown", self.webvpn_user or "no"))
        except Exception:
            log("no saved session")

    def has_sso(self):
        return any(c.name == "iPlanetDirectoryPro" and c.value for c in self.jar)

    # ---------------- CAS 登录 ----------------
    def cas_login(self, username, password, captcha=""):
        """返回 (ok, message, needs_captcha)"""
        with self.lock:
            for attempt in (1, 2):
                r = self.req(CAS_LOGIN)
                html = r.text
                if "统一身份认证平台" in html:
                    break
                self.jar.clear()
            else:
                return False, "无法打开统一身份认证登录页，请检查网络", False
            m = re.search(r'name="execution"\s+value="(.*?)"', html)
            if not m:
                return False, "登录页解析失败（页面结构可能变化）", False
            execution = m.group(1)
            pub = self.req(CAS_PUBKEY)
            try:
                pk = json.loads(pub.body.decode("utf-8"))
            except Exception:
                return False, "获取登录公钥失败", False
            enc = rsa_no_padding(password, pk["modulus"], pk["exponent"])
            r = self.req(CAS_LOGIN, data={
                "username": username, "password": enc, "execution": execution,
                "_eventId": "submit", "authcode": captcha, "rememberMe": "true"})
            if not self.has_sso():
                text = r.text
                if "验证码" in text or "captcha" in text.lower():
                    return False, "需要验证码，请在输入框中填写图片验证码后重试", True
                if "凭据" in text or "密码" in text or "credential" in text.lower():
                    return False, "学号或密码错误", False
                if "冻结" in text or "锁定" in text:
                    return False, "账号被锁定/冻结，请到统一身份认证平台处理", False
                return False, "登录失败：账号或密码错误，或网络异常", False
            self.username = username
            log("CAS login ok (user=%s)" % username)
            # 建立各子系统会话
            warm_errors = []
            if not self.warm_courses():
                warm_errors.append("学在浙大会话建立失败")
            if not self.refresh_yjsy_token():
                warm_errors.append("研究生系统会话建立失败")
            self.save_session()
            if warm_errors:
                return True, "登录成功，但部分子系统会话建立失败：%s" % "；".join(warm_errors), False
            return True, "登录成功", False

    # ---------------- 子系统会话 ----------------
    def warm_courses(self):
        r = self.req(COURSES + "/user/courses")
        ok = r.status == 200 and "courses" in r.text
        log("warm courses: %s (status %s)" % (ok, r.status))
        return ok

    def refresh_yjsy_token(self):
        r = self.req(CAS_LOGIN + "?service=" + up.quote(YJSY_SERVICE, safe=""),
                     follow=False)
        loc = r.headers.get("Location", "") or ""
        q = up.parse_qs(up.urlparse(loc).query)
        ticket = (q.get("ticket") or [None])[0]
        if not ticket:
            log("yjsy ticket missing (status %s)" % r.status)
            return False
        r2 = self.req(YJSY + "/dataapi/sys/cas/client/validateLogin?ticket=%s&service=%s"
                      % (up.quote(ticket), up.quote(YJSY_SERVICE, safe="")))
        try:
            j = json.loads(r2.body.decode("utf-8"))
        except Exception:
            log("yjsy validateLogin parse failed")
            return False
        token = (j.get("result") or {}).get("token", "")
        if j.get("success") and token:
            self.yjsy_token = token
            log("yjsy token ok")
            return True
        log("yjsy validateLogin failed: %s" % j.get("message", ""))
        return False

    def ensure_logged_in(self):
        """已有 SSO cookie 时，校验并按需重建子系统会话；返回是否可用（30s 节流防刷屏）"""
        if self.mock:
            return True
        with self.lock:
            if not self.has_sso():
                return False
            now = time.time()
            if now < getattr(self, "_next_ensure_check", 0):
                return getattr(self, "_last_ensure_result", False)
            self._next_ensure_check = now + 30
            if self._courses_alive():
                if not self.yjsy_token:
                    self.refresh_yjsy_token()
                self._last_ensure_result = True
                return True
            # SSO 可能还在，子系统会话过期 → 重建
            if self.warm_courses() and self.refresh_yjsy_token():
                self.save_session()
                self._last_ensure_result = True
                return True
            self._last_ensure_result = False
            return False

    def try_auto_login(self):
        """配置里存了加密密码时，尝试静默重登"""
        pwd = self.load_stored_password()
        if pwd and self._load_config().get("username"):
            ok, msg, _ = self.cas_login(self._load_config()["username"], pwd)
            return ok
        return False

    def _courses_alive(self):
        r = self.req(COURSES + "/api/todos?no-intercept=true", timeout=10)
        return r.status == 200 and r.body[:1] == b"{"

    # ---------------- 学在浙大数据 ----------------
    def fetch_courses(self):
        courses, seen = [], set()
        for status in ("ongoing", "notStarted"):
            body = {"fields": COURSES_FIELDS, "page": 1, "page_size": 100,
                    "conditions": {"status": status, "keyword": "",
                                   "classify_type": "recently_started"}}
            r = self.req(COURSES + "/api/my-courses", json_body=body)
            if r.status != 200:
                raise RuntimeError("my-courses HTTP %s" % r.status)
            j = json.loads(r.body.decode("utf-8"))
            for c in j.get("courses") or []:
                if c.get("id") in seen:
                    continue
                seen.add(c.get("id"))
                courses.append({
                    "id": str(c.get("id")), "name": c.get("name", ""),
                    "code": c.get("course_code", ""),
                    "teacher": c.get("teacher") or "",
                })
        return courses

    def fetch_homework(self, courses):
        out = []
        for c in courses:
            r = self.req("%s/api/courses/%s/homework-activities?page=1&page_size=1000"
                         % (COURSES, c["id"]))
            if r.status != 200:
                continue
            try:
                j = json.loads(r.body.decode("utf-8"))
            except Exception:
                continue
            for hw in j.get("homework_activities") or []:
                ddl = hw.get("deadline", "")
                out.append({
                    "id": str(hw.get("id")), "course": c["name"],
                    "course_id": str(c.get("id", "")),
                    "title": hw.get("title", ""),
                    "deadline": ddl,
                    "submitted": bool(hw.get("submitted")),
                    "closed": bool(hw.get("is_closed")),
                    "description": re.sub(r"<[^>]+>", " ", (hw.get("data") or {}).get("description", ""))[:200],
                    # 学在浙大作业活动页（在浏览器中打开即可查看/提交，首次需统一身份认证登录）
                    "url": "%s/course/%s/learning-activity#/%s"
                           % (COURSES, c.get("id", ""), hw.get("id", "")),
                })
        return out

    def fetch_todos(self):
        r = self.req(COURSES + "/api/todos?no-intercept=true")
        if r.status != 200:
            return []
        try:
            j = json.loads(r.body.decode("utf-8"))
        except Exception:
            return []
        return j.get("todo_list") or []

    # ---------------- 研究生系统数据 ----------------
    def yjsy_get(self, path_and_query):
        if not self.yjsy_token:
            if not self.refresh_yjsy_token():
                raise RuntimeError("研究生系统会话无效，请重新登录")
        r = self.req(YJSY + path_and_query,
                     headers={"X-Access-Token": self.yjsy_token})
        try:
            j = json.loads(r.body.decode("utf-8"))
        except Exception:
            raise RuntimeError("研究生系统返回异常（HTTP %s）" % r.status)
        if j.get("success") is False or (j.get("code") in (401, 500) and not j.get("data")):
            # token 失效 → 重取一次
            if self.refresh_yjsy_token():
                r = self.req(YJSY + path_and_query,
                             headers={"X-Access-Token": self.yjsy_token})
                j = json.loads(r.body.decode("utf-8"))
            else:
                raise RuntimeError("研究生系统 token 失效，请重新登录")
        return j

    @staticmethod
    def current_xn_xq(now=None):
        now = now or datetime.now()
        y, m = now.year, now.month
        if m >= 7:
            xn = "%d-%d" % (y, y + 1)
        else:
            xn = "%d-%d" % (y - 1, y)
        xq = 15 if (m >= 9 or m == 1) else 16   # 15=秋冬, 16=春夏
        return xn, xq

    # 研究生系统学季代码（读自 yjsy 前端源码）：11=春 12=夏 13=秋 14=冬；15/16 为合并学期
    # xn 用学年起始年份（如 2026），课表需要把秋13+冬14 两个学季合并
    XQ_CANDIDATES_AUTUMN = [13, 14, 15, 11, 12, 16]
    XQ_CANDIDATES_SPRING = [11, 12, 16, 13, 14, 15]

    @staticmethod
    def current_xn_xq(now=None):
        # 返回 (学年起始年份字符串, 主学季代码)；6-12 月属 y 年秋冬，1-5 月属 (y-1) 年春夏
        now = now or datetime.now()
        y = now.year
        if now.month >= 6:
            return str(y), 13
        return str(y - 1), 11

    @staticmethod
    def _dedupe_sessions(sessions):
        seen, out = set(), []
        for s in sessions:
            k = (s["weekday"], s["period"], s["course"], s["location"], s["teacher"], s["weeks_raw"])
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out

    def fetch_timetable(self, xn=None, xq=None):
        """查询研究生课表：学年起始年份 + 学季代码，自动尝试并合并秋/冬两个学季"""
        xn0, xq0 = self.current_xn_xq()
        xn = str(xn or xn0)
        cands = (self.XQ_CANDIDATES_AUTUMN if xq0 == 13 else self.XQ_CANDIDATES_SPRING) \
            if xq is None else [xq]
        tried, collected, last_err = [], {}, ""
        for code in cands:
            try:
                j = self.yjsy_get("/dataapi/py/pyKcbj/queryXskbByLoginUser?xn=%s&pkxq=%s"
                                  % (up.quote(xn), up.quote(str(code))))
            except Exception as e:
                last_err = str(e)[:250]
                tried.append({"pkxq": str(code), "error": last_err})
                continue
            sessions = parse_kcb_response(j)
            tried.append({"pkxq": str(code), "n": len(sessions)})
            if sessions:
                collected[str(code)] = sessions
        # 合并两个半学季（秋13+冬14 或 春11+夏12），其次合并学期(15/16)
        first = collected.get("13") or collected.get("11") or []
        second = collected.get("14") or collected.get("12") or []
        best = self._dedupe_sessions(first + second)
        if not best:
            best = self._dedupe_sessions([s for v in collected.values() for s in v])
        xq_name = "秋冬学期" if xq0 == 13 else "春夏学期"
        return {"xn": xn, "xq": xq0, "xq_name": xq_name, "sessions": best,
                "tried": tried[:10],
                "note": "" if best else ("各学季均无课表数据" + ("；最后错误：" + last_err if last_err else ""))}

    def fetch_exams(self, xn=None, xq=None):
        xn0, xq0 = self.current_xn_xq()
        xn = str(xn or xn0)
        cands = (self.XQ_CANDIDATES_AUTUMN if xq0 == 13 else self.XQ_CANDIDATES_SPRING) \
            if xq is None else [xq]
        last_err, empty_seen, records = "", False, []
        for code in cands:
            qs = ("dm=py_grks&mode=2&role=1&column=createTime&order=desc&queryMode=1"
                  "&field=id,,kcbh,kcmc,rq,ksTime,xn,xq_dictText,ksdd,zwh"
                  "&pageNo=1&pageSize=100&xn=%s&xq=%s" % (up.quote(xn), up.quote(str(code))))
            try:
                j = self.yjsy_get("/dataapi/py/pyKsxsxx/queryPageByXs?" + qs)
            except Exception as e:
                last_err = str(e)[:250]
                continue
            res = j.get("result")
            rec = res.get("records") if isinstance(res, dict) else res
            rec = rec or []
            if rec:
                records.extend(rec)
            elif not empty_seen:
                empty_seen = True
        if last_err and not records:
            raise RuntimeError("考试查询失败：" + last_err)
        exams, seen = [], set()
        for e in records:
            if not isinstance(e, dict):
                continue
            rq = re.sub(r"\D", "", str(e.get("rq", "")))[:8]
            date = "%s-%s-%s" % (rq[:4], rq[4:6], rq[6:8]) if len(rq) == 8 else str(e.get("rq", ""))
            key = (e.get("kcmc"), date, str(e.get("zwh")))
            if key in seen:
                continue
            seen.add(key)
            times = re.findall(r"\d{1,2}\s*:\s*\d{2}", str(e.get("ksTime", "")))
            exams.append({
                "course": e.get("kcmc", ""), "date": date,
                "time": "~".join(times) if times else str(e.get("ksTime", "")),
                "location": e.get("ksdd") or e.get("mc") or "",
                "seat": str(e.get("zwh") or ""),
            })
        exams.sort(key=lambda x: x["date"])
        return exams

    # ---------------- WebVPN（独立账号） ----------------
    def webvpn_login(self, username, password, captcha=""):
        """WebVPN 登录：AES-CFB 加密密码后 POST /do-login。
        返回 (ok, message, needs_captcha)"""
        self._webvpn_manual_until = time.time() + 90
        with self.lock:
            r = self.req(WEBVPN + "/login", timeout=15, webvpn=True)
            html = r.text
            csrf_m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
            cap_m = re.search(r'name="captcha_id"\s+value="([^"]+)"', html)
            need_cap = 'name="needCaptcha" value="true"' in html
            # 验证码 id 与用户看到的图片配对：优先用取图时缓存的 id
            cap_id = (cap_m.group(1) if cap_m else "") or getattr(self, "webvpn_captcha_id", "")
            form = {
                "username": username,
                "password": webvpn_encrypt_password(password),
                "auth_type": "local",
                "needCaptcha": "true" if need_cap else "false",
                "captcha_id": cap_id,
                "captcha": captcha,
            }
            if csrf_m:
                form["_csrf"] = csrf_m.group(1)
            r = self.req(WEBVPN + "/do-login", data=form, timeout=20, webvpn=True)
            try:
                j = json.loads(r.body.decode("utf-8"))
            except Exception:
                return False, "WebVPN 返回异常（HTTP %s）" % r.status, False
            if j.get("success"):
                self.webvpn_user = username
                self._fetch_wengine_keys()
                self.save_session()
                log("webvpn login ok (user=%s, keys=%s)"
                    % (username, bool(self.webvpn_key)))
                return True, "WebVPN 登录成功", False
            err = str(j.get("error") or j.get("message") or "")
            if "captcha" in err.lower() or "验证" in err:
                if captcha:
                    return False, "验证码不对或已过期：点击验证码图片刷新后再试", True
                return False, "WebVPN 需要验证码（已加载，请输入后重试）", True
            if err == "INVALID_ACCOUNT" or "密码" in err or "password" in err.lower() or "用户" in err:
                return False, ("账号或密码不被接受（INVALID_ACCOUNT）。注意：WebVPN 用的是「上网账号」"
                               "——它可能不是学号，而是 info.zju.edu.cn 个人中心里的网上账号码、"
                               "邮箱别名或手机号；密码是上网密码（与统一身份认证可能不同）。"
                               "可先在浏览器登录 webvpn.zju.edu.cn 确认可用的账号形式。"), False
            return False, "WebVPN 登录失败：" + (err or "未知错误"), False

    def _fetch_wengine_keys(self):
        """登录后从门户页提取代理 URL 的加密密钥（wrdvpnKey/wrdvpnIV）"""
        self.webvpn_key, self.webvpn_iv = "", ""
        for path in ("/", "/user", "/portal"):
            try:
                r = self.req(WEBVPN + path, timeout=12, webvpn=True)
                if r.status != 200:
                    continue
                km = re.search(r'wrdvpnKey["\']?\s*[:=]\s*["\']([^"\']{8,64})["\']', r.text)
                im = re.search(r'wrdvpnIV["\']?\s*[:=]\s*["\']([^"\']{8,64})["\']', r.text)
                if km and im:
                    self.webvpn_key, self.webvpn_iv = km.group(1), im.group(1)
                    log("wengine keys captured (key len %d)" % len(self.webvpn_key))
                    return
            except Exception:
                continue
        log("wengine keys NOT found in portal pages")

    def _wengine_host_hex(self, host):
        key = self.webvpn_key or "wrdvpnisawesome!"
        iv = self.webvpn_iv or "wrdvpnisawesome!"
        return wengine_encrypt(host, key, iv)

    def try_webvpn_auto_login(self):
        """配置里存了 WebVPN 加密密码时，静默重登（用户手动登录后 90 秒内不抢跑）"""
        if getattr(self, "_webvpn_trying", False):
            return False
        if time.time() < getattr(self, "_webvpn_manual_until", 0):
            return self.has_webvpn()
        cfg = self._load_config()
        enc, user = cfg.get("webvpn_enc_password"), cfg.get("webvpn_user")
        if not enc or not user:
            return False
        self._webvpn_trying = True
        try:
            try:
                pwd = dpapi_unprotect(enc).decode("utf-8")
            except Exception:
                return False
            ok, msg, _ = self.webvpn_login(user, pwd)
            return ok
        finally:
            self._webvpn_trying = False

    def webvpn_captcha(self):
        """取一张新验证码：先访问登录页拿到 captcha_id（缓存住供登录配对），再取图片"""
        try:
            r = self.req(WEBVPN + "/login", timeout=12, webvpn=True)
            m = re.search(r'name="captcha_id"\s+value="([^"]+)"', r.text)
            cid = m.group(1) if m else ""
            if not cid:
                return None, None
            self.webvpn_captcha_id = cid
            ri = self.req(WEBVPN + "/captcha/" + cid + ".png", timeout=12, webvpn=True)
            if ri.status == 200 and ri.body[:4] == b"\x89PNG":
                return ri.body, "image/png"
            return None, None
        except Exception:
            return None, None

    def webvpn_alive(self):
        """WebVPN 会话是否有效（访问门户不应跳到登录页）"""
        try:
            r = self.req(WEBVPN + "/", follow=False, timeout=10, webvpn=True)
            loc = (r.headers.get("Location") or "")
            if r.status in (301, 302):
                return "/login" not in loc
            return r.status == 200 and "wengine" in r.text.lower()
        except Exception:
            return False

    def webvpn_get(self, url, timeout=15):
        """通过 WebVPN 代理抓取校内页面。深澜 wengine 格式：
        https://webvpn.zju.edu.cn/<scheme>/<hex(iv)+AES-CFB(host)>/<原始路径>?<query>"""
        if not getattr(self, "webvpn_key", ""):
            self._fetch_wengine_keys()
        p = up.urlparse(url)
        scheme = p.scheme or "https"
        netloc = p.netloc
        m = re.match(r"^(.*?)(:(\d+))?$", netloc)
        host, port = m.group(1), m.group(3)
        if scheme == "https" and port == "443":
            port = None
        if scheme == "http" and port == "80":
            port = None
        token = scheme + (("-" + port) if port else "")
        proxied = "%s/%s/%s%s" % (WEBVPN, token, self._wengine_host_hex(host), p.path or "/")
        if p.query:
            proxied += "?" + p.query
        return self.req(proxied, timeout=timeout, webvpn=True)

    def webvpn_captcha_url(self):
        return WEBVPN + "/captcha/" + str(int(time.time() * 1000)) + ".png"

    # ---------------- 学院/总务处通知 ----------------
    def fetch_notices(self):
        items, errors = [], []
        via_webvpn = False
        src_status = []          # 每个来源的抓取状态（供界面展示）
        direct_ok_saa = False

        def count_for(tag):
            return sum(1 for it in items if it.get("source") == tag)

        for base, tag in NOTICE_SOURCES:
            ok_flag, err = False, ""
            try:
                r = None
                for _attempt in range(3):          # 网络抖动重试（RemoteDisconnected 等）
                    try:
                        r = self.req(base, timeout=10)
                        break
                    except Exception:
                        if _attempt == 2:
                            raise
                        time.sleep(1.5)
                if r is not None and r.status == 200:
                    if "saa" in base:
                        direct_ok_saa = True
                    got = extract_notices(r.text, base)
                    log("notices from %s: %d items" % (base, len(got)))
                    items.extend({"source": tag, **it} for it in got)
                    for lp in discover_list_pages(r.text, base)[:3]:
                        try:
                            r2 = self.req(lp, timeout=8)
                            if r2.status == 200:
                                items.extend({"source": tag, **it}
                                             for it in extract_notices(r2.text, lp))
                        except Exception:
                            continue
                    ok_flag = bool(got)
                    if not got:
                        err = "页面解析为 0 条（结构可能变化）"
                else:
                    err = "HTTP %s" % r.status
            except Exception as e:
                err = e.__class__.__name__
            src_status.append({"tag": tag, "ok": ok_flag, "count": count_for(tag),
                               "error": err, "via": "direct"})
        # saa 直连失败（非校园网）→ WebVPN 代理兜底
        if not direct_ok_saa:
            if not self.has_webvpn():
                errors.append("saa 直连失败且 WebVPN 未登录 —— 在设置页登录 WebVPN 后即可抓取学院通知")
                src_status.append({"tag": "saa(经WebVPN)", "ok": False, "count": 0,
                                   "error": "未登录", "via": "webvpn"})
            else:
                via_webvpn = True
                got_any = False
                for base in SAA_SITES:
                    ok_flag, err = False, ""
                    try:
                        r = self.webvpn_get(base, timeout=20)
                        if r.status != 200:
                            err = "HTTP %s" % r.status
                        else:
                            got = extract_notices(r.text, base)
                            if not got:
                                err = "会话可能失效（页面未解析到通知），请退出 WebVPN 重登"
                            else:
                                got_any = True
                                ok_flag = True
                                log("notices via webvpn %s: %d items" % (base, len(got)))
                                items.extend({"source": "saa(经WebVPN)", **it} for it in got)
                                for lp in discover_list_pages(r.text, base)[:3]:
                                    try:
                                        r2 = self.webvpn_get(lp, timeout=20)
                                        if r2.status == 200:
                                            items.extend({"source": "saa(经WebVPN)", **it}
                                                         for it in extract_notices(r2.text, base))
                                    except Exception:
                                        continue
                    except Exception as e:
                        err = e.__class__.__name__
                    src_status.append({"tag": "saa(经WebVPN)", "ok": ok_flag,
                                       "count": count_for("saa(经WebVPN)"),
                                       "error": err, "via": "webvpn"})
                if not got_any:
                    via_webvpn = False
        uniq, seen = [], set()
        for it in items:
            # 同一篇文章可能被多个源抓到（saa 公网/内网镜像 URL 域名不同），按 标题+日期 去重
            key = (it.get("title", "")[:50] or it.get("url", "")) + "@" + it.get("date", "")
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        uniq.sort(key=lambda x: x.get("date", ""), reverse=True)
        result = {"ok": bool(uniq), "items": uniq[:120], "errors": errors,
                  "sources": src_status,
                  "via_webvpn": via_webvpn,
                  "webvpn_logged": bool(self.has_webvpn()),
                  "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if not uniq and not errors:
            result["ok"] = False
            result["errors"] = ["未解析到通知（网站结构可能变化）"]
        return result

    # ---------------- 校历 ----------------
    def fetch_calendar(self):
        xn, _ = self.current_xn_xq()
        out, ok_terms = {}, {}
        for i in (1, 2):
            key = "%s-%d" % (xn, i)
            try:
                r = self.req("%s/%s.json" % (CALENDAR_BASE, key), timeout=8)
                if r.status == 200 and r.body[:1] == b"{":
                    out[key] = json.loads(r.body.decode("utf-8"))
                    ok_terms[key] = True
            except Exception:
                pass
        if "1" not in out and "2" not in out:
            out = {xn + "-1": EMBEDDED_CALENDAR_1}
        return {"year": xn, "configs": out,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}

    # ---------------- 校车班次（后勤公开接口） ----------------
    BUS_API = ("http://www.life.zju.edu.cn/_web/_apps/lightapp/shuttlebus/"
               "busflight/api/lists.rst?id=-1&startStationId=-1&endStationId=-1"
               "&zj=1,2,3,4,5,6,7&startTime=0000&endTime=2359&page=0&rows=99999")
    BUS_DETAIL_API = ("http://www.life.zju.edu.cn/_web/_apps/lightapp/shuttlebus/"
                      "busflight/api/{0}.rst")
    BUS_TTL = 12 * 3600      # 班次含经停站，抓取较慢，缓存 12 小时（按学期调整）

    def fetch_bus(self, reuse=True):
        """浙大后勤班车：列表每次实时抓；经停站详情可复用上次缓存（很少变化），
        仅为新出现的班次补抓。返回 items: [{...,stations:[{name,time}]}]"""
        r = self.req(self.BUS_API, timeout=20)
        if r.status != 200:
            raise RuntimeError("校车接口 HTTP %s" % r.status)
        # 上次的经停站缓存（按班次 id）
        prev_stops = {}
        if reuse:
            disk = self.disk.get("bus", {}).get("payload")
            if isinstance(disk, dict):
                for it in disk.get("items", []):
                    if it.get("id") and it.get("stations"):
                        prev_stops[it["id"]] = it["stations"]
        j = json.loads(r.body.decode("utf-8"))
        data = (j.get("result") or {}).get("data") or []
        items, detail_ids = [], {}
        for b in data:
            if not isinstance(b, dict):
                continue
            sid = str(b.get("id", ""))
            it = {
                "id": sid,
                "bus": b.get("busName", ""),
                "line": b.get("lineName", ""),
                "from": b.get("startStationName", ""),
                "to": b.get("endStationName", ""),
                "depart": _hhmm(b.get("startTime")),
                "arrive": _hhmm(b.get("endTime")),
                "cycle": [int(x) for x in re.findall(r"\d", str(b.get("cycle", "")))],
                "remark": b.get("remark", ""),
                "stations": prev_stops.get(sid, []),
            }
            items.append(it)
            if not it["stations"]:
                detail_ids[sid] = it

        # 只为缺经停站的班次抓详情
        if detail_ids:
            from concurrent.futures import ThreadPoolExecutor
            lock = threading.Lock()

            def fetch_detail(sid):
                try:
                    rd = self.req(self.BUS_DETAIL_API.format(sid), timeout=15)
                    if rd.status != 200:
                        return
                    jd = json.loads(rd.body.decode("utf-8"))
                    arr = (jd.get("result") or {}).get("data") or []
                    st = (arr[0].get("stations") if arr and isinstance(arr[0], dict) else None) or []
                    stations = [{"name": s.get("name", ""), "time": _hhmm(s.get("time"))}
                                for s in st if isinstance(s, dict)]
                    with lock:
                        if sid in detail_ids:
                            detail_ids[sid]["stations"] = stations
                except Exception:
                    pass    # 单个详情失败不影响整体

            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(fetch_detail, list(detail_ids.keys())))

        items.sort(key=lambda x: (x["depart"] or "99:99"))
        n_stops = sum(1 for it in items if it["stations"])
        return {"ok": bool(items), "items": items, "total": len(items),
                "with_stops": n_stops,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}

    # ---------------- 校园巴士实时（bccx 系统） ----------------
    CB_TTL = 1800      # 站点列表缓存 30 分钟；到站数据实时不缓存

    def _cb_post(self, path, fields):
        """bccx 接口均为表单 POST（小程序同源，公开）"""
        data = up.urlencode(fields).encode()
        r = self.req(CB_BASE + path, data=data, timeout=15,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status != 200:
            raise RuntimeError("校园巴士接口 HTTP %s" % r.status)
        return json.loads(r.body.decode("utf-8"))

    def fetch_cb_stations(self):
        j = self._cb_post("tool/stationList", {})
        stations = [s.get("station_alias", "") for s in (j.get("data") or [])
                    if isinstance(s, dict) and s.get("station_alias")]
        return {"ok": bool(stations), "stations": stations}

    def fetch_cb_stop(self, name):
        """某站点实时到站：每条含线路名/开车时间/实时车辆(车牌/车速/状态/下一站)"""
        j = self._cb_post("manage/getBcByStationName", {"bid": "", "stationName": name})
        data = j.get("data") or []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            buses = item.get("runBusInfo") or []
            out.append({
                "line": item.get("line_name") or item.get("linename") or "",
                "start_time": item.get("start_time") or item.get("driveTime") or "",
                "buses": buses,
                "raw_keys": [k for k in item.keys()][:12],
            })
        return {"ok": True, "station": name, "lines": out, "has_data": bool(out),
                "fetched_at": datetime.now().strftime("%H:%M:%S")}

    # ---------------- 缓存包装 ----------------
    def cached(self, key, ttl, fn, fresh=False, persist=False):
        now = time.time()
        hit = self.cache.get(key)
        if hit and not fresh and hit[0] > now:
            return hit[1], True
        payload = fn()
        self.cache[key] = (now + ttl, payload)
        if persist:
            self.disk[key] = {"ts": now, "payload": payload}
            self._save_cache_file()
        return payload, False

    # ---------------- 汇总 ----------------
    def api_all(self, fresh=False):
        result = {"ok": True, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "mock": self.mock, "logged_in": self.mock or self.ensure_logged_in(),
                  "user": self.username if not self.mock else "演示用户"}
        # SSO 失效但存了加密密码时，静默重登
        if not self.mock and not result["logged_in"] and self._load_config().get("enc_password"):
            if self.try_auto_login():
                log("auto re-login ok")
                result["logged_in"] = True
        errors = {}

        cal, _ = self.cached("calendar", CALENDAR_TTL, self.fetch_calendar, fresh, persist=True)
        result["calendar"] = cal

        if self.mock:
            result.update(MOCK_ALL())
            hw = result.get("homework") or []
            for h in hw:
                h["deadline_ts"] = parse_ts(h.get("deadline"))
            hw.sort(key=lambda x: x.get("deadline_ts") or 9e15)
            return result

        if not result["logged_in"]:
            result["errors"] = {"login": "未登录"}
            result["courses"], result["homework"], result["todos"] = [], [], []
            result["timetable"], result["exams"] = None, []
            return result

        try:
            courses, _ = self.cached("courses", DATA_TTL, self.fetch_courses, fresh)
            result["courses"] = courses
        except Exception as e:
            errors["courses"] = str(e); result["courses"] = []

        try:
            hw, _ = self.cached("homework", HW_TTL, lambda: self.fetch_homework(result["courses"]), fresh)
            for h in hw:
                h["deadline_ts"] = parse_ts(h.get("deadline"))
            hw.sort(key=lambda x: x.get("deadline_ts") or 9e15)
            result["homework"] = hw
        except Exception as e:
            errors["homework"] = str(e); result["homework"] = []

        try:
            result["todos"] = self.fetch_todos()
        except Exception as e:
            errors["todos"] = str(e); result["todos"] = []

        try:
            tt, _ = self.cached("timetable", DATA_TTL, self.fetch_timetable, fresh)
            result["timetable"] = tt
        except Exception as e:
            errors["timetable"] = str(e); result["timetable"] = None

        try:
            ex, _ = self.cached("exams", DATA_TTL, self.fetch_exams, fresh)
            result["exams"] = ex
        except Exception as e:
            errors["exams"] = str(e); result["exams"] = []

        result["errors"] = errors
        return result

    def api_status(self):
        cfg = self._load_config()
        return {"ok": True, "mock": self.mock, "bridge": True, "python": sys.version.split()[0],
                "logged_in": bool(self.mock or self.ensure_logged_in()),
                "user": self.username if not self.mock else "演示用户",
                "webvpn_logged_in": bool(self.has_webvpn()),
                "webvpn_user": self.webvpn_user or "",
                "remembered": bool(cfg.get("username")),
                "has_saved_password": bool(cfg.get("enc_password")),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def api_health(self):
        def ping(url):
            t0 = time.time()
            try:
                r = self.req(url, timeout=6, follow=False)
                return {"ok": r.status < 500, "status": r.status,
                        "ms": int((time.time() - t0) * 1000)}
            except Exception as e:
                return {"ok": False, "status": 0, "ms": -1, "error": e.__class__.__name__}
        return {"zjuam": ping("https://zjuam.zju.edu.cn/cas/login"),
                "courses": ping(COURSES + "/login"),
                "yjsy": ping(YJSY + "/dataapi/sys/cas/client/validateLogin"),
                "saa_public": ping("https://saa.zju.edu.cn"),
                "saa_office": ping("https://saa.office.zju.edu.cn")}

    def debug_yjsy(self):
        """研究生系统逐步诊断：CAS ticket → token → 课表原始响应（供排障）"""
        info = {"has_sso": self.has_sso(), "has_token": bool(self.yjsy_token),
                "xn_xq": self.current_xn_xq(), "steps": []}
        xn, _ = self.current_xn_xq()
        try:
            r = self.req(CAS_LOGIN + "?service=" + up.quote(YJSY_SERVICE, safe=""), follow=False)
            loc = r.headers.get("Location", "") or ""
            info["steps"].append({"step": "1.CAS获取ticket", "status": r.status,
                                  "location": loc[:150]})
            ticket = (up.parse_qs(up.urlparse(loc).query).get("ticket") or [None])[0]
            if not ticket:
                info["steps"].append({"step": "结果", "note": "未取到 ticket：SSO Cookie 可能已失效，请重新登录"})
                return info
            r2 = self.req(YJSY + "/dataapi/sys/cas/client/validateLogin?ticket=%s&service=%s"
                          % (up.quote(ticket), up.quote(YJSY_SERVICE, safe="")))
            info["steps"].append({"step": "2.validateLogin", "status": r2.status,
                                  "body": r2.text[:400]})
            try:
                token = (json.loads(r2.body.decode("utf-8")).get("result") or {}).get("token", "")
            except Exception:
                token = ""
            if not token:
                info["steps"].append({"step": "结果", "note": "未取到 token，请把上面 body 内容发给 ZCode 分析"})
                return info
            self.yjsy_token = token
            self.save_session()
            info["has_token"] = True
            xn, _ = self.current_xn_xq()
            r3 = self.req(YJSY + "/dataapi/py/pyKcbj/queryXskbByLoginUser?xn=%s&pkxq=13"
                          % up.quote(xn), headers={"X-Access-Token": token})
            info["steps"].append({"step": "3.课表(pkxq=13秋季)", "status": r3.status,
                                  "body": r3.text[:600]})
            n = -1
            try:
                n = len(parse_kcb_response(json.loads(r3.body.decode("utf-8"))))
            except Exception:
                pass
            info["steps"].append({"step": "结果", "note": "秋季学季解析出 %d 条课表记录" % n})
        except Exception as e:
            info["steps"].append({"step": "异常", "error": "%s: %s" % (e.__class__.__name__, e)})
        return info


# ----------------------------------------------------------------------------
# 解析工具
# ----------------------------------------------------------------------------
def parse_weeks(zc):
    """'1-8' / '9~16' / '1至8' / '1,3,5' / '1-8,10-16' -> [1,2,...]"""
    weeks = set()
    for part in re.split(r"[,，;；\s]+", str(zc or "")):
        m = re.match(r"^(\d+)\s*[-~～至]\s*(\d+)$", part)
        if m:
            weeks.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.strip().isdigit():
            weeks.add(int(part))
    return sorted(weeks)


def parse_kcb_response(j):
    """宽容解析课表响应：支持 result.kcbMap(周->节->列表/VO字段)、平铺 records 等结构"""
    if not isinstance(j, dict):
        return []
    res = j.get("result")
    if isinstance(res, dict) and isinstance(res.get("kcbMap"), dict):
        kcb = res["kcbMap"]
        sessions = []
        for wd, periods in kcb.items():
            if not isinstance(periods, dict):
                continue
            for p, entries in periods.items():
                if isinstance(entries, dict):        # 节次下还有一层 VO 列表
                    entries = entries.get("pyKcbjSjddVOList") or entries.get("list") or []
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    if not isinstance(e, dict) or str(e.get("xkzt")) == "12":
                        continue
                    try:
                        wdi, pi = int(wd), int(p)
                    except (TypeError, ValueError):
                        continue
                    if not (1 <= wdi <= 7 and 1 <= pi <= 15):
                        continue
                    sessions.append({
                        "weekday": wdi, "period": pi,
                        "course": e.get("kcmc", ""),
                        "teacher": e.get("xm", "") or e.get("jsxm", ""),
                        "location": e.get("cdmc", "") or e.get("jxcdmc", ""),
                        "weeks_raw": e.get("zc", "") or e.get("zcz", ""),
                        "weeks": parse_weeks(e.get("zc", "") or e.get("zcz", "")),
                    })
        return sessions
    # 备用：平铺记录列表（字段名尽量兼容）
    recs = None
    if isinstance(res, dict) and isinstance(res.get("records"), list):
        recs = res["records"]
    elif isinstance(res, list):
        recs = res
    if not recs:
        return []
    out = []
    for e in recs:
        if not isinstance(e, dict):
            continue
        try:
            wdi = int(e.get("xq") or e.get("xqj") or e.get("weekday") or 0)
            pi = int(e.get("jc") or e.get("djj") or e.get("period") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= wdi <= 7 and 1 <= pi <= 15):
            continue
        out.append({
            "weekday": wdi, "period": pi,
            "course": e.get("kcmc", ""),
            "teacher": e.get("xm", "") or e.get("jsxm", ""),
            "location": e.get("cdmc", "") or e.get("jxcdmc", ""),
            "weeks_raw": e.get("zc", ""),
            "weeks": parse_weeks(e.get("zc", "")),
        })
    return out


def parse_ts(s):
    """解析 ISO 时间字符串，返回 JS 风格的毫秒时间戳（供前端 Date 直接使用）"""
    if not s:
        return None
    t = str(s).replace("Z", "+00:00")
    m = re.match(r"^(.*[+-]\d{2}:?\d{2})", t)
    try:
        dt = datetime.fromisoformat(t[:19] if not m else t)
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.timestamp() * 1000.0
    except Exception:
        try:
            return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").timestamp() * 1000.0
        except Exception:
            return None


def normalize_date(s):
    m = re.search(r"(20\d\d)[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})", s)
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


ARTICLE_HREF = re.compile(r"/20\d\d/[^\"']*c[^\"']*\.htm", re.I)


def extract_notices(html, base):
    """宽容解析学院官网(WebPlus 类 CMS)的通知列表"""
    items, seen = [], set()
    for m in re.finditer(r'<a\b([^>]*)>(.*?)</a>', html, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        hm = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not hm:
            continue
        href = hm.group(1)
        if not ARTICLE_HREF.search(href):
            continue
        tm = re.search(r'title=["\']([^"\']{6,})["\']', attrs)
        title = (tm.group(1) if tm else re.sub(r"<[^>]+>", "", inner)).strip()
        title = re.sub(r"\s+", " ", title)
        if len(title) < 8:      # 过短的多为导航/栏目名
            continue
        ctx = html[m.end(): m.end() + 400]
        dm = re.search(r"20\d\d[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}", ctx)
        date = normalize_date(dm.group(0)) if dm else ""
        if not date:
            continue
        url = up.urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        items.append({"title": title[:120], "url": url, "date": date})
    return items


LIST_HINT = re.compile(r"(通知|公告|新闻|动态|news|notice|tzgg|xwdt|zpxx|list\d*)", re.I)


def discover_list_pages(html, base):
    urls = []
    for m in re.finditer(r'href=["\']([^"\']+)["\'][^>]*>([^<]{2,20})<', html, re.I):
        href, text = m.group(1), m.group(2).strip()
        if "list" in href and LIST_HINT.search(text or "") or LIST_HINT.search(text or "") and href.endswith((".htm", ".html", "/")):
            u = up.urljoin(base, href)
            if u not in urls and u.rstrip("/") != base.rstrip("/"):
                urls.append(u)
    return urls[:5]


# ----------------------------------------------------------------------------
# 演示数据
# ----------------------------------------------------------------------------
def MOCK_ALL():
    now = datetime.now()
    def d(days, h=23, mnt=59):
        return (now + timedelta(days=days)).strftime("%Y-%m-%d") + "T%02d:%02d:00+08:00" % (h, mnt)
    courses = [
        {"id": "m1", "name": "连续介质力学", "code": "MECH501", "teacher": "张教授"},
        {"id": "m2", "name": "软物质物理", "code": "PHYS620", "teacher": "李教授"},
        {"id": "m3", "name": "机器学习理论与应用", "code": "CS540", "teacher": "王教授"},
        {"id": "m4", "name": "非线性弹性力学", "code": "MECH610", "teacher": "陈教授"},
    ]
    homework = [
        {"id": "h1", "course": "连续介质力学", "course_id": "m1", "title": "第3章 有限应变张量习题",
         "deadline": d(1), "submitted": False, "closed": False, "description": "证明题 3.2 / 3.5 / 3.9", "url": "#"},
        {"id": "h2", "course": "机器学习理论与应用", "course_id": "m3", "title": "反向传播推导报告",
         "deadline": d(3), "submitted": False, "closed": False, "description": "手写推导 + 代码", "url": "#"},
        {"id": "h3", "course": "软物质物理", "course_id": "m2", "title": "橡胶弹性文献综述",
         "deadline": d(-1), "submitted": False, "closed": False, "description": "不少于 3000 字", "url": "#"},
        {"id": "h4", "course": "非线性弹性力学", "course_id": "m4", "title": "Ogden 模型拟合练习",
         "deadline": d(7), "submitted": True, "closed": False, "description": "使用 provided 数据", "url": "#"},
        {"id": "h5", "course": "连续介质力学", "course_id": "m1", "title": "第2章作业",
         "deadline": d(-8), "submitted": True, "closed": True, "description": "", "url": "#"},
        {"id": "h6", "course": "软物质物理", "course_id": "m2", "title": "课堂展示 PPT",
         "deadline": d(12), "submitted": False, "closed": False, "description": "20 分钟", "url": "#"},
    ]
    sessions = []
    plan = [
        (1, 1, 2, "连续介质力学", "张教授", "紫金港东1-319"),
        (1, 6, 8, "机器学习理论与应用", "王教授", "玉泉教十一-405"),
        (2, 1, 2, "软物质物理", "李教授", "紫金港东1-401"),
        (3, 3, 5, "非线性弹性力学", "陈教授", "玉泉教十-201"),
        (3, 11, 12, "连续介质力学(习题课)", "张教授", "紫金港东1-319"),
        (4, 6, 7, "软物质物理", "李教授", "紫金港东1-401"),
        (5, 1, 2, "机器学习理论与应用", "王教授", "玉泉教十一-405"),
    ]
    for wd, p1, p2, name, t, loc in plan:
        for p in range(p1, p2 + 1):
            sessions.append({"weekday": wd, "period": p, "course": name, "teacher": t,
                             "location": loc, "weeks_raw": "1-16",
                             "weeks": list(range(1, 17))})
    timetable = {"xn": "2026-2027", "xq": 15, "sessions": sessions}
    exams = [
        {"course": "连续介质力学", "date": "2026-11-05", "time": "14:00~16:00",
         "location": "紫金港东1-319", "seat": "12"},
        {"course": "机器学习理论与应用", "date": "2026-12-28", "time": "09:00~11:00",
         "location": "玉泉教十一-405", "seat": "08"},
    ]
    notices = [
        {"title": "关于2026年秋季学期研究生中期考核安排的通知", "date": "2026-09-28",
         "url": "#", "source": "saa.zju.edu.cn"},
        {"title": "航空航天学院学术报告：Soft Matter Mechanics and Machine Learning",
         "date": "2026-09-25", "url": "#", "source": "saa.zju.edu.cn"},
        {"title": "关于评选2026年度国家奖学金的通知（内网）", "date": "2026-09-22",
         "url": "#", "source": "saa.office(内网)"},
    ]
    cal = {"year": "2026-2027",
           "configs": {"2026-2027-1": EMBEDDED_CALENDAR_1},
           "fetched_at": "mock"}
    return {"courses": courses, "homework": homework, "todos": [],
            "timetable": timetable, "exams": exams, "calendar": cal,
            "notices": {"ok": True, "items": notices, "errors": [],
                        "fetched_at": "mock"}}


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
CT = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8", ".md": "text/plain; charset=utf-8",
      ".json": "application/json; charset=utf-8", ".png": "image/png",
      ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".tex": "text/plain; charset=utf-8",
      ".txt": "text/plain; charset=utf-8"}


# ----------------------------------------------------------------------------
# 资料库（文件浏览/上传）——路径安全 + 外部盘关联
# ----------------------------------------------------------------------------
DENY_FILES = {"config.json", "session.json", "cache.json"}
DENY_EXT = {".exe"}


def _lib_links():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("lib_links", [])
    except Exception:
        return []


def _save_lib_links(links):
    cfg = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg["lib_links"] = links
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def safe_join(rel):
    """把相对路径安全地解析到工作台根目录内，或已关联的外部文件夹内。
    内部路径：普通相对路径；外部：ext:<序号>/子路径。返回绝对路径或 None"""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if rel.startswith("ext:"):
        rest = rel[4:]
        parts = rest.split("/", 1)
        try:
            idx = int(parts[0])
        except ValueError:
            return None
        links = _lib_links()
        if idx < 0 or idx >= len(links):
            return None
        root = links[idx].get("path", "")
        sub = parts[1] if len(parts) > 1 else ""
        full = os.path.normpath(os.path.join(root, sub))
        if not (full == os.path.normpath(root)
                or full.startswith(os.path.normpath(root) + os.sep)):
            return None
        if os.path.isfile(full) and os.path.splitext(full)[1].lower() in DENY_EXT:
            return None
        return full
    full = os.path.normpath(os.path.join(ROOT_DIR, rel))
    if not (full == os.path.normpath(ROOT_DIR)
            or full.startswith(os.path.normpath(ROOT_DIR) + os.sep)):
        return None
    if os.path.isfile(full) and (os.path.basename(full) in DENY_FILES
                                 or os.path.splitext(full)[1].lower() in DENY_EXT):
        return None
    return full


def fs_list(rel):
    """列出工作台内某目录：文件夹在前，附大小/修改时间"""
    full = safe_join(rel)
    if full is None or not os.path.isdir(full):
        return {"ok": False, "error": "目录不存在或不可访问"}
    entries = []
    try:
        for name in os.listdir(full):
            if name.startswith(".") or name in DENY_FILES:
                continue
            p = os.path.join(full, name)
            is_dir = os.path.isdir(p)
            try:
                st = os.stat(p)
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                size = 0 if is_dir else st.st_size
            except OSError:
                mtime, size = "", 0
            entries.append({"name": name, "dir": is_dir, "size": size, "mtime": mtime})
    except OSError as e:
        return {"ok": False, "error": str(e)}
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    rel_in = (rel or "").strip().replace("\\", "/").lstrip("/")
    if rel_in.startswith("ext:"):
        rel_norm = rel_in.rstrip("/")
        parent = "/".join(rel_norm.split("/")[:-1])
        if not parent.startswith("ext:"):
            parent = ""
    else:
        rel_norm = os.path.relpath(full, ROOT_DIR).replace("\\", "/")
        if rel_norm == ".":
            rel_norm = ""
        parent = "/".join(rel_norm.split("/")[:-1]) if rel_norm else ""
    return {"ok": True, "path": rel_norm, "parent": parent, "entries": entries}


def fs_mkdir(rel, name):
    full = safe_join(os.path.join(rel, name))
    if full is None:
        return {"ok": False, "error": "非法路径"}
    try:
        os.makedirs(full, exist_ok=True)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def fs_delete(rel):
    import shutil
    full = safe_join(rel)
    if full is None or full == os.path.normpath(ROOT_DIR):
        return {"ok": False, "error": "非法路径"}
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def load_state():
    """工作台页面数据（任务/文献/笔记/灵感/设置…）的持久化副本"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(db):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
    except Exception as e:
        log("save_state failed: %s" % e)


def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            return json.load(f).get("keys", [])
    except Exception:
        return []


def save_seen(keys):
    try:
        keys = list(dict.fromkeys(keys))[-3000:]     # 去重并限制规模
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"keys": keys}, f, ensure_ascii=False)
    except Exception as e:
        log("save_seen failed: %s" % e)


class Handler(BaseHTTPRequestHandler):
    bridge = None    # type: Bridge
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass    # 静默默认日志，避免刷屏

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        parsed = up.urlparse(self.path)
        path, qs = parsed.path, up.parse_qs(parsed.query)
        fresh = qs.get("fresh", ["0"])[0] == "1"
        B = self.bridge
        try:
            if path == "/api/status":
                return self._json(B.api_status())
            if path == "/api/health":
                return self._json(B.api_health())
            if path == "/api/captcha":
                r = B.req(CAS_CAPTCHA + "?t=%d" % int(time.time() * 1000), timeout=10)
                self.send_response(r.status)
                self.send_header("Content-Type", "image/jpeg")
                self._cors()
                self.send_header("Content-Length", str(len(r.body)))
                self.end_headers()
                return self.wfile.write(r.body)
            if path == "/api/all":
                return self._json(B.api_all(fresh=fresh))
            if path == "/api/notices":
                if B.mock:
                    return self._json(MOCK_ALL()["notices"])
                payload, _ = B.cached("notices", NOTICE_TTL, B.fetch_notices,
                                      fresh, persist=True)
                return self._json(payload)
            if path == "/api/bus":
                if B.mock:
                    return self._json(MOCK_BUS())
                # 班次列表 10 分钟内视为新鲜（贴近实时），经停站详情自动复用磁盘缓存
                payload, _ = B.cached("bus", 600, B.fetch_bus, fresh, persist=True)
                return self._json(payload)
            if path == "/api/campusbus/stations":
                if B.mock:
                    return self._json({"ok": True, "stations": ["云峰西侧（校车停车场）", "银泉", "材化高组团", "玉湖", "管理学院", "文科组团（澄月）", "医药组团", "农生环组团", "东教学区", "风雨操场（蒙民伟楼）", "东二门"]})
                payload, _ = B.cached("cb_stations", B.CB_TTL, B.fetch_cb_stations, fresh, persist=True)
                return self._json(payload)
            if path == "/api/campusbus/stop":
                if B.mock:
                    return self._json({"ok": True, "station": "东教学区", "has_data": True, "lines": [
                        {"line": "校内1号线（小白车）", "start_time": "10:30",
                         "buses": [{"carno": "浙A·小白01", "speed": "12", "state": "未到站", "next_station": "医药组团", "distance": "600m"}]},
                        {"line": "校内2号线（宝宝巴士）", "start_time": "11:00",
                         "buses": [{"carno": "浙A·宝宝02", "speed": "0", "state": "等待发车", "next_station": "", "distance": ""}]}],
                        "fetched_at": "mock"})
                name = qs.get("name", [""])[0]
                if not name:
                    return self._json({"ok": False, "error": "缺少站点名"})
                return self._json(B.fetch_cb_stop(name))
            if path == "/api/webvpn/status":
                if not B.has_webvpn() and not B.mock:
                    B.try_webvpn_auto_login()   # 存了加密密码时静默重登
                return self._json({"ok": True, "logged_in": bool(B.has_webvpn()),
                                   "user": B.webvpn_user or ""})
            if path == "/api/webvpn/captcha":
                body_, ctype_ = B.webvpn_captcha()
                if not body_:
                    return self._json({"ok": False, "error": "验证码获取失败"}, 502)
                self.send_response(200)
                self.send_header("Content-Type", ctype_)
                self._cors()
                self.send_header("Content-Length", str(len(body_)))
                self.end_headers()
                return self.wfile.write(body_)
            if path == "/api/fs":
                return self._json(fs_list(qs.get("path", [""])[0]))
            if path == "/api/notices/seen":
                return self._json({"ok": True, "keys": load_seen()})
            if path == "/api/state":
                st = load_state()
                return self._json({"ok": True, "db": st.get("db"), "updatedAt": st.get("updatedAt", "")})
            if path == "/api/fs/links":
                return self._json({"ok": True, "links": _lib_links()})
            if path == "/api/fs/open":
                full = safe_join(qs.get("path", [""])[0])
                if full and os.path.isdir(full):
                    try:
                        os.startfile(full)          # 文件夹 → 资源管理器
                        return self._json({"ok": True})
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)})
                if full and os.path.isfile(full):
                    try:
                        os.startfile(full)          # 文件 → 系统默认应用（Word/PDF/PPT...）
                        return self._json({"ok": True})
                    except Exception as e:
                        return self._json({"ok": False, "error": "没有可用的默认应用：" + str(e)})
                return self._json({"ok": False, "error": "文件或目录不存在"})
            if path == "/api/calendar":
                payload, _ = B.cached("calendar", CALENDAR_TTL, B.fetch_calendar,
                                      fresh, persist=True)
                return self._json(payload)
            if path == "/api/debug/yjsy":
                return self._json(B.debug_yjsy())
            # 静态文件
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)}, 500)
            except Exception:
                pass

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(ROOT_DIR, rel))
        if not full.startswith(os.path.normpath(ROOT_DIR)) or not os.path.isfile(full):
            self.send_response(404)
            self._cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "9")
            self.end_headers()
            return self.wfile.write(b"not found")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CT.get(ext, "application/octet-stream"))
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- POST ----
    def do_POST(self):
        path = up.urlparse(self.path).path
        B = self.bridge
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            # 文件上传：查询串带 path/name，请求体即文件内容（前端用 fetch 发原始字节）
            if path == "/api/fs/upload":
                qs = up.parse_qs(up.urlparse(self.path).query)
                rel = qs.get("path", [""])[0]
                name = os.path.basename((qs.get("name", [""])[0] or "file").replace("\\", "/"))
                if not name or name.startswith("."):
                    return self._json({"ok": False, "error": "非法文件名"}, 400)
                if os.path.splitext(name)[1].lower() in DENY_EXT:
                    return self._json({"ok": False, "error": "不允许上传该类型文件"}, 400)
                full = safe_join(os.path.join(rel, name))
                if full is None:
                    return self._json({"ok": False, "error": "非法路径"}, 400)
                with open(full, "wb") as f:
                    f.write(raw)
                return self._json({"ok": True, "name": name, "size": len(raw)})
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}
            if path == "/api/log":
                msg = str(body.get("text", ""))[:8000]
                log("PAGE: " + msg.replace("\n", " | "))
                return self._json({"ok": True})
            if path == "/api/login":
                username = (body.get("username") or "").strip()
                password = body.get("password") or ""
                captcha = body.get("captcha") or ""
                remember = bool(body.get("remember"))
                if not username or not password:
                    return self._json({"ok": False, "message": "请输入学号和密码"})
                ok, msg, needs_captcha = B.cas_login(username, password, captcha)
                if ok and remember:
                    B.save_credentials(username, password)
                elif ok:
                    B.save_credentials(username)
                result = {"ok": ok, "message": msg, "needs_captcha": needs_captcha,
                          "user": B.username}
                if ok:
                    result["data"] = B.api_all(fresh=True)
                return self._json(result)
            if path == "/api/logout":
                B.jar.clear()
                B.yjsy_token = ""
                B.username = ""
                try:
                    os.remove(SESSION_PATH)
                except OSError:
                    pass
                return self._json({"ok": True, "message": "已退出登录"})
            if path == "/api/webvpn/login":
                username = (body.get("username") or "").strip()
                password = body.get("password") or ""
                captcha = body.get("captcha") or ""
                remember = bool(body.get("remember"))
                if not username or not password:
                    return self._json({"ok": False, "message": "请输入上网账号和密码"})
                ok, msg, needs_captcha = B.webvpn_login(username, password, captcha)
                if ok and remember:
                    cfg = B._load_config()
                    cfg["webvpn_user"] = username
                    try:
                        cfg["webvpn_enc_password"] = dpapi_protect(password.encode("utf-8"))
                    except Exception:
                        cfg["webvpn_enc_password"] = ""
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                return self._json({"ok": ok, "message": msg, "needs_captcha": needs_captcha})
            if path == "/api/webvpn/logout":
                B.webvpn_jar.clear()
                B.webvpn_user = ""
                B.save_session()
                return self._json({"ok": True, "message": "已退出 WebVPN"})
            if path == "/api/state":
                db = body.get("db")
                if isinstance(db, dict):
                    save_state({"db": db, "updatedAt": body.get("updatedAt", "")})
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "db 必须是对象"}, 400)
            if path == "/api/notices/seen":
                keys = body.get("keys") or []
                if isinstance(keys, list) and keys:
                    merged = load_seen() + [str(k) for k in keys]
                    save_seen(merged)
                return self._json({"ok": True, "total": len(load_seen())})
            if path == "/api/fs/mkdir":
                return self._json(fs_mkdir(body.get("path", ""), body.get("name", "")))
            if path == "/api/fs/delete":
                return self._json(fs_delete(body.get("path", "")))
            if path == "/api/fs/links/add":
                name = (body.get("name") or "").strip()
                p = (body.get("path") or "").strip()
                if not name or not p:
                    return self._json({"ok": False, "error": "请填写名称和路径"})
                if not os.path.isdir(p):
                    return self._json({"ok": False, "error": "路径不存在或不是文件夹：" + p})
                links = _lib_links()
                if any(l.get("path", "").lower() == os.path.normpath(p).lower() for l in links):
                    return self._json({"ok": False, "error": "该文件夹已关联过"})
                links.append({"name": name, "path": os.path.normpath(p)})
                _save_lib_links(links)
                return self._json({"ok": True, "links": links})
            if path == "/api/fs/links/remove":
                idx = int(body.get("index", -1))
                links = _lib_links()
                if 0 <= idx < len(links):
                    removed = links.pop(idx)
                    _save_lib_links(links)
                    return self._json({"ok": True, "removed": removed.get("name", "")})
                return self._json({"ok": False, "error": "序号无效"})
            return self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)}, 500)
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def self_check():
    B = Bridge()
    log("ZJU bridge network self-check")
    h = B.api_health()
    for name, r in h.items():
        mark = "OK " if r.get("ok") else "FAIL"
        log("  %-11s %-4s status=%-4s %sms %s" % (name, mark, r.get("status"), r.get("ms"), r.get("error", "")))
    log("done.")


class QuietServer(ThreadingHTTPServer):
    """禁止 Windows 下的端口重复绑定：端口被占时直接换下一个端口"""
    allow_reuse_address = False


def main():
    ap = argparse.ArgumentParser(description="ZJU bridge for research workbench")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mock", action="store_true", help="serve demo data without login")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="run network self-check and exit")
    args = ap.parse_args()

    if args.check:
        self_check()
        return

    B = Bridge(mock=args.mock)
    Handler.bridge = B

    port, server = args.port, None
    for p in range(args.port, args.port + 20):
        try:
            server = QuietServer(("127.0.0.1", p), Handler)
            server.daemon_threads = True
            port = p
            break
        except OSError:
            log("port %d busy, trying next..." % p)
    if server is None:
        log("ERROR: no free port in range")
        sys.exit(1)

    url = "http://127.0.0.1:%d/" % port
    log("=" * 56)
    log("ZJU bridge started%s" % ("  [MOCK demo mode]" if args.mock else ""))
    log("Workbench: %s" % url)
    log("Keep this window open. Ctrl+C to stop.")
    log("=" * 56)
    # 后台预取校历与校车班次（启动即可用最新数据）
    if not args.mock:
        threading.Thread(target=lambda: _safe(B.fetch_calendar), daemon=True).start()
        threading.Thread(target=lambda: _safe(B.fetch_bus), daemon=True).start()
    if not args.no_browser:
        try:
            os.startfile(url)   # Windows
        except Exception:
            threading.Thread(target=lambda: (time.sleep(.5), webbrowser.open(url)),
                             daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye.")


if __name__ == "__main__":
    main()
