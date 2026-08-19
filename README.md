# EWT360 刷课工具 (最终版)

> 2026-08-08 ｜ 基于 hmruu/ewt360、EwtAutoStudyBot、ewt360-reverse-engineering 等
> 8 个开源仓库知识 + 平台接口实测验证开发。

## 📦 文件说明

| 文件 | 用途 |
|---|---|
| `ewt360_final.py` | **唯一入口** (CLI + 字符版交互菜单) |
| `requirements.txt` | 依赖清单 |
| `PRD.md` | 产品需求文档 (实测结论/接口/风控, 后续开发依据) |
| `README.md` | 本文件 |

## 🔧 安装

```bash
pip install -r requirements.txt
```

依赖: `requests` + `pycryptodome`(或纯 Python 的 `pyaes`, 脚本自动探测)。

### 运行时配置

签名和加密材料不保存在仓库中。复制 `.env.example` 为项目目录下的 `.env`，
填入本地配置后直接运行脚本即可自动读取。`.env` 已被 Git 忽略，不要提交到仓库。

## 🚀 使用

### 方式一: 命令行 (推荐)

```bash
# 快速验证未完成课程 (默认模式，提交后复查)
python ewt360_final.py --user 账号 --pass 密码

# 指定模式
python ewt360_final.py --user 账号 --pass 密码 --mode diagnose # 只读检查进度，不提交播放数据
python ewt360_final.py --user 账号 --pass 密码 --mode fast    # 快速验证，未达标会报告
python ewt360_final.py --user 账号 --pass 密码 --mode bfe     # 真实计时
python ewt360_final.py --user 账号 --pass 密码 --mode quick   # 已失效, 仅参考

# 使用已有 token / 只刷指定课程
python ewt360_final.py --token <TOKEN> --mode fast --homework-id 10517977 --lesson-ids 117980,65169
```

### 方式二: 交互菜单

### 方式三: HTML 前端 + Python 后端

启动 ewt360_web.py，然后打开 http://127.0.0.1:8765。
后端复用 ewt360_final.py，登录信息只保存在进程内存中。

```bash
python ewt360_final.py
```

手机端：手机与电脑连接同一 Wi-Fi，在电脑上运行下面的命令，
再用手机浏览器打开终端输出的局域网地址。默认监听仍只允许本机访问。

```bash
python ewt360_web.py --host 0.0.0.0 --port 8765
```

菜单流程: `1 登录` → `2 扫描课程` → `3 诊断` / `4 快速验证` / `5 BFE`

## 🧠 核心原理 (2026-08 实测确认)

- ❌ `updateUserLessonTaskV2`: 平台已改为**假成功** (返回 success 但进度不涨)
- ✅ **BFE 心跳上报** (`bfe.ewt360.com/monitor/app/collect/batch`) 真实有效:
  - `report_time` 必须用**当前真实时间戳**
  - `begin_time = now - 已看时长`, 与上报时长自洽
  - fast 模式: 每门课最多发一个受限心跳，提交后查询 playTime；仅真实达标才算成功
- 签名: BFE 用 HMAC-SHA1 (密钥从 `getPlayerGlobalConf` 动态获取)
- 登录: AES-CBC 加密密码 + Web 端 MD5 签名

## ⚠️ 风控提醒

| 错误码 | 含义 | 应对 |
|---|---|---|
| 699101 | 环境异常 (伪造/回拨时间戳) | 等 1-2 分钟自动恢复, 切勿回拨时间戳 |
| 699001 | 一心二用 (手机 App 同时在播) | 运行前关闭 App |

平台反作弊持续升级, 本工具仅作学习研究用途, 请合理使用。
