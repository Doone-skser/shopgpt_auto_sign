#!/usr/bin/env python3
"""
ShopGPT 一键 / 多账号签到

优先使用本地 cookie；失效时用 accounts.json 里的账号密码自动登录再签到。

用法:
  # 多账号（默认读 accounts.json）
  .venv/bin/python sign.py
  .venv/bin/python sign.py --accounts accounts.json

  # 单账号
  .venv/bin/python sign.py -u 账号 -p 密码

  # 强制全部重新登录
  .venv/bin/python sign.py --force-login
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from shopgpt_login import (
    DEFAULT_BENEFIT_ID,
    DEFAULT_COOKIE_FILE,
    ShopGPTLogin,
    _do_claim,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_ACCOUNTS_FILE = ROOT / "accounts.json"
DEFAULT_COOKIES_DIR = ROOT / "cookies"


def _safe_name(username: str) -> str:
    """用户名转成 cookie 文件名安全片段。"""
    name = re.sub(r"[^\w.\-@]+", "_", username.strip())
    return name or "user"


def load_accounts_config(path: Path) -> dict[str, Any]:
    """
    读取账号配置，支持两种格式:

    1) 对象:
       {"benefit_id": 1, "accounts": [{"username","password",...}, ...]}
    2) 纯数组:
       [{"username","password"}, ...]
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {"benefit_id": DEFAULT_BENEFIT_ID, "accounts": raw}
    if isinstance(raw, dict):
        accounts = raw.get("accounts")
        if accounts is None and raw.get("username"):
            accounts = [raw]
        if not isinstance(accounts, list):
            raise ValueError("accounts.json 需要 accounts 数组，或直接是账号对象数组")
        return raw
    raise ValueError("accounts.json 格式无效")


def normalize_account(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"accounts[{index}] 必须是对象")
    username = str(item.get("username") or item.get("user") or "").strip()
    password = str(item.get("password") or item.get("pass") or "")
    if not username or not password:
        raise ValueError(f"accounts[{index}] 缺少 username/password")
    enabled = item.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"0", "false", "no", "off"}
    return {
        "username": username,
        "password": password,
        "enabled": bool(enabled),
        "benefit_id": item.get("benefit_id"),
        "note": item.get("note") or item.get("name") or "",
        "cookie_file": item.get("cookie_file"),
        "force_login": bool(item.get("force_login", False)),
    }


def cookie_path_for(account: dict[str, Any], cookies_dir: Path) -> Path:
    if account.get("cookie_file"):
        p = Path(account["cookie_file"])
        return p if p.is_absolute() else ROOT / p
    return cookies_dir / f"{_safe_name(account['username'])}.json"


def sign_one(
    account: dict[str, Any],
    *,
    default_benefit_id: int,
    base_url: str | None,
    cookies_dir: Path,
    force_login: bool = False,
) -> dict[str, Any]:
    """签到单个账号，返回结果摘要。"""
    username = account["username"]
    benefit_id = int(account.get("benefit_id") or default_benefit_id or DEFAULT_BENEFIT_ID)
    cookie_path = cookie_path_for(account, cookies_dir)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)

    client = ShopGPTLogin(base_url=base_url) if base_url else ShopGPTLogin()
    label = f"{username}" + (f" ({account['note']})" if account.get("note") else "")

    print()
    print("=" * 60)
    print(f"[*] 账号: {label}")
    print(f"[*] cookie: {cookie_path}")
    print(f"[*] benefit_id: {benefit_id}")

    need_login = force_login or account.get("force_login")

    if not need_login and cookie_path.is_file():
        try:
            client.load_cookies_json(cookie_path)
            info = client.coreuser_info()
            if info.get("code") == 200:
                data = info.get("data") or {}
                print(
                    f"[*] cookie 有效 | active={data.get('active')} "
                    f"expire={data.get('expire_time')} status={data.get('status')}"
                )
                code = _do_claim(client, benefit_id)
                return {
                    "username": username,
                    "ok": code == 0,
                    "from": "cookie",
                    "exit_code": code,
                }
            print(f"[*] cookie 失效: {info.get('msg') or info}")
        except Exception as e:  # noqa: BLE001
            print(f"[*] cookie 不可用: {e}")

    print("[*] 登录中…")
    try:
        state = client.ensure_login(
            username,
            account["password"],
            cookie_file=cookie_path,
            force=True,
        )
        print(f"[+] 登录完成 from={state['from']}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 登录失败: {e}", file=sys.stderr)
        return {
            "username": username,
            "ok": False,
            "from": "login_error",
            "error": str(e),
            "exit_code": 1,
        }

    code = _do_claim(client, benefit_id)
    return {
        "username": username,
        "ok": code == 0,
        "from": "login",
        "exit_code": code,
    }


def sign_accounts(
    accounts_file: Path,
    *,
    force_login: bool = False,
    cookies_dir: Path = DEFAULT_COOKIES_DIR,
) -> int:
    if not accounts_file.is_file():
        print(f"[!] 账号文件不存在: {accounts_file}", file=sys.stderr)
        print(f"    请复制模板: cp accounts.example.json {accounts_file.name}", file=sys.stderr)
        return 1

    cfg = load_accounts_config(accounts_file)
    default_benefit_id = int(cfg.get("benefit_id") or DEFAULT_BENEFIT_ID)
    base_url = cfg.get("base_url") or None
    raw_accounts = cfg.get("accounts") or []

    accounts: list[dict[str, Any]] = []
    for i, item in enumerate(raw_accounts):
        try:
            accounts.append(normalize_account(item, i))
        except ValueError as e:
            print(f"[!] 跳过无效账号: {e}", file=sys.stderr)

    enabled = [a for a in accounts if a["enabled"]]
    skipped = len(accounts) - len(enabled)

    print(f"[*] 账号配置: {accounts_file}")
    print(f"[*] 启用 {len(enabled)} 个账号" + (f"，跳过 {skipped} 个" if skipped else ""))
    print(f"[*] 默认 benefit_id={default_benefit_id}")
    print(f"[*] 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not enabled:
        print("[!] 没有启用的账号", file=sys.stderr)
        return 1

    cookies_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for idx, account in enumerate(enabled, 1):
        print(f"\n>>> [{idx}/{len(enabled)}]")
        try:
            results.append(
                sign_one(
                    account,
                    default_benefit_id=default_benefit_id,
                    base_url=base_url,
                    cookies_dir=cookies_dir,
                    force_login=force_login,
                )
            )
        except Exception as e:  # noqa: BLE001
            print(f"[!] 账号异常 {account['username']}: {e}", file=sys.stderr)
            traceback.print_exc()
            results.append(
                {
                    "username": account["username"],
                    "ok": False,
                    "from": "exception",
                    "error": str(e),
                    "exit_code": 1,
                }
            )
        # 账号之间稍作间隔，降低风控概率
        if idx < len(enabled):
            time.sleep(1.0)

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n

    print()
    print("=" * 60)
    print(f"[*] 完成: 成功 {ok_n} / 失败 {fail_n} / 共 {len(results)}")
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        extra = r.get("error") or r.get("from") or ""
        print(f"  - [{status}] {r.get('username')} {extra}")
    print(f"[*] 结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return 0 if fail_n == 0 else 2


def sign_single(
    username: str,
    password: str,
    *,
    cookie_file: Path,
    benefit_id: int,
    force_login: bool,
    base_url: str | None,
) -> int:
    account = {
        "username": username,
        "password": password,
        "enabled": True,
        "benefit_id": benefit_id,
        "cookie_file": str(cookie_file),
        "note": "",
    }
    result = sign_one(
        account,
        default_benefit_id=benefit_id,
        base_url=base_url,
        cookies_dir=cookie_file.parent,
        force_login=force_login,
    )
    return 0 if result.get("ok") else int(result.get("exit_code") or 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShopGPT 多账号一键签到")
    parser.add_argument("-u", "--username", default=os.environ.get("SHOPGPT_USER"))
    parser.add_argument("-p", "--password", default=os.environ.get("SHOPGPT_PASS"))
    parser.add_argument(
        "--accounts",
        default=str(DEFAULT_ACCOUNTS_FILE),
        help=f"多账号 JSON 路径 (默认 {DEFAULT_ACCOUNTS_FILE.name})",
    )
    parser.add_argument(
        "--cookie-file",
        default=None,
        help="单账号模式下 cookie 路径 (默认 cookies/<用户名>.json)",
    )
    parser.add_argument(
        "--cookies-dir",
        default=str(DEFAULT_COOKIES_DIR),
        help="多账号 cookie 目录 (默认 cookies/)",
    )
    parser.add_argument("--benefit-id", type=int, default=DEFAULT_BENEFIT_ID)
    parser.add_argument("--force-login", action="store_true", help="强制重新登录")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)

    # 单账号优先
    if args.username and args.password:
        cookie = (
            Path(args.cookie_file)
            if args.cookie_file
            else Path(args.cookies_dir) / f"{_safe_name(args.username)}.json"
        )
        if not cookie.is_absolute():
            cookie = ROOT / cookie
        return sign_single(
            args.username,
            args.password,
            cookie_file=cookie,
            benefit_id=args.benefit_id,
            force_login=args.force_login,
            base_url=args.base_url,
        )

    return sign_accounts(
        Path(args.accounts),
        force_login=args.force_login,
        cookies_dir=Path(args.cookies_dir),
    )


if __name__ == "__main__":
    raise SystemExit(main())
