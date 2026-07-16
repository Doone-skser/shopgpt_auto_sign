# ShopGPT Auto Sign

ShopGPT 多账号自动签到脚本：登录后领取核心用户每日奖励，支持定时任务。

## 功能

- 多账号批量签到
- 自动识别登录验证码
- Cookie 缓存，减少重复登录
- 支持 cron 定时执行

## 安装

```bash
git clone https://github.com/Doone-skser/shopgpt_auto_sign.git
cd shopgpt_auto_sign

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置账号

```bash
cp accounts.example.json accounts.json
```

编辑 `accounts.json`：

```json
{
  "benefit_id": 1,
  "accounts": [
    {
      "username": "账号1",
      "password": "密码1",
      "enabled": true,
      "note": "主号"
    },
    {
      "username": "账号2",
      "password": "密码2",
      "enabled": true
    }
  ]
}
```

`enabled: false` 可跳过某个账号。真实账号文件不要提交到 Git。

## 使用

```bash
# 多账号一键签到
.venv/bin/python sign.py

# 或
./run_sign.sh

# 单账号
.venv/bin/python sign.py -u 用户名 -p 密码

# 强制重新登录后再签到
.venv/bin/python sign.py --force-login
```

## 定时任务

每天 00:05 自动签到：

```bash
crontab -e
```

写入：

```cron
5 0 * * * /bin/zsh /绝对路径/shopgpt_auto_sign/run_sign.sh
```

日志在 `logs/sign-YYYYMMDD.log`。

macOS 需保证机器在该时间点开机/唤醒；若任务不跑，检查 cron 的完全磁盘访问权限。

## 说明

仅供个人账号使用，请遵守站点服务条款。
