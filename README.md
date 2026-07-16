# ShopGPT Auto Sign

ShopGPT（[shopgpt.plus](https://shopgpt.plus)）**多账号自动登录 + 核心用户签到领奖** 脚本。

适合每天定时领取 `coreuser` 福利（`benefit_id=1`），支持：

- 自动过 **4 位数字图形验证码**（OCR，非 4 位会重拉）
- **多账号** JSON 配置批量签到
- Cookie 缓存，减少重复登录
- macOS / Linux `cron` 定时任务

> 仅供学习与个人账号自动化使用，请遵守目标站点服务条款。

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 登录 | 逆向网页登录流程，自动识别验证码并获取 `ACG-SHOP` / `USER_SESSION` |
| 签到 | `POST /user/api/coreuser/claim`，默认 `benefit_id=1` |
| 多账号 | `accounts.json` 列表批量处理 |
| 容错 | 验证码识别失败重试；本周期已领取视为成功（方便 cron） |
| 定时 | 提供 `run_sign.sh`，可挂到每天 00:05 |

---

## 目录结构

```text
auto_sign_gpt/
├── shopgpt_login.py      # 登录 / 验证码 / 签到核心库
├── sign.py               # 多账号一键签到入口
├── run_sign.sh           # cron 友好启动脚本（写日志）
├── accounts.example.json # 账号配置模板（可提交）
├── accounts.json         # 真实账号（本地创建，勿提交）
├── cookies/              # 各账号 cookie 缓存（勿提交）
├── logs/                 # 定时任务日志（勿提交）
└── requirements.txt
```

敏感文件已在 `.gitignore` 中排除：`accounts.json`、`cookies/`、`logs/`、验证码图片等。

---

## 环境要求

- Python 3.10+
- macOS / Linux
- 依赖：`ddddocr`（验证码 OCR）

```bash
git clone <your-repo-url>
cd auto_sign_gpt

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 快速开始

### 1. 配置账号

复制模板并填写自己的账号密码（**不要把真实密码提交到 Git**）：

```bash
cp accounts.example.json accounts.json
chmod 600 accounts.json
```

`accounts.json` 示例：

```json
{
  "benefit_id": 1,
  "base_url": "https://shopgpt.plus",
  "accounts": [
    {
      "username": "your_username_1",
      "password": "your_password_1",
      "enabled": true,
      "note": "主号"
    },
    {
      "username": "your_username_2",
      "password": "your_password_2",
      "enabled": true,
      "note": "小号"
    },
    {
      "username": "paused_account",
      "password": "xxx",
      "enabled": false,
      "note": "暂时跳过"
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `username` | 是 | 用户名 / 手机号 / 邮箱 |
| `password` | 是 | 密码 |
| `enabled` | 否 | 默认 `true`；`false` 跳过 |
| `benefit_id` | 否 | 覆盖全局默认签到福利 ID |
| `note` | 否 | 备注，仅日志展示 |
| `cookie_file` | 否 | 自定义 cookie 路径 |

每个启用账号的 cookie 默认保存在：

```text
cookies/<username>.json
```

### 2. 一键多账号签到

```bash
.venv/bin/python sign.py
# 或
./run_sign.sh
```

### 3. 单账号

```bash
.venv/bin/python sign.py -u 用户名 -p 密码
```

### 4. 仅登录 / 仅签到

```bash
# 登录并导出 cookie
.venv/bin/python shopgpt_login.py -u 用户名 -p 密码

# 登录后立刻签到
.venv/bin/python shopgpt_login.py -u 用户名 -p 密码 --claim

# 使用已有 cookie 签到
.venv/bin/python shopgpt_login.py --claim-only --cookie-file cookies/your_username.json
```

---

## 定时任务（每天 00:05）

```bash
crontab -e
```

加入：

```cron
5 0 * * * /bin/zsh /绝对路径/auto_sign_gpt/run_sign.sh
```

日志输出到：

```text
logs/sign-YYYYMMDD.log
```

查看任务：

```bash
crontab -l
tail -f logs/sign-$(date +%Y%m%d).log
```

### macOS 注意

1. 机器需在 00:05 **开机或唤醒**，深度睡眠可能错过 cron。
2. 若任务不触发，给 **cron / 终端** 开启「完全磁盘访问权限」。
3. 建议先手动跑一次 `./run_sign.sh`，确认 `logs/` 有输出。

---

## 原理说明（简要）

### 登录

前端 `login.js` 提交：

```http
POST /user/api/authentication/login
Content-Type: application/x-www-form-urlencoded

username=...&password=...&captcha=1234&remember=1
```

成功：`{"code":200,"msg":"登录成功",...}`  
会话 Cookie：`ACG-SHOP`、`USER_SESSION`

图形验证码：

```http
GET /user/captcha/image?action=login
```

站点验证码为 **4 位数字**。OCR 结果若不是 4 位数字会自动重新拉取。

### 签到

```http
POST /user/api/coreuser/claim
Content-Type: application/x-www-form-urlencoded
Cookie: ACG-SHOP=...; USER_SESSION=...

benefit_id=1
```

成功示例：

```json
{
  "code": 200,
  "msg": "领取成功",
  "data": {
    "type": "balance",
    "balance": 1.88
  }
}
```

若返回「本周期领取次数已达上限」等，脚本视为今日已完成，退出码 0，避免 cron 误报失败。

---

## 常用命令

```bash
# 强制所有账号重新登录后再签到
.venv/bin/python sign.py --force-login

# 指定配置文件
.venv/bin/python sign.py --accounts /path/to/accounts.json

# 环境变量单账号
export SHOPGPT_USER=your_user
export SHOPGPT_PASS=your_pass
.venv/bin/python sign.py
```

---

## 安全建议

1. **永远不要**把 `accounts.json`、`cookies/` 推到公开仓库。
2. 本地建议：`chmod 600 accounts.json`。
3. 在不可信环境不要明文保存密码；可用本机权限收紧的私有仓库或密钥管理。
4. 密码若曾出现在聊天/截图中，建议尽快修改。

---

## 免责声明

本项目仅用于个人学习与自有账号运维自动化。使用造成的任何账号风险、服务限制或损失，由使用者自行承担。请勿用于未授权访问或批量滥用。

---

## License

MIT（如需更严格协议可自行替换）
