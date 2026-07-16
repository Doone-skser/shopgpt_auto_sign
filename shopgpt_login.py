#!/usr/bin/env python3
"""
ShopGPT (shopgpt.plus) 登录 + 签到脚本

登录流程（对应前端 login.js）:
  1. GET  /user/authentication/login
  2. GET  /user/captcha/image?action=login
  3. POST /user/api/authentication/login
     成功: JSON {"code": 200, ...}

签到/领取奖励:
  POST /user/api/coreuser/claim
  body: benefit_id=1
  成功示例: {"code":200,"msg":"领取成功","data":{"type":"balance","balance":1.88}}

验证码为 4 位数字。自动识别时若结果不是 4 位数字，会重新拉取验证码重试。

会话 cookie: ACG-SHOP + USER_SESSION

用法:
  # 仅登录
  python3 shopgpt_login.py -u 用户名 -p 密码

  # 登录后签到
  python3 shopgpt_login.py -u 用户名 -p 密码 --claim

  # 用已有 cookie 一键签到（推荐日常）
  python3 shopgpt_login.py --claim-only
  python3 shopgpt_login.py --claim-only --cookie-file shopgpt_cookies.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE_URL = "https://shopgpt.plus"
LOGIN_PAGE = f"{BASE_URL}/user/authentication/login"
CAPTCHA_URL = f"{BASE_URL}/user/captcha/image?action=login"
LOGIN_API = f"{BASE_URL}/user/api/authentication/login"
CLAIM_API = f"{BASE_URL}/user/api/coreuser/claim"
COREUSER_PAGE = f"{BASE_URL}/user/coreuser/index"
COREUSER_INFO_API = f"{BASE_URL}/user/api/coreuser/info"

DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Mobile Safari/537.36"
)
DEFAULT_BENEFIT_ID = 1
DEFAULT_COOKIE_FILE = "shopgpt_cookies.json"

# 站点验证码固定为 4 位数字
CAPTCHA_DIGITS = 4
CAPTCHA_RE = re.compile(rf"^\d{{{CAPTCHA_DIGITS}}}$")


def is_valid_captcha(text: str | None) -> bool:
    """识别结果必须是恰好 4 位数字才算有效。"""
    if not text:
        return False
    return bool(CAPTCHA_RE.fullmatch(str(text).strip()))


def normalize_ocr_text(text: str) -> str:
    """清洗 OCR 输出：只保留数字。"""
    if not text:
        return ""
    # 常见 OCR 误识别字符映射
    table = str.maketrans(
        {
            "O": "0",
            "o": "0",
            "I": "1",
            "l": "1",
            "L": "1",
            "Z": "2",
            "z": "2",
            "S": "5",
            "s": "5",
            "B": "8",
            "g": "9",
            "q": "9",
        }
    )
    cleaned = text.translate(table)
    return re.sub(r"\D", "", cleaned)


class CaptchaOCR:
    """ddddocr 封装；识别失败时返回空串。"""

    def __init__(self) -> None:
        self._ocr = None
        self._init_error: str | None = None
        try:
            import ddddocr  # type: ignore

            # show_ad=False 关闭广告输出
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        except Exception as e:  # noqa: BLE001
            self._init_error = str(e)

    @property
    def available(self) -> bool:
        return self._ocr is not None

    def recognize(self, image_path: str | Path | bytes) -> str:
        if not self._ocr:
            raise RuntimeError(
                f"ddddocr 不可用: {self._init_error or '未安装'}。"
                "请执行: pip install ddddocr"
            )
        if isinstance(image_path, (bytes, bytearray)):
            raw = bytes(image_path)
        else:
            raw = Path(image_path).read_bytes()
        try:
            text = self._ocr.classification(raw)
        except Exception as e:  # noqa: BLE001
            print(f"[!] OCR 异常: {e}", file=sys.stderr)
            return ""
        return normalize_ocr_text(str(text or ""))


class ShopGPTLogin:
    def __init__(self, base_url: str = BASE_URL, user_agent: str = DEFAULT_UA) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.user_agent = user_agent
        self._ocr: CaptchaOCR | None = None

    def _headers(
        self,
        extra: dict[str, str] | None = None,
        *,
        referer: str | None = None,
    ) -> dict[str, str]:
        h = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
            "Referer": referer or f"{self.base_url}/user/authentication/login",
        }
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        req = Request(url, data=data, headers=headers or self._headers(), method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                body = resp.read()
                status = getattr(resp, "status", 200) or 200
                resp_headers = {k: v for k, v in resp.headers.items()}
                return status, body, resp_headers
        except HTTPError as e:
            body = e.read() if e.fp else b""
            return e.code, body, dict(e.headers.items()) if e.headers else {}

    def init_session(self) -> None:
        """访问登录页，建立服务端会话。"""
        status, _, _ = self._request(f"{self.base_url}/user/authentication/login")
        if status != 200:
            raise RuntimeError(f"打开登录页失败: HTTP {status}")

    def fetch_captcha(self, save_path: str | Path = "captcha.png") -> Path:
        """
        拉取图形验证码并保存。
        验证码与当前 ACG-SHOP 会话绑定，登录前必须用同一次会话的 captcha。
        """
        url = f"{self.base_url}/user/captcha/image?action=login&t={int(time.time() * 1000)}"
        status, body, headers = self._request(url)
        if status != 200:
            raise RuntimeError(f"获取验证码失败: HTTP {status}")

        ctype = headers.get("Content-Type", "")
        if "image" not in ctype and not body.startswith(b"\x89PNG"):
            raise RuntimeError(f"验证码响应异常 Content-Type={ctype!r} body={body[:200]!r}")

        path = Path(save_path)
        path.write_bytes(body)
        return path

    def get_ocr(self) -> CaptchaOCR:
        if self._ocr is None:
            self._ocr = CaptchaOCR()
        return self._ocr

    def fetch_valid_captcha(
        self,
        save_path: str | Path = "captcha.png",
        *,
        max_retries: int = 20,
        ocr: CaptchaOCR | None = None,
        recognize: Callable[[Path], str] | None = None,
    ) -> tuple[Path, str]:
        """
        拉取验证码并识别；若识别结果不是 4 位数字则重新拉取。

        Returns:
            (captcha_path, captcha_text)
        """
        engine = ocr or self.get_ocr()
        last_text = ""

        for attempt in range(1, max_retries + 1):
            path = self.fetch_captcha(save_path)
            if recognize is not None:
                text = normalize_ocr_text(recognize(path))
            else:
                if not engine.available:
                    raise RuntimeError(
                        f"ddddocr 不可用: {engine._init_error or '未安装'}。"
                        "请执行: pip install ddddocr  或使用 -c 手动输入验证码"
                    )
                text = engine.recognize(path)

            last_text = text
            if is_valid_captcha(text):
                print(f"[*] 验证码识别成功 (第 {attempt} 次): {text}")
                return path, text

            print(
                f"[*] 识别结果无效 (第 {attempt}/{max_retries}): "
                f"{text!r} —— 不是 {CAPTCHA_DIGITS} 位数字，重新拉取…",
                file=sys.stderr,
            )
            time.sleep(0.15)

        raise RuntimeError(
            f"连续 {max_retries} 次未能识别出 {CAPTCHA_DIGITS} 位数字验证码，"
            f"最后一次结果: {last_text!r}"
        )

    def login(
        self,
        username: str,
        password: str,
        captcha: str,
        *,
        remember: bool = True,
    ) -> dict[str, Any]:
        """
        提交登录。

        前端等价代码:
          util.post("/user/api/authentication/login", {
            username, password, captcha, remember?: "1"
          })
        """
        form: dict[str, str] = {
            "username": username,
            "password": password,
            "captcha": captcha.strip(),
        }
        if remember:
            form["remember"] = "1"

        body = urlencode(form).encode("utf-8")
        headers = self._headers(
            {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
        )
        status, raw, _ = self._request(
            f"{self.base_url}/user/api/authentication/login",
            method="POST",
            data=body,
            headers=headers,
        )
        text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise RuntimeError(f"登录请求失败: HTTP {status} body={text[:500]}")

        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"登录响应不是 JSON: {text[:500]}") from e

        return result

    def login_with_auto_captcha(
        self,
        username: str,
        password: str,
        *,
        remember: bool = True,
        captcha_file: str | Path = "captcha.png",
        max_captcha_retries: int = 20,
        max_login_retries: int = 5,
    ) -> dict[str, Any]:
        """
        自动识别 4 位数字验证码并登录。
        - 识别结果不是 4 位数字 → 重新拉验证码
        - 接口返回验证码错误 → 重新拉验证码再试
        """
        last: dict[str, Any] = {}
        for attempt in range(1, max_login_retries + 1):
            _, captcha = self.fetch_valid_captcha(
                captcha_file,
                max_retries=max_captcha_retries,
            )
            print(f"[*] 提交登录 (第 {attempt}/{max_login_retries}) captcha={captcha}…")
            last = self.login(username, password, captcha, remember=remember)
            code = last.get("code")
            msg = str(last.get("msg", ""))
            print(f"[*] 接口返回: code={code} msg={msg}")

            if code == 200:
                return last

            # 验证码错误 → 换一张继续；其他错误（密码错等）直接返回
            if "验证码" in msg:
                print("[*] 验证码错误，重新拉取…", file=sys.stderr)
                continue
            return last

        return last

    def claim(self, benefit_id: int | str = DEFAULT_BENEFIT_ID) -> dict[str, Any]:
        """
        核心用户签到 / 领取奖励。

        对应 curl:
          POST /user/api/coreuser/claim
          Content-Type: application/x-www-form-urlencoded
          body: benefit_id=1
          Cookie: ACG-SHOP=...; USER_SESSION=...
        """
        body = urlencode({"benefit_id": str(benefit_id)}).encode("utf-8")
        headers = self._headers(
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
            },
            referer=f"{self.base_url}/user/coreuser/index",
        )
        status, raw, _ = self._request(
            f"{self.base_url}/user/api/coreuser/claim",
            method="POST",
            data=body,
            headers=headers,
        )
        text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise RuntimeError(f"签到请求失败: HTTP {status} body={text[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"签到响应不是 JSON: {text[:500]}") from e

    def coreuser_info(self) -> dict[str, Any]:
        """查询核心用户状态（可选，用于签到前确认资格）。"""
        headers = self._headers(
            {"Accept": "application/json, text/javascript, */*; q=0.01"},
            referer=f"{self.base_url}/user/coreuser/index",
        )
        status, raw, _ = self._request(
            f"{self.base_url}/user/api/coreuser/info",
            headers=headers,
        )
        text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise RuntimeError(f"查询核心用户失败: HTTP {status} body={text[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"核心用户响应不是 JSON: {text[:500]}") from e

    def set_cookies(self, cookies: dict[str, str], *, domain: str | None = None) -> None:
        """写入 cookie 到当前会话（用于加载本地 cookie 后签到）。"""
        host = (domain or self.base_url.replace("https://", "").replace("http://", "")).split("/")[0]
        for name, value in cookies.items():
            if not name or value is None:
                continue
            self.cookie_jar.set_cookie(
                Cookie(
                    version=0,
                    name=str(name),
                    value=str(value),
                    port=None,
                    port_specified=False,
                    domain=host,
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=self.base_url.startswith("https"),
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )

    def load_cookies_json(self, path: str | Path) -> dict[str, str]:
        """从 shopgpt_cookies.json 加载 cookie。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cookies = data.get("cookies") or {}
        if not cookies and data.get("cookie_header"):
            cookies = {}
            for part in str(data["cookie_header"]).split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        if not cookies:
            raise RuntimeError(f"cookie 文件无有效 cookies: {path}")
        self.set_cookies(cookies)
        if data.get("base_url"):
            self.base_url = str(data["base_url"]).rstrip("/")
        return self.cookies_dict()

    def cookies_dict(self) -> dict[str, str]:
        return {c.name: c.value for c in self.cookie_jar}

    def cookie_header(self) -> str:
        """拼成请求头 Cookie: 可用的字符串。"""
        return "; ".join(f"{k}={v}" for k, v in self.cookies_dict().items())

    def save_cookies_json(self, path: str | Path) -> None:
        data = {
            "cookies": self.cookies_dict(),
            "cookie_header": self.cookie_header(),
            "base_url": self.base_url,
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_cookies_netscape(self, path: str | Path) -> None:
        """Netscape / Mozilla cookie 文件，可供 curl --cookie 使用。"""
        jar = MozillaCookieJar(str(path))
        for c in self.cookie_jar:
            jar.set_cookie(c)
        jar.save(ignore_discard=True, ignore_expires=True)

    def ensure_login(
        self,
        username: str,
        password: str,
        *,
        cookie_file: str | Path | None = DEFAULT_COOKIE_FILE,
        force: bool = False,
        **login_kwargs: Any,
    ) -> dict[str, Any]:
        """
        优先复用本地 cookie；失效或 force=True 时重新登录。
        返回 {"from": "cookie"|"login", "login_result": ...|None}
        """
        if not force and cookie_file and Path(cookie_file).is_file():
            try:
                self.load_cookies_json(cookie_file)
                info = self.coreuser_info()
                if info.get("code") == 200:
                    print(f"[*] 复用本地 cookie 有效: {cookie_file}")
                    return {"from": "cookie", "login_result": None, "info": info}
                print(f"[*] 本地 cookie 无效 (code={info.get('code')})，重新登录…")
            except Exception as e:  # noqa: BLE001
                print(f"[*] 本地 cookie 不可用 ({e})，重新登录…")

        self.cookie_jar.clear()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.init_session()
        result = self.login_with_auto_captcha(username, password, **login_kwargs)
        if result.get("code") != 200:
            raise RuntimeError(f"登录失败: {result}")
        if cookie_file:
            self.save_cookies_json(cookie_file)
            print(f"[+] cookie 已保存: {Path(cookie_file).resolve()}")
        return {"from": "login", "login_result": result, "info": None}


def format_claim_result(result: dict[str, Any]) -> str:
    """把签到接口结果格式化成可读文案。"""
    code = result.get("code")
    msg = result.get("msg", "")
    data = result.get("data") or {}
    if code == 200:
        balance = data.get("balance")
        typ = data.get("type")
        extra = ""
        if balance is not None:
            extra = f"，到账余额相关: {balance}"
        if typ:
            extra = f"，类型={typ}" + (f"，数值={balance}" if balance is not None else "")
        return f"签到成功: {msg}{extra}"
    return f"签到失败: code={code} msg={msg} data={data}"


def _open_image(path: Path) -> None:
    """尽量在系统里打开验证码图片，方便人工输入。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


def _is_already_claimed(result: dict[str, Any]) -> bool:
    """本周期已领过也算签到流程成功（方便 cron）。"""
    msg = str(result.get("msg", ""))
    return any(
        key in msg
        for key in (
            "已达上限",
            "已领取",
            "已经领取",
            "重复领取",
            "今日已",
            "已签到",
        )
    )


def _do_claim(client: ShopGPTLogin, benefit_id: int) -> int:
    """执行签到并打印结果。返回进程退出码。"""
    print(f"[*] 签到领取奖励 benefit_id={benefit_id}…")
    try:
        # 先访问核心用户页，贴近浏览器行为
        client._request(
            f"{client.base_url}/user/coreuser/index",
            headers=client._headers(referer=f"{client.base_url}/"),
        )
        result = client.claim(benefit_id=benefit_id)
    except (URLError, RuntimeError) as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[+] {format_claim_result(result)}")
    if result.get("code") == 200:
        return 0
    if _is_already_claimed(result):
        print("[*] 本周期已领取过，视为签到完成")
        return 0
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ShopGPT 登录 / 签到（coreuser claim）",
    )
    parser.add_argument("-u", "--username", default=None, help="用户名 / 手机号 / 邮箱")
    parser.add_argument("-p", "--password", default=None, help="密码")
    parser.add_argument("-c", "--captcha", default=None, help="图形验证码（手动指定则跳过 OCR）")
    parser.add_argument(
        "--auto",
        action="store_true",
        default=True,
        help="自动 OCR 识别验证码（默认开启；结果非 4 位数字会重拉）",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="手动输入验证码（关闭自动 OCR）",
    )
    parser.add_argument(
        "--captcha-file",
        default="captcha.png",
        help="验证码图片保存路径 (默认 captcha.png)",
    )
    parser.add_argument(
        "--max-captcha-retries",
        type=int,
        default=20,
        help="识别结果非 4 位数字时最多重拉次数 (默认 20)",
    )
    parser.add_argument(
        "--max-login-retries",
        type=int,
        default=5,
        help="验证码错误导致登录失败时最多重试次数 (默认 5)",
    )
    parser.add_argument(
        "--no-remember",
        action="store_true",
        help="不勾选「保持会话」",
    )
    parser.add_argument(
        "--cookie-file",
        default=DEFAULT_COOKIE_FILE,
        help=f"JSON cookie 路径 (默认 {DEFAULT_COOKIE_FILE})",
    )
    parser.add_argument(
        "--netscape",
        default=None,
        help="同时导出 Netscape cookie 文件路径（可选，如 cookies.txt）",
    )
    parser.add_argument(
        "--claim",
        action="store_true",
        help="登录成功后立刻签到领取奖励",
    )
    parser.add_argument(
        "--claim-only",
        action="store_true",
        help="仅签到：使用 --cookie-file 中的 cookie，不重新登录",
    )
    parser.add_argument(
        "--benefit-id",
        type=int,
        default=DEFAULT_BENEFIT_ID,
        help=f"签到 benefit_id (默认 {DEFAULT_BENEFIT_ID})",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="忽略本地 cookie，强制重新登录",
    )
    parser.add_argument("--no-open", action="store_true", help="不自动打开验证码图片")
    parser.add_argument("--base-url", default=BASE_URL, help="站点根地址")
    args = parser.parse_args(argv)

    client = ShopGPTLogin(base_url=args.base_url)

    # ---------- 仅签到 ----------
    if args.claim_only:
        cookie_path = Path(args.cookie_file)
        if not cookie_path.is_file():
            print(f"[!] cookie 文件不存在: {cookie_path}", file=sys.stderr)
            print("    请先登录: python3 shopgpt_login.py -u 账号 -p 密码", file=sys.stderr)
            return 1
        try:
            client.load_cookies_json(cookie_path)
        except Exception as e:  # noqa: BLE001
            print(f"[!] 加载 cookie 失败: {e}", file=sys.stderr)
            return 1
        print(f"[*] 已加载 cookie: {cookie_path.resolve()}")
        print(f"[*] cookies: {list(client.cookies_dict())}")
        return _do_claim(client, args.benefit_id)

    # ---------- 登录（可选再签到） ----------
    if not args.username or not args.password:
        print("[!] 登录需要 -u/--username 与 -p/--password", file=sys.stderr)
        print("    若只想签到: --claim-only --cookie-file shopgpt_cookies.json", file=sys.stderr)
        return 1

    use_auto = not args.manual and args.captcha is None
    remember = not args.no_remember

    # 登录后要签到时：可复用 cookie，减少验证码
    if args.claim and not args.force_login and not args.captcha and use_auto:
        try:
            state = client.ensure_login(
                args.username,
                args.password,
                cookie_file=args.cookie_file,
                force=args.force_login,
                remember=remember,
                captcha_file=args.captcha_file,
                max_captcha_retries=args.max_captcha_retries,
                max_login_retries=args.max_login_retries,
            )
            print(f"[+] 登录态就绪 (from={state['from']})")
            if args.netscape:
                client.save_cookies_netscape(args.netscape)
            return _do_claim(client, args.benefit_id)
        except (URLError, RuntimeError) as e:
            print(f"[!] {e}", file=sys.stderr)
            return 1

    print("[*] 初始化会话…")
    try:
        client.init_session()
    except (URLError, RuntimeError) as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1

    try:
        if args.captcha is not None:
            captcha = normalize_ocr_text(args.captcha)
            if not is_valid_captcha(captcha):
                print(
                    f"[!] 验证码必须是 {CAPTCHA_DIGITS} 位数字，当前: {args.captcha!r}",
                    file=sys.stderr,
                )
                return 1
            print("[*] 获取验证码图片…")
            captcha_path = client.fetch_captcha(args.captcha_file)
            print(f"[*] 验证码已保存: {captcha_path.resolve()} (使用手动指定: {captcha})")
            result = client.login(
                args.username, args.password, captcha, remember=remember
            )
            print(f"[*] 接口返回: code={result.get('code')} msg={result.get('msg', '')}")
        elif use_auto:
            print("[*] 自动识别 4 位数字验证码（非 4 位会重新拉取）…")
            result = client.login_with_auto_captcha(
                args.username,
                args.password,
                remember=remember,
                captcha_file=args.captcha_file,
                max_captcha_retries=args.max_captcha_retries,
                max_login_retries=args.max_login_retries,
            )
        else:
            print("[*] 获取验证码…")
            captcha_path = client.fetch_captcha(args.captcha_file)
            print(f"[*] 验证码已保存: {captcha_path.resolve()}")
            print(f"[*] 当前会话 cookie: {client.cookies_dict()}")
            if not args.no_open:
                _open_image(captcha_path)
            captcha = input("请输入 4 位数字验证码: ").strip()
            captcha = normalize_ocr_text(captcha)
            if not is_valid_captcha(captcha):
                print(
                    f"[!] 验证码必须是 {CAPTCHA_DIGITS} 位数字，当前: {captcha!r}",
                    file=sys.stderr,
                )
                return 1
            print("[*] 提交登录…")
            result = client.login(
                args.username, args.password, captcha, remember=remember
            )
            print(f"[*] 接口返回: code={result.get('code')} msg={result.get('msg', '')}")
    except (URLError, RuntimeError) as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1

    code = result.get("code")
    if code != 200:
        print("[!] 登录失败（常见原因: 验证码错误 / 账号密码错误）", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    cookies = client.cookies_dict()
    cookie_header = client.cookie_header()
    client.save_cookies_json(args.cookie_file)
    print("[+] 登录成功")
    print(f"[+] cookies dict : {cookies}")
    print(f"[+] Cookie header: {cookie_header}")
    print(f"[+] 已写入 JSON  : {Path(args.cookie_file).resolve()}")

    if args.netscape:
        client.save_cookies_netscape(args.netscape)
        print(f"[+] Netscape 文件: {Path(args.netscape).resolve()}")

    if args.claim:
        return _do_claim(client, args.benefit_id)

    print()
    print("# 一键签到（使用已保存 cookie）:")
    print(f"# python3 shopgpt_login.py --claim-only --cookie-file {args.cookie_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
