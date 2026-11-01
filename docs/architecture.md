# 架构说明 · news_ai_science（AI 科技头条）

对标 Perplexity Discover 的卡片范式：**封面图 + 中文标题 + 3~10 条摘要 + 原文跳转**。

## 1. 分层

```
src/news_ai_science/
├── main.py                 CLI：解析参数 -> 加载配置 -> 起一轮
├── core/                   核心，无业务逻辑，被所有层依赖
│   ├── paths.py            唯一的路径真相（项目根/配置/数据/日志）
│   ├── config.py           config.yaml + sources.yaml + .env，env 覆盖
│   ├── logger.py           控制台 + 按天轮转，幂等
│   ├── text.py             空白折叠/截断/去标签/中英判断
│   └── errors.py           ConfigError / CrawlError / LLMError
├── contracts/models.py     数据契约：Article（source/title/url/body/cover_image）
├── stages/
│   ├── crawl/web.py        Camoufox 抓列表页 + 文章正文 + 正文大图打分
│   ├── generate/
│   │   ├── client.py       OpenAI 兼容客户端（超时/重试/熔断/关思考模式）
│   │   └── translator.py   中文标题 + 3~10 条中文摘要
│   └── push/
│       ├── card_builder.py 飞书 2.0 图文卡片（纯函数）
│       ├── image_uploader.py 应用凭证换 token -> 上传图 -> img_key
│       └── feishu_sender.py 群机器人 webhook + 令牌桶限流
├── pipeline/runner.py      编排：collect -> push
└── storage/db.py           SQLite：去重、pending/pushed、轮换
```

依赖方向严格单向：`main → pipeline → stages → contracts/core`。

## 2. 一轮的数据流

```
sources.yaml 里启用的源
   └─> Camoufox（整轮只开一次浏览器）
         ├─> 列表页抽候选链接（同域 + 日期路径 + slug 启发式）
         └─> 链接层去重（canonical = host+path，已入库的不进正文页）
               └─> 逐篇进正文页：抽正文 + 正文大图打分（取不到才回退 og:image）
                │
                ▼
       去重（canonical_url）──> 必须有配图，否则跳过
                │
                ▼
       LLM：中文标题 + 100 字摘要 + 全文翻译 ×  N 条（默认 4）
                │  输出必须通过中文校验（cjk 占比），不合格即丢弃并从候选池补位
                ▼
       上传封面图拿 img_key ──> SQLite(pending)
                │
                ▼
       一张聚合卡（1~5 条，左图右文 + 折叠要点） ──成功──> mark_pushed（整批）
                                                 └─失败──> 保持 pending，下一轮重发（不发纯文本）
```

> 聚合推送：一轮抓 4 条全新新闻 → 组装成**一条** `interactive` 卡片消息发出去。
> 纯文本兜底已废弃——它会让群里出现「一份文本 + 一张卡片」，看起来像重复推送。

## 3. 错误策略

| 异常 | 触发点 | 上层动作 |
|---|---|---|
| `ConfigError` | 启动加载配置 / 无启用源 | 打印 FATAL，exit 2 |
| `CrawlError` | 浏览器起不来 / **所有源都失败** | 打 ERROR 堆栈；仍用库里有图的存量推送 |
| `LLMError` | 模型连续失败/空回复 | 该篇丢弃：中文源降级为中文原文节选，外文源直接不推（**绝不推英文**），不影响整轮 |

单源失败只记 `WARNING` 并跳过，不影响其它源——这是"部分失败可接受，全失败才算故障"的取舍。

## 4. 关键设计取舍

- **配图只用正文里的真实大图**：在 `article/main` 内遍历图片，剔除 logo/icon/banner/pixel，
  要求 ≥400×200、宽高比 0.5~3，取最大者；取不到才回退 `og:image`。
  宁可少推一条，也不用无关图凑数（`crawl.require_image: true`）。
- **去重键是 URL 路径，不是标题**：`canonical_url()` 丢掉 scheme/query/fragment/www/尾斜杠，
  归一化成 `host+path` 后做唯一索引。标题去重会被「多家媒体同题」「翻译抖动」「标题党改写」击穿。
- **一张卡装多条**：`column_set` 左图右文（图 weight 2 / 文 weight 3），单卡上限 5 条；
  超过 5 条飞书客户端会明显卡顿甚至拒收。
- **`note` 组件在卡片 2.0 不可用**（ErrCode 200861 整卡被拒）：页脚只能用 `markdown` 灰字。
- **标题排在图片之前**：`img` 是唯一依赖外部资源的组件，图挂了标题和摘要照样能读。
- **`img` 组件不支持点击跳转**（飞书限制）：跳转靠标题链接 + 「点击查看原文」链接。
- **剥列表符号**：模型爱写「- 」「1. 」，卡片自己会加「- 」，不剥就变两层符号。
- **图床上传失败仍继续推**：文章没图和图床故障是两件事，后者不该导致整轮断更。
- **卡片失败不降级成纯文本**：失败即保持 pending，下一轮重发；降级会制造「重复推送」的假象。
- **中文是硬门槛，宁缺毋滥**：翻译层（`chinese_problems`）、入库前、上卡前共三道
  `cjk` 占比校验。模型吐英文、额度打满、漏配 key 时一律**丢弃而不是推原文**——
  推英文比不推更糟（群里看不懂，还占掉名额）。外文条目记 `rejected` 永久拉黑，
  避免每轮重复烧额度；纯环境故障（LLM 不可用）不落库，恢复后自动补推。

## 5. 配置来源

| 文件 | 内容 | 谁改 |
|---|---|---|
| `config/sources.yaml` | 新闻源清单（id/name/url/enabled） | 经常改，不含密钥 |
| `config/config.yaml` | 抓取参数、LLM、webhook、存储 | 偶尔改，含密钥 |
| `.env` / 环境变量 | 覆盖上面两项的敏感字段 | 部署时用 |

`config/settings.py` 是只读快照（路径常量 + 环境变量清单），给运维脚本/测试用。

## 6. 已知限制

- Ars Technica / The Next Web 整站人机验证（可见窗口也过不去）；The Verge 45s 稳定超时。
  三者都在 `sources.yaml` 里 `enabled: false`，网络/反爬方案就绪后一行打开。
- 每源最多取 2 篇正文、跨源上限 6 篇（正文页最耗时）。
- 单轮最多试 3 篇候选，避免在一次失败上烧光 token。
- 本机直连被目标站封，需走环境代理（`crawl.bypass_proxy: false` 表示不绕过代理）。
