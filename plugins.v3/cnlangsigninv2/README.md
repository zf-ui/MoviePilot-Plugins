# 国语视界签到V3 (CnlangSigninV2)

国语视界（[cnlang.org](https://bbs.cnlang.org/)）自动签到助手，按 MoviePilot **V3**
插件开发规范实现。

- 插件 ID：`CnlangSigninV2`
- 插件目录：`plugins.v3/cnlangsigninv2/`
- 插件版本：`3.6.3`
- 主系统要求：`>=3.0.0`

> **v3.6.3 修复了浏览器模式卡在签到页 Cloudflare 挑战的问题**（`v3.6.2` 会持续提示
> 「签到页 Cloudflare 验证未通过」）。详见 [v3.6.3 修复说明](#v363-修复说明)。
>
> 站点现状：首页不受 Cloudflare 挑战，但**只有签到页路径下发托管挑战**，纯 `requests`
> 必然被拦截（403 + `Cf-Mitigated: challenge`），因此浏览器模式是必需的。

## 功能

| 能力 | 说明 |
| --- | --- |
| 定时签到 | 使用 Cron 表达式配置签到周期，默认每天 07:00 |
| 随机延迟 | 配置 `100-200` 形式的区间，实际执行时刻在区间内随机，降低风控风险 |
| 立即运行一次 | 保存配置后立即触发一次签到，任务交由宿主调度器执行 |
| 系统代理 | 可复用 MoviePilot 配置的系统代理访问站点，代理失效时自动回退直连 |
| 浏览器模式 | 被 Cloudflare 拦截时用宿主内置无头浏览器完成验证并签到（默认开启） |
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
| 浏览器模式 | 开 | 被 Cloudflare 拦截时切换到宿主内置无头浏览器完成签到 |
| 浏览器UA | 空 | 留空时：`requests` 用内置 Chrome UA、浏览器模式用浏览器原生 UA。填写则两边都用该值，且**必须与导出 Cookie 的浏览器完全一致** |
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
>
> 站点受 Cloudflare 保护，Cookie 中通常会包含 `cf_clearance`。该 Cookie **与浏览器
> User-Agent 及 TLS 指纹绑定**：如果你打算靠 Cookie 走纯 `requests` 路径，请把
> 「浏览器UA」填成与导出 Cookie 时完全一致的 UA，否则会立刻被判为无效。

## 插件 API

所有接口都需要 `bear` 鉴权，最终路径为 `/api/v1/plugin/CnlangSigninV2/<path>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 账号与签到状态摘要（含一次实时站点探测） |
| GET | `/history` | 签到历史明细与统计结果 |
| POST | `/signin` | 立即执行一次签到，返回本次结果 |
| POST | `/history/clear` | 清空签到历史与最近结果 |

## v3.6.3 修复说明

修复 `v3.6.2` 中浏览器模式**卡在签到页 Cloudflare 挑战**、始终提示
`签到页 Cloudflare 验证未通过` 的问题。

**实测到的站点行为**（这是定位问题的关键）：

| URL | 结果 |
| --- | --- |
| `https://cnlang.org/` | **200**，`Server: cloudflare` 但**无挑战**，只下发 4 个 Discuz Cookie，**不签发 `cf_clearance`** |
| `https://cnlang.org/dsu_paulsign-sign.html?mobile=no` | **403**，`Cf-Mitigated: challenge`，`Set-Cookie` 为空，正文 `<title>Just a moment...</title>` |

即：首页完全不受挑战，只有签到页路径下发托管挑战。由此推出 `v3.6.2` 的三个缺陷。

**修复内容**：

1. **不再把内置 UA 强加给浏览器**。`v3.6.2` 无条件传
   `user_agent="…Chrome/131.0.0.0…"`，把 Chrome 131 套到 cloakbrowser 实际的
   Chromium 版本上，造成 UA 与 Client Hints 不一致——这是 Cloudflare 判定
   「非真实浏览器」的典型特征，会持续下发交互式挑战。现在**只在用户显式配置了
   「浏览器UA」时才覆盖**，否则交给浏览器使用自己的原生 UA。
2. **注入 Cookie 时剔除 Cloudflare 自管字段**。`cf_clearance` / `__cf_bm` /
   `cf_chl_*` 与签发时的 UA、TLS 指纹、IP 强绑定，把旧值注入无头浏览器会让 CF 发现
   「通行证对不上自己的签发记录」而直接不信任会话。论坛登录态（`_auth` 等）仍然保留。
3. **等待挑战期间不再重载页面**。Turnstile 的挑战进度保存在当前文档里，`reload`
   会让进度归零；`v3.6.2` 的「每轮只等 20 秒 + 重载重试 3 轮」等于每次都从零开始，
   签到页因此永远等不到通过。改为**单次 90 秒不中断等待**，仅在页面长时间空白
   （连接被挂起）时才重载一次。
4. **补点人机验证框改为限次**：整轮最多 3 次、首次静待约 14 秒。高频点击 Turnstile
   反而会被判为机器人行为。
5. **新增页面内 fetch 兜底**：若文档导航持续被挑战，改从站点首页用**同源 `fetch`**
   取签到页——复用浏览器已建立的 TLS 会话与 Cookie，往往能绕开只针对文档导航下发的
   托管挑战。取到后，签到提交与积分查询本来就走页面内 fetch，整条链路保持一致。
6. **修正挑战页识别**：`_is_cf_challenge_page` 原先只能靠页面标题判断，而页面内
   `fetch` 拿到的响应**没有页面标题**，会把挑战页误当业务页解析并报出
   「未获取到用户名」这种误导性错误。现在同时识别正文里的 `<title>` 特征。
7. 直接访问签到页，不再先「热身」首页——首页不签发 `cf_clearance`，热身毫无收益。
8. 回归测试由 74 个增加到 97 个。

**若仍然失败**：说明该挑战确实需要人工交互，无头浏览器无法通过。此时请改用
方案二——关闭「浏览器模式」，从浏览器开发者工具复制**完整** Cookie（务必包含
`cf_clearance`），并把「浏览器UA」填成与你浏览器**完全一致**的值。

## v3.6.2 修复说明

修复签到被 Cloudflare 人机验证拦截、日志反复出现
`请求 https://cnlang.org/dsu_paulsign-sign.html?mobile=no 失败，状态码：403`
导致**完全无法签到**的问题。

**成因**：国语视界已启用 Cloudflare 托管挑战（Managed Challenge）。站点对
`requests` 返回的是验证页而不是业务页面，响应特征为：

```
HTTP/1.1 403
Server: cloudflare
Cf-Mitigated: challenge
Content-Type: text/html; charset=UTF-8

<title>Just a moment...</title>
```

这类挑战要求浏览器执行 JavaScript 并完成 Turnstile 校验，纯 `requests`
（无论直连还是走代理）都不可能通过。`v3.6.0` 移除浏览器模式后，签到功能实际
已不可用。

**修复内容**：

1. **Cloudflare 拦截识别**：新增 `_is_cf_challenge()`，优先读官方响应头
   `Cf-Mitigated: challenge`；无该头时，仅当 `Server: cloudflare` 且正文含
   `Just a moment...` / `challenges.cloudflare.com` 才判定为拦截——避免把普通
   403 误判成挑战页。
2. **浏览器模式（重新引入，默认开启）**：识别到拦截后按配置切换到宿主内置
   无头浏览器（`app.sdk.browser.launch_browser_context`，即 cloakbrowser）
   完成整轮签到。**验证与签到提交都在浏览器页面上下文内进行**，因为
   `cf_clearance` 与 TLS 指纹、User-Agent 绑定，`requests` 无法复用浏览器拿到的
   通行证。流程为：访问站点首页等待挑战通过 → 合并浏览器新签发的 Cookie 回配置
   （**同时记录浏览器实际使用的 UA**，因为 `cf_clearance` 与 UA 绑定，否则写回的
   Cookie 配上一个不同的 UA 会立即失效）→ 打开签到页（挑战未通过时重载重试 3 轮，
   并尝试点击 Turnstile 复选框）→ 页面内提交签到 → 读取大洋余额 → 落库并通知。
3. **浏览器UA 配置项（恢复）**：请求头统一发送浏览器 UA，不再暴露
   `python-requests` 标识。留空时使用内置的 Chrome 131 UA。
4. **代理容错**：代理无响应时自动回退直连重试一轮，避免代理失效导致整轮签到报废。
5. **失败原因可定位**：失败信息携带 HTTP 状态码（如「获取签到页面失败，状态码：403」），
   不再只报笼统的「请检查网络或代理设置」。
6. **兜底建议**：若用户关闭了浏览器模式而被拦截，会返回明确的可执行建议
   （开启浏览器模式，或提供含 `cf_clearance` 且 UA 完全一致的 Cookie），
   而不是一个无法定位的错误。
7. 回归测试由 47 个增加到 74 个（v3.6.3 再增加到 97 个），覆盖拦截识别、UA 传递、浏览器模式分支与代理回退。

**宿主依赖**：浏览器模式需要宿主提供 `app.sdk.browser`。MoviePilot V3 依赖中已包含
`playwright` 与 `cloakbrowser`；若宿主未安装浏览器依赖，插件会给出明确提示而不是静默失败。

## v3.6.1 修复说明

修复保存插件配置时前端提示 **「国语视界签到V3 配置保存失败：未知错误」** 的问题。

**成因**：宿主在“保存插件配置”流程中直接调用 `init_plugin()`，而
`PluginConfigCommand.update()` 只捕获 `PluginMutationRejectedError`，其余异常会被
统一转成 HTTP 500 并抹掉细节（`app/factory.py` 的兜底处理器把 message 固定为
“未知错误”）。原实现在 `init_plugin()` 里直接访问
`app.sdk.scheduler.add_plugin_once_job`，在**未提供该接口的宿主版本**上会抛出
`AttributeError`，于是配置永远保存不成功。

**修复内容**：

1. `init_plugin()` 内部的副作用操作（停止旧任务、清除历史、回写配置）全部就地兜底，
   失败只记日志，不再向上抛出——插件异常不应让宿主的配置保存接口返回 500。
2. 按官方开发指南的兼容要求，用 `getattr(scheduler_sdk, "add_plugin_once_job", None)`
   探测宿主能力；缺失或登记失败时回退为**后台守护线程**执行签到，
   `remove_plugin_once_job` 同理。
3. 时区解析改为 `pytz` 优先、`zoneinfo` 兜底、最后交由调度器默认时区，逐级降级。
   MoviePilot V3 依赖 `pytz` 但不保证容器内存在系统时区库，精简镜像下
   `ZoneInfo` 会抛 `ZoneInfoNotFoundError`。
4. `get_service()` / `_next_sign_time()` 统一走 `_build_trigger()`，异常捕获从
   `(ValueError, TypeError)` 放宽到 `Exception`，避免漏掉时区类异常。
5. 回归测试由 43 个增加到 47 个，覆盖上述兼容与兜底分支。

## v3.6.0 变更说明

> ⚠️ **本节中「已移除的能力」已在 `v3.6.2` 中部分撤销**：站点实际启用了 Cloudflare
> 托管挑战，纯 `requests` 方案会被拦截，因此浏览器模式与「浏览器UA」配置项已重新
> 引入。账号密码自动登录与验证码 OCR 识别仍不提供。

### 已移除的能力

本次重写回归纯 `requests` 实现，以下能力**不再提供**：

- 浏览器模式（无头浏览器过 Cloudflare 验证）——*v3.6.2 已恢复*
- `cf_clearance` / UA 自动刷新与写入浏览器 Cookie 罐
- 账号密码自动登录
- 登录验证码 OCR 识别
- 浏览器 UA 配置项——*v3.6.2 已恢复*

> 重写前的完整实现保留在标签 `backup/pre-rewrite-v3.5.2` 中，可直接取回。

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
