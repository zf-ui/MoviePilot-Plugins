# 国语视界签到V3 (CnlangSigninV2)

国语视界（[cnlang.org](https://bbs.cnlang.org/)）自动签到助手，按 MoviePilot **V3**
插件开发规范实现。

- 插件 ID：`CnlangSigninV2`
- 插件目录：`plugins.v3/cnlangsigninv2/`
- 插件版本：`3.6.6`
- 主系统要求：`>=3.0.0`

> **v3.6.6 更正了 v3.6.5 的错误结论。** 实测证明：站点签到路径上的挑战是 Cloudflare
> **交互式**（Turnstile 勾选框）类型，必须由真实浏览器**人工**完成。以下客户端全部无法
> 通过（均已实测）：纯 `requests`、`curl_cffi`（chrome150 指纹 + 完整 Client Hints）、
> 无头 Chrome、以及**全新 profile 的有头 Chrome（跑满 150 秒仍未取得 `cf_clearance`）**。
> **唯一能直接放行的是有效的 `cf_clearance`**，因此请以「更新 Cookie」为主要手段，
> 浏览器模式只作兜底。详见 [v3.6.6 修复说明](#v366-修复说明)。

## 功能

| 能力 | 说明 |
| --- | --- |
| 定时签到 | 使用 Cron 表达式配置签到周期，默认每天 07:00 |
| 随机延迟 | 配置 `100-200` 形式的区间，实际执行时刻在区间内随机，降低风控风险 |
| 立即运行一次 | 保存配置后立即触发一次签到，任务交由宿主调度器执行 |
| 系统代理 | 可复用 MoviePilot 配置的系统代理访问站点，代理失效时自动回退直连 |
| curl_cffi 指纹 | 快速路径可改用 curl_cffi，以真实 Chrome TLS/JA3 + HTTP/2 指纹发请求（默认开启，未安装时自动回退宿主网络组件） |
| 浏览器模式 | 被 Cloudflare 拦截时用宿主内置浏览器完成验证并签到（默认开启，仅作兜底：交互式挑战实测无法自动通过） |
| 强制浏览器模式 | 跳过快速路径直接走浏览器模式，用于主动验证浏览器模式是否可用（默认关闭） |
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
| 浏览器模式 | 开 | 被 Cloudflare 拦截时切换到宿主内置浏览器完成签到（仅作兜底） |
| 强制浏览器模式 | 关 | 跳过 HTTP 快速路径，直接走浏览器模式，用于验证浏览器模式是否可用 |
| curl_cffi 指纹 | 开 | 快速路径用 curl_cffi 发请求（真实 Chrome TLS/JA3 指纹）；未安装 curl_cffi 时自动回退 |
| 指纹伪装目标 | `auto` | `auto` 按「浏览器UA」里的 Chrome 版本自动匹配；也可填 `chrome131` 等具体值；**留空则关闭 curl_cffi** |
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
> 站点受 Cloudflare 保护，签到路径要求 Cookie 中含有效的 `cf_clearance`。
> **`cf_clearance` 与 User-Agent 绑定**（实测：与 TLS 指纹**无关**——用 Python
> `requests` 的 TLS 栈同样能通过，只要 Cookie 够新、UA 与签发时一致）。因此请把
> 「浏览器UA」填成与导出 Cookie 时**完全一致**的 `navigator.userAgent`，否则会被立即拒绝。
>
> `cf_clearance` 的有效期有限，签到失败时**首选操作就是重新复制一份 Cookie**。

## 插件 API

所有接口都需要 `bear` 鉴权，最终路径为 `/api/v1/plugin/CnlangSigninV2/<path>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 账号与签到状态摘要（含一次实时站点探测） |
| GET | `/history` | 签到历史明细与统计结果 |
| POST | `/signin` | 立即执行一次签到，返回本次结果 |
| POST | `/history/clear` | 清空签到历史与最近结果 |

## v3.6.6 修复说明

**这一版推翻了 `v3.6.5` 的核心结论，并把插件的定位改对了。**

### 推翻的两个错误结论

1. **`v3.6.5` 说「先在首页换取域级 `cf_clearance`」是浏览器模式失败的结构性原因。**
   **这是错的。** 实测：`https://cnlang.org/` 返回 `200`，且**不下发任何 Cloudflare
   Cookie**（只有 4 个 Discuz 会话 Cookie）——首页根本不会签发 `cf_clearance`，
   「首页优先」既无收益，也不构成任何修复。
2. **`v3.6.4` 说「挑战必须人工交互」** —— 方向是对的，但当时没有证据。本版补齐了证据。

### 实测证据（全部来自真实请求）

| 客户端 | 结果 |
| --- | --- |
| 纯 `requests` / `urllib` | `403` + `Cf-Mitigated: challenge` |
| `curl_cffi` `impersonate=chrome150` | `403`，挑战类型 `cType=interactive` |
| `curl_cffi` + 完整 Client Hints（`Sec-CH-UA-*` 全量） | `403`，`cType=interactive` |
| `curl_cffi` + 先建立论坛会话（`saltkey`/`sid`） | `403`，`cType=interactive` |
| 无头 Chrome（真实 Chrome） | 停在 `请稍候…`，未通过 |
| **有头 Chrome，全新 profile，跑满 150 秒** | **始终未取得 `cf_clearance`**，反复 `__cf_chl_rt_tk` 重试 |

站点对签到相关路径（`/dsu_paulsign-sign.html`、`/plugin.php?id=dsu_paulsign:*`）**一律**
下发 `cType=interactive` 的 Turnstile 勾选框挑战；而 `/` 与 `/home.php?mod=spacecp&ac=credit`
完全不受保护。

**结论**：这类挑战必须由真实浏览器执行 JS 并完成交互，**任何自动化客户端都无法通过**，
指纹伪装（含 `curl_cffi`）也不例外。唯一能直接放行的是**有效的 `cf_clearance`**。

### 本版改动

1. **接入 `curl_cffi`（可选增强）**：快速路径默认改用 `curl_cffi` 发请求，复刻 Chrome 的
   TLS/JA3 + HTTP/2 指纹与请求头顺序，让「带有效 `cf_clearance` 的快速路径」以最接近真实
   浏览器的形态发出请求。`impersonate` 支持 `auto`：按「浏览器UA」里的 Chrome 主版本自动
   挑选最接近的受支持目标，避免 TLS 指纹版本与 UA 版本不一致。**未安装 `curl_cffi` 或请求
   出错时静默回退宿主 `RequestUtils`**，不影响可用性。
2. **新增「强制浏览器模式」开关**：跳过快速路径直接走浏览器模式，方便主动验证浏览器模式
   在你自己的环境里到底能不能用。
3. **兜底建议文案重写**：不再推荐「开启浏览器模式」，而是给出**更新 Cookie 的分步操作**
   （含 `cf_clearance`、UA 对齐、有效期说明）。
4. **更正文档**：删除「`cf_clearance` 绑定 TLS 指纹」的错误说法。实测用 Python `requests`
   的 TLS 栈同样能通过——**决定因素是 Cookie 的新鲜度与 UA 是否一致**。
5. 回归测试由 113 个增加到 127 个（curl_cffi 接线与回退、指纹目标解析、强制浏览器模式、
   新增配置项回写等）。

### 你应该怎么做

- **签到失败时，第一件事是重新复制 Cookie**：F12 → 网络 → 任一 `cnlang.org` 请求 →
  复制完整 `Cookie` 头，并把「浏览器UA」填成 `navigator.userAgent` 的原值。
- 浏览器模式在 Docker 容器（Xvfb 虚拟显示）里**基本无效**——实测虚拟显示下 CF 不会放行。
- 想验证浏览器模式？打开「强制浏览器模式」跑一次即可，日志会逐次汇总每次尝试的失败原因。

## v3.6.5 修复说明

> ⚠️ **本节的核心结论已被 `v3.6.6` 推翻。** 实测证明首页**不会签发 `cf_clearance`**
> （首页返回 200 且不下发任何 Cloudflare Cookie），因此「首页优先」不是浏览器模式失败的
> 原因，也没有任何修复效果。真正的原因是：签到路径的挑战是 **interactive** 类型，必须由
> 真实浏览器人工完成，**自动化客户端无法通过**。请以 `v3.6.6` 为准。

修复 `v3.6.4` 仍然存在的**浏览器模式卡在签到页 Cloudflare 挑战**问题。这一次找到的是
**结构性原因**，而不是参数细节。

**根因：访问顺序错了。**

`cf_clearance` 是 Cloudflare 签发的**域级**通行证。正确的顺序是：

```
访问站点首页 → 完成挑战、拿到 cf_clearance → 再访问签到页（此时直接放行）
```

而 `v3.6.3` / `v3.6.4` 是**一上来就直接访问签到页**——此时浏览器手里一张通行证都没有，
等于主动撞上 Cloudflare 为签到页路径配置的那条最严规则，于是签到页下发**交互式挑战**
（Turnstile 复选框），而自动化浏览器恰恰过不了它。90 秒里一直停在 `Just a moment...`，
补点验证框也没有用。

这一点在最后一个可用版本 `v3.5.2` 里本来是**正确的**（它的注释写着「先访问站点首页：
CF 验证对全站生效」），`v3.6.3` 我基于「首页不受挑战、所以热身没有收益」的实测把它删掉了
——**实测结论没错，推论错了**：首页不受挑战不代表不需要先访问它，因为要的是它顺带签发的
那张通行证。

**修复内容**：

1. **恢复「首页优先」顺序**：先 `goto` 站点首页并等待挑战通过，**立刻写回**这一步拿到的
   Cookie（`cf_clearance` 就在其中），然后再 `goto` 签到页。
2. **不再剔除配置里的 Cloudflare Cookie**。`v3.6.3` 出于「旧通行证与签发记录不符会让 CF
   不信任会话」的猜测把 `cf_clearance` / `__cf_bm` / `cf_chl_*` 一律丢掉了。实际上
   `cf_clearance` 是域级的：只要还在有效期内，注入后首页与签到页都直接放行，是**成本最低的
   热启动路径**；即便已失效，Cloudflare 也只是重新下发一次挑战，**不会因此永久不信任会话**。
   剔除反而让每次执行都必须从零过一次挑战。
3. **CF 通行证缓存改为带 `domain` / `path` 的结构化列表**。同名 Cookie 可能同时存在多条
   （不同作用域），原先拼成 `k=v; k=v` 会把它们压成一条并丢失作用域——实测日志里确实出现过
   两条 `cf_clearance`。
4. **导航超时由 45 秒降到 20 秒**。Cloudflare 挑战页的脚本会长时间占住
   `DOMContentLoaded`，白等 45 秒没有意义；真正的等待交给页面内容轮询。
5. **预算收敛**：首页热身 45s、签到页 60s、有头重试 45s，控制整轮签到耗时。
6. **兜底建议文案改写**：把「用自己浏览器的 Cookie + 与之一致的 UA」明确标为**最稳方案**，
   并写清获取步骤与 `cf_clearance` 的有效期。
7. 回归测试由 108 个增加到 113 个（新增首页优先顺序、首页失败后继续、通行证立刻写回、
   同名 Cookie 分作用域缓存等）。

**若仍然失败**：说明 Cloudflare 确实拒绝了自动化浏览器，请改用最稳方案——**关闭「浏览器模式」**，
用你自己的浏览器登录站点，复制**完整** Cookie（务必含 `cf_clearance`），并把「浏览器UA」
填成该浏览器 `navigator.userAgent` 的**完全一致**值。`cf_clearance` 有效期通常只有几十分钟。

## v3.6.4 修复说明

修复 `v3.6.3` 仍然存在的**浏览器模式卡在签到页 Cloudflare 挑战**问题：日志里签到页
标题持续为 `Just a moment...`，补点人机验证框也没有效果，最终提示
`浏览器模式未通过 Cloudflare 人机验证`。

**根因**：插件启动浏览器时只传了 `headless` 与 `user_agent`，**漏掉了宿主的拟人化
配置**。MoviePilot 自身启动浏览器时固定会传这两个参数（见
`app/adapters/network/browser.py`）：

```python
launch_browser_context(
    headless=headless,
    proxy=proxies,
    user_agent=user_agent,
    humanize=get_runtime_setting('CLOAKBROWSER_HUMANIZE'),          # 默认 True
    human_preset=get_runtime_setting('CLOAKBROWSER_HUMAN_PRESET'),  # 默认 "default"
)
```

而 `app.sdk.browser.launch_browser_context` 会把这两个参数原样透传给
`cloakbrowser.launch_context`。插件省略它们，等于让 cloakbrowser 退回**脚本化的固定
行为**——鼠标瞬移、输入零延迟，Cloudflare 的托管挑战据此把会话判为机器人，于是持续
下发**无法自动完成的交互式验证**。这也解释了为什么「首页 0 秒通过、签到页 90 秒不通」：
首页本来就没有挑战，签到页的挑战才是真的过不去。

**修复内容**：

1. **跟随宿主配置传入拟人化参数**（核心修复）。读 `settings.CLOAKBROWSER_HUMANIZE`
   与 `settings.CLOAKBROWSER_HUMAN_PRESET`，与宿主自身启动浏览器的方式完全一致；
   宿主关闭拟人化时也不擅自开启。
2. **无头模式失败后自动升级到有头模式重试一次**。无头 Chromium 是 Cloudflare 下发
   交互式挑战的常见诱因；有头模式依赖宿主提供的虚拟显示资源（`host.display`，
   MoviePilot 通过 `pyvirtualdisplay` 提供），**资源缺失时该次尝试自动跳过**，
   并保留无头模式的失败结论，不会因此报错。
3. **启动参数分级尝试**：`无头 + 拟人化`（预算 90s）→ `有头 + 拟人化`（预算 45s）。
   预算逐级收敛，避免整轮签到耗时失控。
4. **兼容旧版浏览器实现**：若 `cloakbrowser` 不认 `humanize` / `human_preset`
   （`TypeError`），自动去掉这两个参数重试一次，而不是直接失败。
5. **Cloudflare 通行证改为单独缓存**。上一轮**由本浏览器自己签发**的
   `cf_clearance` / `__cf_bm` 存进插件数据（`cf_cookies`），下次启动浏览器时注入回去，
   让浏览器直接从「已通过验证」的状态开始，省掉一整轮交互式挑战。同时不再把 20 多个
   浏览器 Cookie 灌进用户配置的 Cookie 字段（此前会污染该配置项）；配置字段里**来自
   用户自己浏览器**的旧 `cf_clearance` 仍然会被剔除，因为它与本浏览器的 UA、TLS 指纹
   对不上。
6. **失败信息汇总每一次尝试的具体原因**，可直接区分是缺依赖、无显示资源，还是挑战
   确实未通过。
7. 回归测试由 97 个增加到 108 个。

**若仍然失败**：说明该挑战确实需要人工交互。此时请改用方案二——关闭「浏览器模式」，
从浏览器开发者工具复制**完整** Cookie（务必包含 `cf_clearance`），并把「浏览器UA」
填成与你浏览器**完全一致**的值。

> ⚠️ 本节「若仍然失败」的判断**已被 `v3.6.5` 修正**：真正的原因是**访问顺序**错了
> （没有先在首页换取域级 `cf_clearance` 就直撞签到页），不是「挑战必须人工交互」。
> 请优先升级到 `v3.6.5`。本节第 2 条「剔除所有 Cloudflare 自管 Cookie」也已在
> `v3.6.4` / `v3.6.5` 中撤销：`cf_clearance` 是域级的，注入回来是最有效的热启动手段。

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

> ⚠️ 本节结论**已被 `v3.6.4` 修正**：签到页挑战过不去的真正原因是插件漏传了宿主的
> 拟人化启动参数（`humanize` / `human_preset`），而不是「挑战必须人工交互」。请优先
> 升级到 `v3.6.4`。另外本节第 2 条「剔除所有 Cloudflare 自管 Cookie」在 `v3.6.4`
> 中细化为：只剔除**配置字段里**的旧值，插件自己签发的通行证会被缓存并复用。

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
