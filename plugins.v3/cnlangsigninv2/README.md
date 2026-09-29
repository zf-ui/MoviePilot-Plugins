# 国语视界签到V3 (CnlangSigninV2)

国语视界（[cnlang.org](https://bbs.cnlang.org/)）自动签到助手，按 MoviePilot **V3**
插件开发规范实现。

- 插件 ID：`CnlangSigninV2`
- 插件目录：`plugins.v3/cnlangsigninv2/`
- 插件版本：`3.6.0`
- 主系统要求：`>=3.0.0`

> **v3.6.0 是一次实现层面的整体重写**：由浏览器驱动方案回归纯 `requests` 实现。
> 浏览器模式、Cloudflare 绕过、账号密码自动登录、验证码识别均已移除，详见下方
> [v3.6.0 变更说明](#v360-变更说明)。

## 功能

| 能力 | 说明 |
| --- | --- |
| 定时签到 | 使用 Cron 表达式配置签到周期，默认每天 07:00 |
| 随机延迟 | 配置 `100-200` 形式的区间，实际执行时刻在区间内随机，降低风控风险 |
| 立即运行一次 | 保存配置后立即触发一次签到，任务交由宿主调度器执行 |
| 系统代理 | 可复用 MoviePilot 配置的系统代理访问站点 |
| 通知样式 | 5 套通知模板（简约 / 清新 / 科技 / 商务 / 优雅），Cookie 失效时给出专门提示 |
| 签到寄语 | 自动从一言接口获取 6~50 字的签到寄语，取不到时使用兜底文案 |
| 历史与统计 | 按天保留签到历史，统计成功率、大洋收益、连续签到天数与最佳签到时段 |
| 远程命令 | `/cnlang_signin` |
| 插件 API | `/status`、`/history`、`/signin`、`/history/clear` |

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | 插件总开关，关闭时不注册定时服务 |
| 开启通知 | 关 | 签到结果推送到 MoviePilot 通知渠道 |
| 立即运行一次 | 关 | 保存配置后延迟 3 秒执行一次签到，开关自动复位 |
| 清除历史记录 | 关 | 清空历史与最近结果，开关自动复位 |
| 使用代理 | 关 | 使用宿主配置的系统代理访问站点 |
| 签到周期 | `0 7 * * *` | Cron 表达式 |
| 随机延迟(秒) | 空 | 形如 `100-200`，留空表示不延迟 |
| 保留历史天数 | `30` | 超期记录在写入时被裁剪 |
| 通知样式 | `style1` | `style1`~`style5` |
| Cnlang Cookie | 空 | 国语视界站点 Cookie，必需 |

### 获取 Cookie

1. 使用浏览器访问 <https://bbs.cnlang.org/> 并登录（登录时勾选「自动登录」，有效期约 30 天）。
2. 按 `F12` 打开开发者工具，切换到「网络 / Network」标签。
3. 刷新页面，点击任一 `bbs.cnlang.org` 请求，在「请求标头 / Headers」中找到
   `Cookie:` 开头的行。
4. 复制整行 Cookie 值（不含 `Cookie:` 前缀），粘贴到插件配置中。

> Cookie 会周期性失效。签到失败并提示 Cookie 失效时，请重新获取并更新。

## 插件 API

所有接口都需要 `bear` 鉴权，最终路径为 `/api/v1/plugin/CnlangSigninV2/<path>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 账号与签到状态摘要（含一次实时站点探测） |
| GET | `/history` | 签到历史明细与统计结果 |
| POST | `/signin` | 立即执行一次签到，返回本次结果 |
| POST | `/history/clear` | 清空签到历史与最近结果 |

## v3.6.0 变更说明

### 已移除的能力

本次重写回归纯 `requests` 实现，以下能力**不再提供**：

- 浏览器模式（无头浏览器过 Cloudflare 验证）
- `cf_clearance` / UA 自动刷新与写入浏览器 Cookie 罐
- 账号密码自动登录
- 登录验证码 OCR 识别
- 浏览器 UA 配置项

> ⚠️ 如果国语视界对签到接口启用了 Cloudflare 校验，纯 `requests` 方案会被拦截。
> 此时需要重新引入浏览器方案。重写前的完整实现保留在标签
> `backup/pre-rewrite-v3.5.2` 中，可直接取回。
>
> 配置项 `browser_mode`、`browser_ua` 已不再读取，保存配置后会被移除，其余配置项
> （Cookie、周期、通知等）名称未变，用户配置可继续沿用。

### V3 规范适配

1. **导入路径**：全部改为稳定 SDK（`app.plugins`、`app.schemas`、`app.sdk.*`），
   移除 `app.core.*`、`app.utils.*`、`app.log` 等兼容桥接路径。
2. **调度方式**：不持有 `BackgroundScheduler`。周期签到由 `get_service()` 返回
   `CronTrigger` 注册到宿主调度器；「立即运行一次」与「随机延迟执行」改用
   `app.sdk.scheduler.add_plugin_once_job()`，不再常驻调度线程。
3. **资源释放**：`stop_service()` 取消本插件登记的一次性任务，可重复调用；
   `init_plugin()` 每次先释放上一轮任务再重建状态，可重复执行。
4. **无导入副作用**：模块导入期与类定义期不做网络请求、不访问数据库、不创建线程。
5. **API 补齐**：`get_api()` 返回真实的接口声明。
6. **虚拟分身兼容**：插件 ID 统一使用 `self.__class__.__name__`，配置、数据与调度
   任务 ID 均随实例隔离。

### 缺陷修复

- 积分与用户组请求先取 `.text` 再判断 `status_code` 的逻辑错误，导致**大洋余额与
  用户组始终取不到值**，现已修正；
- 连续签到天数统计算法重写，原实现在中断后无法得出当前连续天数；
- 请求头 `Accept - Encoding` 拼写修正为 `Accept-Encoding`；
- 历史记录新增 `success` 字段，统计不再依赖站点返回文本中是否包含「签到成功」，
  同时兼容旧数据。

### 其他

- 5 套通知样式由重复的字符串拼接收敛为模块级模板表；
- 新增 43 个单元测试（`tests/v3/cnlangsigninv2/`）。

## 开发与测试

```bash
# 在 MoviePilot 宿主环境中运行
../MoviePilot/.venv/bin/python -m compileall plugins.v3/cnlangsigninv2
../MoviePilot/.venv/bin/python .github/scripts/check_plugin_versions.py \
  package.json package.v2.json package.v3.json
../MoviePilot/.venv/bin/python -m pytest tests/v3/cnlangsigninv2
```

测试使用轻量桩模块提供宿主接口，不访问真实站点，可在任意 Python 3.12+ 环境运行
（需要 `pytest` 与 `apscheduler>=3.10,<4`）。

## 致谢

插件最初来自 imaliang 大佬的脚本。
