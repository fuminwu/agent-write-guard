---
name: write-guard
description: 写入/编辑文件被 write-guard 拦下时（WRITE_GUARD_BLOCK / WRITE_GUARD_PENDING），或需要整篇重写、批量改文档、判定误报时，按此处置。
---

# 被 write-guard 拦下怎么办

拦截信息里有文件名和具体断言。**先修，再重试**；`allow` 是最后手段。

## 按断言处置

| 断言 | 正确做法 |
|---|---|
| 行尾 CRLF→LF | 保持原行尾。写 `\r\n`，不要写 `\n` |
| 会写入 MIXED 行尾 | 全文统一成原有行尾，别只改一段 |
| BOM 丢失 | 首字符写成 `\ufeff`，或改用 edit 定点改（edit 不改编码） |
| `[[双链]]` 计数下降 | 用 edit 只替换目标片段，别整篇重写 |
| 【整篇重写】 | 换 edit 定点改；确实要重排结构 → 开例外通道 |
| shell 覆盖直写 | 改用 write/edit 工具，别用 `>` 盖文件 |

**通用原则：用 `edit` 定点改，不要用 `write` 整篇重写。** 九成拦截都是这一条惹的。

## 确实需要整篇重写

```bash
python guard.py allow --tag "重排目录结构，需整篇重写" --minutes 15
```

- `--minutes` 给到刚好够用（默认 15）
- 每次被使用都会记 `OVERRIDE-USED tag=... file=...`，事后可被审计
- 用完不必手动关，到期自动失效

## 拦错了（误报）

1. 先确认是不是可以换 `edit` 绕过 —— 多数「误报」其实是整篇重写。
2. 真是启发式误伤：开一次 `allow`，并在 tag 里写清原因。
3. 长期误伤某类文件：加进 `GUARD_SKIP`（`os.pathsep` 分隔的片段），或把它移出 `GUARD_ROOTS`。
4. 应急全关：`GUARD_DISABLE=1`。**这是给自己挖坑的开关**，用完记得撤。

## 不要做

- 不要为了过检查去改 `guard.py` 自己。
- 不要反复 `allow` 而不修根因 —— 日志里连续几行 OVERRIDE 就是「这人在绕过防线」的证据。
- 不要在 `allow` 期间批量重写多个文件。一次一个，改完让守卫重新验一遍。
