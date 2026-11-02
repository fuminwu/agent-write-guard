# agent-write-guard

装在 agent **落盘缝**上的守卫。写之前否决会踩坑的写法，写之后复核实际结果。

> 一句话：靠「记得检查」的防线会失效，靠「代码拦截」的防线不会。

```
$ python guard.py selftest
SELFTEST PASS 15/15
```

## 它拦什么

| 事故 | 典型场景 | 检测方式 |
|---|---|---|
| **整篇重写** | 只想改两行，结果 400 行全被重写，diff 一片红、看不出改了什么 | 基准 ≥40 行且 (改+删) 与 (改+增) 都 ≥90% |
| **行尾翻转** | Windows 上 CRLF→LF，`git diff` 整文件都变了 | 对比写前快照的 eol |
| **BOM / 编码丢失** | `.ps1`、`.csv` 里的中文变 ANSI，文件当场语法错 | 写前有 BOM、写后没有 → 拦 |
| **顺手删（吞行）** | 改一处却少了一行；Obsidian 的 `[[双链]]` 最常被吞 | 双链计数下降（只对本来就有双链的文件生效） |
| **shell 覆盖直写** | `echo hi > notes.md` 绕过 edit 工具，把受保护文件盖掉 | 重定向/`Out-File`/`Set-Content` 命中受保护扩展名 |
| **写后变坏** | 局部替换（edit）时全文未知，PRE 放行 → POST 复核实际结果 | 写后重新 probe，比对 eol/BOM/UTF-8/双链/整篇 |

全部判定基于**写前快照 vs 写后实际内容**，不看模型说了什么。

## 为什么不是「提醒自己注意」

我们在这套东西上踩过的坑，每一条都是真实事故：

1. **CRLF 被翻转**：一次「整篇重写」，400 行的文档在 git 里全变红。事后能修，但那次 review 谁也没看出改的是哪两行。
2. **`.ps1` 的 BOM 丢了**：中文全变 ANSI，脚本报 5 处语法错，排查了半天才发现是编码。
3. **索引条目被吞**：改一个章节标题，连带少了一行 `[[链接]]`，三天后才发现有一页成了孤儿。
4. **事故之后又犯一次**：把断言写成「手动跑才生效」，于是「忘了跑」= 没防线。

第 4 条是这套东西存在的真正原因：**任何需要人记得执行的安全措施，最终都会失效。**

## 安装

需要 Python 3.8+，无第三方依赖。

### 方式 A：DSH 原生插件（推荐，走 `fs` 缝）

DSH 里有一个值得先知道的结论：

> `@deepseek-ai/dsh-hooks-claude-code` 监听的是 `tools/pre-execute`，但那一版运行期**没有任何地方 emit 这条缝**。
> 桥挂载成功、配置解析成功、日志里 `config OK` —— 却 0 条钩子事件。桥没配错，是踩在空缝上。

所以 `dsh/index.js` 直接挂到真在派发的三条缝上：`fs/write-intent`、`fs/edit-intent`、`fs/observed`。

1. 把 `dsh/` 整个目录放到 `<profile>/plugins/dsh-write-guard/`
2. 把 `dsh/cordis-snippet.yml` 的段落插进 `<profile>/cordis.patch.yml`
3. 重启会话，`write-guard-boot.log` 里应出现 `applied; python=...`

用 `{ prepend: true }` 抢最外层，检查完仍调 `next()` —— **不削弱** DSH 自带的「读前写守」。

### 方式 B：Claude Code 方言的 hook

DSH 的 hook 桥、Claude Code 的 hooks 用的是同一套方言（stdin JSON + exit 2 拦截）。把 `examples/claude-code-settings.json` 合进你的 `settings.json` 即可。

## 用法

```bash
python guard.py selftest     # 15 条断言，0=全过 / 3=有失败
python guard.py status       # 监控根、日志、例外通道状态
python guard.py allow --tag "整篇重排目录结构" --minutes 15
```

### 例外通道

真需要整篇重写时，**显式开一次、限时、留痕**：

```
python guard.py allow --tag "原因" --minutes 15
```

每次被使用都会写一行 `OVERRIDE-USED tag=... file=...`。事后能数出「谁在什么时候走了后门」——
例外通道的价值不在放行，在于**放行这件事本身是可审计的**。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GUARD_ROOTS` | 会话 cwd | 监控根，`os.pathsep` 分隔；多个根用 `;`（Windows）/`:` |
| `GUARD_STATE` | `<脚本目录>/.guard_state` | 快照与 override 存放处 |
| `GUARD_LOG` | `<脚本目录>/guard.log` | 审计日志 |
| `GUARD_SKIP` | 空 | 额外跳过片段，`os.pathsep` 分隔 |
| `GUARD_DISABLE` | — | 置 `1` 整体放行（应急开关） |

默认跳过：`_backup_*`、`*.bak`、`__pycache__`、`node_modules`、`.git`、`.dsh-uploads`。

**没有内置任何业务规则。** 判决口径、业务白名单那类东西属于你的项目，不该塞进通用工具 ——
需要就 fork 出 `pre_rule_violations()` 加自己的断言。

## 设计取舍

- **失败即放行**：守卫自己抛异常时放行并记日志，绝不因为工具坏了把正常写入卡死。日志里能数出这类异常。
- **只判「本次新写的」**：不翻旧账。文件里原有的毛病不拦，免得一开就满屏红。
- **快照 6 小时过期**：拿很久以前的快照当基准会误伤，过期即视为无基准。
- **POST 失败不打断本次结果**：DSH 适配层把「写坏了」记账，下次碰同一文件时当面拦下 —— 当场炸掉会更让人不知所措。

## 边界（诚实版）

- 它防的是**格式与结构事故**，不是内容错误。写错了事实它不管。
- 局部替换（`edit`）在写前拿不到全文，只能写后复核 —— 所以坏结果会晚一个回合被发现。
- 整篇重写的判定是启发式（90%/40 行）。改结构的时候会误伤，用 `allow`。
- `shell` 检测是正则粗筛，只认覆盖式重定向 + 文档类扩展名，不追求覆盖所有写法（那会天天误报）。

## License

MIT
