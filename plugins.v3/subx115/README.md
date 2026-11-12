# SubX115 — 网盘聚合签到（MoviePilot V3 插件）

把独立工具 [subx](https://hub.docker.com/r/lmhhj/subx)（115 订阅管理，Docker 镜像 `lmhhj/subx`）的能力**原生移植**进 MoviePilot：八大资源渠道配置、订阅匹配、自动转存 115、消息通知，一插件全包，无需再单独跑一个 subx 容器；并整合了 [hdhavensign](https://github.com/JinxJie/MoviePilot-Plugins/tree/main/plugins.v3/hdhavensign) 的**栖影（HDHaven）自动签到**。

配置页侧栏与 subx 原版一致：**资源搜索**（最上面）/ **115设置** / 8 个渠道页（聚影 / 栖影 / 剧迷 / 癫影 / 观影 / 帧影 / 蜗牛4K / 权益宝）。渠道页为 subx 风格卡片（站点地址、账号、密码、Cookie、启用XX搜索），左侧导航（Vuetify `VTabs` 垂直侧栏 + `VWindow` 面板）。纯 Python 交付，无需前端编译。

## 功能

- **资源搜索**（侧栏最上面）：订阅关键词（一行一个）+ 搜索站点勾选；定时轮询在勾选站点中按关键词匹配并自动转存，与 MoviePilot 订阅监控并用（命中任一即转存）
- **115设置**：启用插件、消息通知、轮询周期（cron）、115 Cookie、默认转存目录 cid
- **8 个渠道页**（与 subx 侧栏一致）：聚影 / 栖影 / 剧迷 / 癫影 / 观影 / 帧影 / 蜗牛4K / 权益宝，每渠道站点地址、账号/密码、Cookie、接口地址、来源列表、转存目录、包含/排除关键词（正则）、条数限制；「启用XX搜索」开关与资源搜索页的站点勾选同时生效
- **栖影（HDHaven）自动签到**（在栖影配置页内）：登录预检 -> 查询今日签到状态 -> 明确未签到才提交；提交前写 pending 标记防重复扣账；签到结果通知；支持系统代理/自定义代理；`/subx_sign` 立即签到
- **帧影（FrameHDR）自动签到**（在帧影配置页内）：登录预检 -> 签到（`api/checkin.php`）；Cookie 要求含 `wangpan_session`；签到结果通知；支持系统代理/自定义代理；帧影页「立即签到/测试登录」按钮走独立端点
- **订阅匹配**：自动读取 MoviePilot 订阅（片名+季匹配）
- **自动转存**：命中即调用 115 分享转存；单轮限量防风控；转存历史去重（保留最近 200 条）
- **消息通知**：转存成功 / 签到结果推送消息
- **远程命令**：`/subx_run` 立即轮询，`/subx_status` 查看状态，`/subx_sign` 立即签到
- **插件 API**：`GET /api/v1/plugin/Subx115/history`、`GET /api/v1/plugin/Subx115/status`（apikey 鉴权，供外部系统/脚本调用）

## 安装

```text
plugins.v3/
└── subx115/
    ├── __init__.py    # 主类 Subx115
    ├── channels.py    # 渠道适配层（零宿主依赖）
    ├── hdhaven.py     # 栖影（HDHaven）签到客户端（零宿主依赖）
    ├── zhenying.py    # 帧影（FrameHDR）签到客户端（零宿主依赖）
    ├── p115.py        # 115 转存客户端
    └── pyproject.toml # 额外依赖：curl_cffi（栖影签到用）
```

1. 把 `plugins.v3/subx115/` 整个目录复制到你的插件仓库 `plugins.v3/` 下（或本地 MoviePilot 的插件目录）；
2. 把 `package.v3.json` 中的 `Subx115` 条目合并进插件市场索引 `package.v3.json`（自建市场时）；
3. 重启 MoviePilot，在「插件市场 → 已安装」中找到 **网盘聚合签到** 并安装/启用；
4. 按下方顺序完成首次配置。

> 三处版本必须一致：`__init__.py` 的 `plugin_version`、索引 `version`、`history` 顶部记录（当前均为 `1.6.0`）。

## 快速上手（3 步）

1. **顶部全局栏**：启用插件；浏览器登录 115 → F12 复制完整 Cookie 粘贴进来；默认转存目录填 cid（打开 115 网页版进入目标目录，地址栏 `cid=` 后面的数字，`0` 为根目录）
2. **渠道页**：至少启用 1 个渠道并填接口地址（RSS 或 JSON 接口；具体地址从各渠道自身获取）
3. 保存后发送 `/subx_run` 立即执行一轮，观察日志中 `SubX115` 前缀的输出

**栖影签到**：浏览器登录 hdhaven.com 完成人机验证 → F12 复制完整 Cookie（必须含 `hdh_session`）→ 填到栖影配置页的「栖影登录会话 Cookie」→ 开启「启用自动签到」→ 可勾选「保存后立即签到一次」验证。

**帧影签到**：浏览器登录 framehdr.com → F12 复制完整 Cookie（必须含 `wangpan_session`）→ 填到帧影配置页「帧影签到设置」的「帧影登录会话 Cookie」→ 开启「启用每日自动签到」→ 点帧影页右上角「测试登录」验证会话有效后再「立即签到」。

## 工作流程

```text
cron 轮询
  └─ 各启用渠道 fetch() 拉取最新 115 分享（RssChannel / JsonApiChannel）
       └─ 渠道级过滤：include / exclude 正则 + 条数限制
            └─ 与 MoviePilot 订阅匹配（片名 + 季）
                 └─ 历史去重（save_data）
                      └─ P115Client.receive_share() 转存
                           └─ 消息通知

栖影签到（cron，默认每天 09:30）
  └─ 登录预检 /api/auth/me（只读）
       └─ 查询今日签到状态：已签到则直接返回，不重复提交
            └─ 写 pending 标记并读回校验（防重复扣账）
                 └─ POST /api/account/checkin（空请求体）
                      └─ 仅明确成功（201 + success + normal）才记成功 → 通知
```

## 渠道接口校准（重要）

各渠道（聚影/栖影等）没有公开统一的接口规范，插件内置两种通用适配模式：

| 适配器 | 适用 | 行为 |
| --- | --- | --- |
| `RssChannel` | RSS / HTML 列表 | 正则解析 `<item>`，提取 115 链接与提取码 |
| `JsonApiChannel` | JSON 接口 | 兼容 `data/list/items/result` 等常见结构，字段名取 `title/name`、`link/url/share_url` |

- 若某渠道的实际协议不同（如需要特定签名、分页参数），继承对应基类重写 `fetch()` 即可（`channels.py` 中每个渠道已有独立子类，改动一处不影响其它渠道）；
- 渠道需要登录时把 Cookie/Token 填到该渠道的「凭证」字段（会同时以 `Cookie` 头和 `Bearer` Token 发送）。

## 风险提示与常见坑

- **115 网页接口非公开**：`p115.py` 使用的 `webapi.115.com` 接口可能随官方调整失效，届时按浏览器抓包校准 `ENDPOINTS` 与参数；有条件建议换成 115 开放平台（`proapi.115.com`）实现，保持 `receive_share/get_user` 签名不变即可无缝替换。
- **Cookie 有效期**：115 Cookie 会过期（异地登录/风控），失效后插件日志会告警；重新复制即可。Cookie 是账号凭证，**不要发到群聊/截图/提交仓库**。
- **风控**：转存是写操作，单轮转存上限固定为 10（`__init__.py` 的 `_max_transfer`，需要调整时修改后重载插件），轮询周期不建议低于 1 小时；失败项不写历史，下一轮会自动重试。
- **订阅匹配是粗匹配**：片名包含 + 季一致；标题里解析不出季时不过滤季（避免漏单集追更）。误匹配多时用渠道 `include/exclude` 正则收紧。
- **转存成功但 MoviePilot 不识别**：本插件只负责把文件放进 115 目录，入库刮削依赖 MoviePilot 本身的整理链路（115 目录挂载/STRM 由你现有的方案处理）。
- **秒传说明**：subx 的 `./upload:/115upload` 是本地秒传目录方案，与「分享转存」是两条不同链路；本插件实现的是分享转存（不依赖本地文件），如有本地秒传需求保持原有挂载方案并用本插件做订阅触发即可。
- **栖影签到依赖 curl_cffi**：签到需要 chrome TLS 指纹模拟（`pyproject.toml` 已声明）；宿主未安装时签到自动禁用并告警，渠道订阅不受影响。登录与人机验证必须在浏览器完成，插件不处理人机验证；站点返回非 JSON（遭遇人机验证）时签到会明确报「请人工核对」，**勿盲目重试**。
- **签到防重复扣账**：插件沿用原插件的安全设计——每次先查站点状态、提交前写 pending 标记、只认明确成功响应；出现「结果待核对」通知时请先到站点人工核对。

## dev / prod 环境建议

| 环境 | 建议 |
| --- | --- |
| dev（测试） | 只启用 1 个渠道 + 关闭通知，cron 拉长（如 `0 4 * * *`），用小目录 cid 验证转存闭环；先用只读渠道（RSS）确认解析正确再开转存 |
| prod | 启用通知；115 Cookie 只配在 prod；定期看详情页成功率和日志；转存异常连续出现时先检查 Cookie 再查接口 |

## 二次开发

- 纯函数（解析/匹配）在 `channels.py`，零宿主依赖，可用 `pytest tests/v3/subx115/` 直接跑测试；
- 新增渠道：`CHANNEL_DEFS`（主类，配置页）+ `ADAPTER_CLASSES`（channels.py，适配器）各加一条即可；
- 固定运行参数（单轮最大转存数 `_max_transfer`、请求超时 `_p115_timeout`）在主类顶部，按需修改后重载插件；
- 提交前自检：`python -m compileall plugins.v3/subx115`、跑测试、并在真实 V3 宿主中至少加载一次，确认 启用/停用/重载 无资源残留、事件只注册一次。

## 远程命令与 API

| 入口 | 说明 |
| --- | --- |
| `/subx_run` | 立即执行一轮轮询 |
| `/subx_status` | 查看运行状态 |
| `/subx_sign` | 立即执行栖影（HDHaven）签到 |
| `GET /api/v1/plugin/Subx115/history` | 转存历史（apikey） |
| `GET /api/v1/plugin/Subx115/status` | 运行状态（apikey，含签到状态） |
