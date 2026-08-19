# EWT360 自动刷课工具 — 产品需求文档 (PRD)

> 版本: v1.0 ｜ 日期: 2026-08-08 ｜ 状态: 已确认事实汇总, 作为后续开发依据

---

## 1. 项目背景与目标

**升学e网通 (ewt360, 域名 ewt360.com)** 的学生假期/新生平台含大量视频课程任务
(语文/数学/英语/物理/化学/选科等, 每门课需观看至 80% 时长记为完成)。

本项目目标是**自动化完成视频课程进度上报**, 支持:

- 单/多账号批量处理
- **快速验证模式**: 单次受限上报并复查真实进度
- 保守模式: 按真实时间间隔模拟观看 (备选)
- 课程扫描、进度查询、结果复查

技术依据: 2026-08-08 对平台接口的**实测验证** + 8 个开源仓库交叉参考。

---

## 2. 已确认事实 (实测证据, 2026-08-08)

### 2.1 进度上报接口有效性

| # | 方法 | 服务器响应 | 进度是否变化 | 结论 |
|---|---|---|---|---|
| 1 | `updateUserLessonTaskV2` (playTime=88888888 / 80% / 100% 时长, 带/不带 header 签名) | HTTP 200, `success:true` | **否** (0→0) | ❌ 假成功, 平台已修复 |
| 2 | `dlog.ewt360.com` 速通 (回拨 begin_time, 0.2s 连发 60000ms 块) | HTTP 200 | **否** | ❌ 假成功 |
| 3 | BFE `monitor/app/collect/batch` + **回拨时间戳**快速连发 | 699101 风控 | 否 | ❌ 触发风控 |
| 4 | BFE + **真实时间戳**单轮上报 (report_time=now, begin_time=now-时长) | HTTP 200, `success:true` | **是** (+60000ms / +120000ms 实测) | ✅ **唯一有效路径** |

**核心结论**: 平台 2026 年更新后, 进度只认 BFE 心跳上报, 且要求
`report_time` 为当前真实时间戳; 伪造/回拨时间戳会被风控拦截。

### 2.2 风控码

| 错误码 | 含义 | 触发条件 | 恢复 |
|---|---|---|---|
| 699101 | 环境异常, 学习数据无法记录 | 回拨/伪造时间戳、快速连发 | 临时性, ~1-2 分钟内自动恢复 (实测) |
| 699001 | 一心二用, 检测到 App 端新播放 | 手机 App 同时在播课程时脚本上报 | 关闭 App 后恢复 |

**规避规则**:
1. 上报必须使用当前真实时间戳
2. `begin_time = now - stay_time`, 与上报时长自洽
3. 运行时关闭手机 App / 其他刷课工具, 避免并发会话
4. 课程间保留 1-2s 间隔, 失败课程延迟重试

### 2.3 课程数据接口结构 (实测抓包)

**场景 → 作业 → 天数 → 课程任务** 链路:

| 接口 | 关键返回/参数 |
|---|---|
| `GET /api/holidayprod/scene/student/study/checkHoliday` | `data.sceneList[]`, `id` 为字符串 (如 "192" 新生假期平台) |
| `GET /api/homeworkprod/homework/student/holiday/getHomeworkSummaryInfo` | `data.homeworkIds[]` (多数场景为空, 假期平台有值) |
| `POST /api/homeworkprod/homework/student/holiday/getHomeworkDistribution` | `data.days[]`: **`day`=整数毫秒时间戳**, **`dayId`=[字符串]** |
| `POST /api/homeworkprod/homework/student/holiday/pageHomeworkTasks` | **`dayId` 必须传 `[int]`** (Java `ArrayList<Long>`, 传字符串会 500); `day`=int; `status`: **0=未完成, 1=已完成**; 返回 `data.data[]` |
| `POST /api/homeworkprod/homework/student/getUserHomeworkLessonTaskInfo` | `playTime`, `percent`, `finishPlayTime`(80%时长), `finishPercent`=0.8, `lessonTime`, `lessonId` |
| `POST /api/homeworkprod/player/getLessonDetailV2` | `playTime`("MM:SS" 需取分钟数+1), `videoPlayTime`, `contentType` |

**任务字段**: `contentId`(lessonId), `parentContentId`(courseId), `homeworkId`,
`title`, `duration`(秒), `ratio`, `finished`, `contentType`(1=视频, 2=试题)。

**处理规则**: contentType=2 试题类跳过 (无法通过进度上报完成)。

### 2.4 密钥与签名体系 (已验证)

| 项 | 值 | 用途 |
|---|---|---|
| Gateway secretId=1 | `EWT360_SECRET_APP` 环境变量 | APP 端 header 签名 `MD5(ts+key).toUpperCase()` |
| Gateway secretId=2 | `EWT360_SECRET_WEB` 环境变量 | Web 端登录签名 `MD5(ts+key).toUpperCase()` |
| AES Key | `EWT360_AES_KEY` 环境变量 (16/24/32B) | 登录密码加密 |
| AES IV | `EWT360_AES_IV` 环境变量 (16B) | 同上, CBC/PKCS7, 输出大写 hex |
| BFE HMAC | `getPlayerGlobalConf` 动态获取 `globalInfo.secret` | BFE 签名, 仅运行时使用 |
| dlog 盐 | `EWT360_BODY_SALT` 环境变量 | 旧接口的 MD5 密钥, **该接口已失效** |

**BFE 签名算法**:
```
sig_str = "action={action}&duration={duration}&mediaTime={mediaTime}"
        + "&mstid={token}&platform=2&signatureMethod=HMAC-SHA1"
        + "&signatureVersion=1.0&timestamp={now_ms}&version=2022-08-02"
signature = HMAC-SHA1(secret, sig_str)  # hex 小写
```

**BFE 请求**:
```
POST https://bfe.ewt360.com/monitor/app/collect/batch
  ?TrLessonId={lessonId}&TrVideoBizCode=2013&TrUuId={uuid}
  &TrFallback=0&TrUserId={userId}&token={token}
Header: token, x-bfe-session-id (来自 getPlayerGlobalConf)
Body: CommonPackage(设备信息, mstid=token) + EventPackage[1条]
  {lesson_id, course_id, stay_time, begin_time, report_time(=now),
   point_time=60000, point_num, speed, quality, action=2, status=1}
```

### 2.5 运行时配置与账号验证环境

- 测试账号信息不写入仓库；运行时通过交互输入或命令行参数提供
- 签名与加密材料不写入仓库；运行前通过 `.env.example` 中列出的环境变量注入

---

## 3. 系统架构

```
workspace/
├── ewt360_final.py   # 唯一入口: CLI + 字符版交互菜单
├── requirements.txt  # requests / pycryptodome
└── PRD.md        # 本文档
```

**模式定义**:

| 模式 | 实现 | 状态 |
|---|---|---|
| `quick` | `updateUserLessonTaskV2` 直接提交 | ⚠️ 已失效, 保留仅作参考 |
| `bfe` | BFE 心跳逐轮上报, 每 60s 真实等待上报 120s (2 倍速) | ✅ 有效但慢 (~10h/87门) |
| `fast` | 每门课最多一个受限 BFE 心跳, 提交后复查 `playTime` | ⚠️ 仅真实达标才算成功, 未达标需转 `bfe` |

---

## 4. 功能需求

| 编号 | 需求 | 优先级 | 说明 |
|---|---|---|---|
| FR-1 | 账号密码登录 (AES-CBC + Web 签名) | P0 | 已实现 |
| FR-2 | 课程扫描 (场景/作业/天数/任务, status 0+1 合并去重, 跳过试题) | P0 | 已实现并修复 |
| FR-3 | **fast 模式**: 受限 BFE 心跳, 失败自动重试, 提交后复查真实进度 | P0 | 已合入，拒绝 HTTP 假成功 |
| FR-4 | bfe 模式: 真实计时逐轮上报 | P1 | 已实现 |
| FR-5 | quick 模式: 保留入口但明确标记失效 | P2 | 已标记 |
| FR-6 | 结果汇总 (已达标/成功/失败) + 复查 | P1 | 已实现 |
| FR-7 | 多账号并发 (Excel 导入) | P2 | 参考 EwtAutoStudyBot |
| FR-8 | 试题答案获取 | P3 | 参考 hmruu/答案获取.py、zhicheng233、ZZ0YY |

---

## 5. 非功能需求

- **依赖**: requests, pycryptodome (或 pyaes)
- **性能**: fast 模式按服务端实际记账结果运行；无法用几分钟可靠完成未观看课程，未达标项转 `bfe`
- **风控规避**: 真实时间戳、begin_time 自洽、关闭 App 并发、失败延迟重试
- **可观测**: 每门课实时输出服务器响应; 结束输出统计与复查结果
- **兼容**: 支持账号密码输入与命令行参数两种方式

---

## 6. 风险与限制

1. **平台反作弊持续升级** (2026.7.30 更新认真度检测/黑名单, 见 luoying2334 v4.3.0):
   纯 API 伪造路径可能再次失效; 终极方案是浏览器驱动真实播放器 + 反检测
2. **风控标记**: 连续异常可能升级账号风控等级, 需控制频率
3. **临时风控**: 699101/699001 为临时态, 可恢复, 但多次触发风险自担
4. 课程数据字段可能随平台版本变化 (如 day/dayId 类型已变过一次)

---

## 7. 开发任务清单 (下一步)

- [x] 将 fast 验证模式合入 `ewt360_final.py` (`--mode fast`)
- [x] 将 diagnose、fast、bfe 整合进字符菜单
- [ ] 在单门测试课程上记录提交前后 `playTime`、完整响应和风控码
- [ ] (P2) 多账号 Excel 并发
- [ ] (P3) 试题答案模块

---

## 8. 参考资料 (开源仓库)

| 仓库 | 要点 |
|---|---|
| hmruu/ewt360 | updateUserLessonTaskV2 body 盐（运行时配置为 `EWT360_BODY_SALT`）; 油猴脚本 |
| 15812642/ewt360-reverse-engineering | 密钥体系/API 全量逆向文档 |
| yangsongh/EwtAutoStudyBot | Web 端 BFE 真实计时逐分钟上报; 多账号 |
| landuoguo/ewt360 | dlog 速通 (2023, 已失效); BFE v3 真实计时 |
| luoying2334/EWT360-NEW-Helper | 2026.7.30 反作弊对抗 (addVideoss/addStudp/699 码) |
| ZNink/EWT360-Helper | 浏览器刷课 UI 自动化 (过检/倍速/连播) |
| ZZ0YY/EWT-TOOL | 试卷答案填写 (reportId 流程) |
| zhicheng233/GetEWTAnswers | 试题答案获取 (reportId 越权) |
