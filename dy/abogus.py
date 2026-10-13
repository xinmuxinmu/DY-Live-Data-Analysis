# -*- coding: utf-8 -*-
"""
抖音 a_bogus 请求签名 —— 纯 Python 实现（无第三方依赖）

算法来源：DY直播数据分析（DNGuard 保护）成品包内嵌 JS。
原始 JS 保存在本目录的 abogus_origin.js，本文件是其等价 Python 移植，
并已用固定随机数/时间戳与原始 JS 做逐字节比对验证（见 selftest.py）。

包含：
  - RC4
  - SM3（国密哈希）
  - 抖音自定义 base64（s0~s4 五套字母表）
  - a_bogus 组装

对外只需调用：a_bogus(query_string, user_agent)
"""
from __future__ import annotations

import random as _random
import time
import urllib.parse

MASK32 = 0xFFFFFFFF

# ---------------------------------------------------------------- JS 语义助手

def _to_int32(x) -> int:
    """JS ToInt32：截断取整后按 2^32 取模，映射到有符号范围。"""
    v = int(x) & MASK32
    return v - 0x100000000 if v >= 0x80000000 else v


def _to_uint32(x) -> int:
    """JS ToUint32。"""
    return int(x) & MASK32


def _shr(a, b) -> int:
    """JS >> （有符号右移，带符号扩展）。"""
    b &= 31
    return _to_int32(_to_int32(a) >> b)


def _ushr(a, b) -> int:
    """JS >>> （无符号右移）。"""
    b &= 31
    return _to_uint32(_to_uint32(a) >> b)


def _shl(a, b) -> int:
    """JS << （有符号左移）。"""
    b &= 31
    return _to_int32(_to_int32(a) << b)


def _le(e, r) -> int:
    """JS: (e << (r %= 32) | e >>> 32 - r) >>> 0  —— 循环左移。"""
    r %= 32
    return _to_uint32(_shl(e, r) | _ushr(e, 32 - r))


# ------------------------------------------------------------------------ RC4

def rc4_encrypt(plaintext, key) -> str:
    """与原始 JS 一致：入参为「码位 0~255 的字符串」，返回同型字符串。"""
    s = list(range(256))
    klen = len(key)
    j = 0
    for i in range(256):
        j = (j + s[i] + (ord(key[i % klen]) if klen else 0)) % 256
        s[i], s[j] = s[j], s[i]
    out = []
    i = j = 0
    for ch in plaintext:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(chr(s[(s[i] + s[j]) % 256] ^ ord(ch)))
    return "".join(out)


# ------------------------------------------------------------------------ SM3

def _sm3_tj(j) -> int:
    if 0 <= j < 16:
        return 2043430169
    if 16 <= j < 64:
        return 2055708042
    raise ValueError("invalid j for constant Tj")


def _sm3_ff(j, x, y, z) -> int:
    if 0 <= j < 16:
        return _to_uint32(x ^ y ^ z)
    if 16 <= j < 64:
        return _to_uint32((x & y) | (x & z) | (y & z))
    raise ValueError("invalid j for bool function FF")


def _sm3_gg(j, x, y, z) -> int:
    if 0 <= j < 16:
        return _to_uint32(x ^ y ^ z)
    if 16 <= j < 64:
        return _to_uint32((x & y) | (~x & z))
    raise ValueError("invalid j for bool function GG")


class SM3:
    def __init__(self):
        self.reg = [0] * 8
        self.chunk = []
        self.size = 0
        self.reset()

    def reset(self):
        self.reg = [1937774191, 1226093241, 388252375, 3666478592,
                    2842636476, 372324522, 3817729613, 2969243214]
        self.chunk = []
        self.size = 0

    def write(self, data):
        if isinstance(data, str):
            # 等价于 JS 的 encodeURIComponent -> 逐字节还原（即 UTF-8 字节序列）
            a = list(data.encode("utf-8"))
        else:
            a = list(data)
        self.size += len(a)
        f = 64 - len(self.chunk)
        if len(a) < f:
            self.chunk += a
            return
        self.chunk += a[:f]
        while len(self.chunk) >= 64:
            self._compress(self.chunk[:64])
            if f < len(a):
                self.chunk = a[f:min(f + 64, len(a))]
            else:
                self.chunk = []
            f += 64

    def sum(self, data=None, fmt=None):
        if data is not None and data is not False:
            self.reset()
            self.write(data)
        self._fill()
        for f in range(0, len(self.chunk), 64):
            self._compress(self.chunk[f:f + 64])
        if fmt == "hex":
            out = "".join(format(self.reg[f], "x").rjust(8, "0") for f in range(8))
        else:
            out = [0] * 32
            for f in range(8):
                c = self.reg[f]
                out[4 * f + 3] = c & 255
                c = _ushr(c, 8)
                out[4 * f + 2] = c & 255
                c = _ushr(c, 8)
                out[4 * f + 1] = c & 255
                c = _ushr(c, 8)
                out[4 * f] = c & 255
        self.reset()
        return out

    def _compress(self, t):
        if len(t) < 64:
            raise ValueError("compress error: not enough data")
        f = [0] * 132
        for i in range(16):
            v = (_shl(t[4 * i], 24) | _shl(t[4 * i + 1], 16)
                 | _shl(t[4 * i + 2], 8) | t[4 * i + 3])
            f[i] = _to_uint32(v)
        for n in range(16, 68):
            a = f[n - 16] ^ f[n - 9] ^ _le(f[n - 3], 15)
            a = a ^ _le(a, 15) ^ _le(a, 23)
            f[n] = _to_uint32(a ^ _le(f[n - 13], 7) ^ f[n - 6])
        for n in range(64):
            f[n + 68] = _to_uint32(f[n] ^ f[n + 4])

        i = list(self.reg)
        for c in range(64):
            o = _le(i[0], 12) + i[4] + _le(_sm3_tj(c), c)
            o = _le(_to_uint32(o), 7)
            s = _to_uint32(o ^ _le(i[0], 12))
            u = _sm3_ff(c, i[0], i[1], i[2])
            u = _to_uint32(u + i[3] + s + f[c + 68])
            b = _sm3_gg(c, i[4], i[5], i[6])
            b = _to_uint32(b + i[7] + o + f[c])
            i[3] = i[2]
            i[2] = _le(i[1], 9)
            i[1] = i[0]
            i[0] = u
            i[7] = i[6]
            i[6] = _le(i[5], 19)
            i[5] = i[4]
            i[4] = _to_uint32(b ^ _le(b, 9) ^ _le(b, 17))
        for l in range(8):
            self.reg[l] = _to_uint32(self.reg[l] ^ i[l])

    def _fill(self):
        a = 8 * self.size
        self.chunk.append(128)
        f = len(self.chunk) % 64
        if 64 - f < 8:
            f -= 64
        while f < 56:
            self.chunk.append(0)
            f += 1
        for i in range(4):
            c = a // 4294967296
            self.chunk.append(_ushr(c, 8 * (3 - i)) & 255)
        for i in range(4):
            self.chunk.append(_ushr(a, 8 * (3 - i)) & 255)


# --------------------------------------------------- 抖音自定义 base64 编码

_ALPHABET = {
    "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
    "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
    "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
    "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
}


def _get_long_int(round_, long_str) -> int:
    p = round_ * 3
    n = len(long_str)

    def cc(idx):
        return ord(long_str[idx]) if 0 <= idx < n else 0

    return (_shl(cc(p), 16) | _shl(cc(p + 1), 8) | cc(p + 2))


def result_encrypt(long_str, num) -> str:
    table = _ALPHABET[num]
    result = []
    lound = 0
    long_int = _get_long_int(0, long_str)
    # 注意：原始 JS 的循环上界是浮点数 len/3*4，不做截断，
    # 因此长度非 3 的倍数时会多跑一轮。这里必须保持同样语义。
    total = len(long_str) / 3 * 4
    i = 0
    while i < total:
        if i // 4 != lound:
            lound += 1
            long_int = _get_long_int(lound, long_str)
        key = i % 4
        if key == 0:
            temp = _shr(long_int & 16515072, 18)
        elif key == 1:
            temp = _shr(long_int & 258048, 12)
        elif key == 2:
            temp = _shr(long_int & 4032, 6)
        else:
            temp = long_int & 63
        result.append(table[temp])
        i += 1
    return "".join(result)


# ------------------------------------------------------------------ a_bogus

def gener_random(random_v, option):
    random_v = _to_int32(random_v)
    return [
        (random_v & 255 & 170) | (option[0] & 85),
        (random_v & 255 & 85) | (option[0] & 170),
        (_shr(random_v, 8) & 255 & 170) | (option[1] & 85),
        (_shr(random_v, 8) & 255 & 85) | (option[1] & 170),
    ]


def generate_random_str(rand=_random.random) -> str:
    lst = []
    lst += gener_random(rand() * 10000, [3, 45])
    lst += gener_random(rand() * 10000, [1, 0])
    lst += gener_random(rand() * 10000, [1, 5])
    return "".join(chr(x) for x in lst)


def generate_rc4_bb_str(url_search_params, user_agent, window_env_str,
                        suffix="cus", arguments=(0, 1, 14),
                        now=lambda: int(time.time() * 1000)):
    sm3 = SM3()
    start_time = now()
    url_search_params_list = sm3.sum(sm3.sum(url_search_params + suffix))
    cus = sm3.sum(sm3.sum(suffix))
    ua = sm3.sum(result_encrypt(
        rc4_encrypt(user_agent, "".join(chr(x) for x in (0, 1, 14))), "s3"))
    end_time = now()

    b = {
        8: 3,
        10: end_time,
        15: {"aid": 6383, "pageId": 6241},
        16: start_time,
        18: 44,
        19: [1, 0, 1, 5],
    }
    b[20] = _shr(b[16], 24) & 255
    b[21] = _shr(b[16], 16) & 255
    b[22] = _shr(b[16], 8) & 255
    b[23] = b[16] & 255
    b[24] = _shr(b[16] / 256 / 256 / 256 / 256, 0)
    b[25] = _shr(b[16] / 256 / 256 / 256 / 256 / 256, 0)
    b[26] = _shr(arguments[0], 24) & 255
    b[27] = _shr(arguments[0], 16) & 255
    b[28] = _shr(arguments[0], 8) & 255
    b[29] = arguments[0] & 255
    b[30] = (arguments[1] // 256) & 255
    b[31] = (arguments[1] % 256) & 255
    b[32] = _shr(arguments[1], 24) & 255
    b[33] = _shr(arguments[1], 16) & 255
    b[34] = _shr(arguments[2], 24) & 255
    b[35] = _shr(arguments[2], 16) & 255
    b[36] = _shr(arguments[2], 8) & 255
    b[37] = arguments[2] & 255
    b[38] = url_search_params_list[21]
    b[39] = url_search_params_list[22]
    b[40] = cus[21]
    b[41] = cus[22]
    b[42] = ua[23]
    b[43] = ua[24]
    b[44] = _shr(b[10], 24) & 255
    b[45] = _shr(b[10], 16) & 255
    b[46] = _shr(b[10], 8) & 255
    b[47] = b[10] & 255
    b[48] = b[8]
    b[49] = _shr(b[10] / 256 / 256 / 256 / 256, 0)
    b[50] = _shr(b[10] / 256 / 256 / 256 / 256 / 256, 0)
    b[51] = b[15]["pageId"]
    b[52] = _shr(b[15]["pageId"], 24) & 255
    b[53] = _shr(b[15]["pageId"], 16) & 255
    b[54] = _shr(b[15]["pageId"], 8) & 255
    b[55] = b[15]["pageId"] & 255
    b[56] = b[15]["aid"]
    b[57] = b[15]["aid"] & 255
    b[58] = _shr(b[15]["aid"], 8) & 255
    b[59] = _shr(b[15]["aid"], 16) & 255
    b[60] = _shr(b[15]["aid"], 24) & 255

    window_env_list = [ord(c) for c in window_env_str]
    b[64] = len(window_env_list)
    b[65] = b[64] & 255
    b[66] = _shr(b[64], 8) & 255
    b[69] = 0
    b[70] = b[69] & 255
    b[71] = _shr(b[69], 8) & 255
    b[72] = (b[18] ^ b[20] ^ b[26] ^ b[30] ^ b[38] ^ b[40] ^ b[42] ^ b[21]
             ^ b[27] ^ b[31] ^ b[35] ^ b[39] ^ b[41] ^ b[43] ^ b[22] ^ b[28]
             ^ b[32] ^ b[36] ^ b[23] ^ b[29] ^ b[33] ^ b[37] ^ b[44] ^ b[45]
             ^ b[46] ^ b[47] ^ b[48] ^ b[49] ^ b[50] ^ b[24] ^ b[25] ^ b[52]
             ^ b[53] ^ b[54] ^ b[55] ^ b[57] ^ b[58] ^ b[59] ^ b[60] ^ b[65]
             ^ b[66] ^ b[70] ^ b[71])

    bb = [b[18], b[20], b[52], b[26], b[30], b[34], b[58], b[38], b[40], b[53],
          b[42], b[21], b[27], b[54], b[55], b[31], b[35], b[57], b[39], b[41],
          b[43], b[22], b[28], b[32], b[60], b[36], b[23], b[29], b[33], b[37],
          b[44], b[45], b[59], b[46], b[47], b[48], b[49], b[50], b[24], b[25],
          b[65], b[66], b[70], b[71]]
    bb = bb + window_env_list + [b[72]]
    return rc4_encrypt("".join(chr(x & 255) for x in bb), chr(121))


WINDOW_ENV = "1536|747|1536|834|0|30|0|0|1536|834|1536|864|1525|747|24|24|Win32"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def a_bogus(url_search_params: str, user_agent: str = DEFAULT_UA,
            rand=_random.random, now=lambda: int(time.time() * 1000)) -> str:
    """生成抖音 web 端 a_bogus 签名（已 URL 编码，可直接拼进 query）。"""
    if not user_agent:
        user_agent = DEFAULT_UA
    result_str = (generate_random_str(rand)
                  + generate_rc4_bb_str(url_search_params, user_agent,
                                        WINDOW_ENV, now=now))
    raw = result_encrypt(result_str, "s4") + "="
    return urllib.parse.quote(raw, safe="-_.!~*'()")
