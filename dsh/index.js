// agent-write-guard · DSH 原生插件适配层（走 fs 缝）
//
// 【为什么需要这一层 —— 这段取证结论请保留，能省掉后来者一整天的排查】
//   ① `@deepseek-ai/dsh-hooks-claude-code` 监听的是 `tools/pre-execute`；
//   ② 当时那一版运行期**没有任何地方 emit 这条缝**（只有监听者、没有派发者）；
//   ③ 实测：桥挂载成功、配置解析成功、日志里 config OK —— 却 0 条钩子事件。
//   → 那条缝是"登记了签名、但没人派发"的空缝。桥没配错，是踩在空缝上。
//
// 【所以改走哪条缝】`fs/write-intent` / `fs/edit-intent`（`dsh-tool-fs` 与 str-replace-editor
//   真在派发），以及写后触发的 `fs/observed`。
//   cordis 的 waterfall 语义：监听者按注册顺序**由外向内**跑，不调 next() 就截断整条链 ——
//   所以本插件用 `{ prepend: true }` 抢到最外层，检查完再 `next()`，把决策权交还给既有防线。
//
// 【规则出处单一】判定逻辑全在 guard.py，本插件只是「缝的转接头」：
//   合成 payload → 起子进程 → exit 2 即拦。不复制任何规则。
import { spawn } from 'node:child_process'
import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname, join, isAbsolute } from 'node:path'
import { FsError } from '@deepseek-ai/dsh-fs'

const name = 'write-guard'

function makeTrace(logPath) {
  if (!logPath) return () => {}
  let dir
  try {
    dir = dirname(logPath)
    mkdirSync(dir, { recursive: true })
  } catch {
    return () => {}
  }
  return (line) => {
    try {
      appendFileSync(logPath, `${new Date().toISOString()} | [write-guard] ${line}\n`)
    } catch {
      /* 留痕失败不影响判定 */
    }
  }
}

/** 起子进程跑 Python 守卫：stdin 喂 UTF-8 JSON，exit 2 = 拦 */
function runGuard(python, guard, payload, timeoutMs) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(python, [guard], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    } catch (error) {
      resolve({ code: 125, stderr: String(error) })
      return
    }
    let err = ''
    let done = false
    const finish = (r) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve(r)
    }
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* ignore */
      }
      finish({ code: 124, stderr: 'write-guard: 守卫子进程超时' })
    }, timeoutMs)
    child.stderr.on('data', (d) => {
      err += d.toString('utf8')
    })
    child.on('error', (e) => finish({ code: 125, stderr: String(e) }))
    child.on('close', (code) => finish({ code, stderr: err }))
    child.stdin.on('error', () => {})
    child.stdin.end(Buffer.from(JSON.stringify(payload), 'utf8'))
  })
}

function apply(ctx, config) {
  const python = config?.python ?? 'python'
  const guard = config?.guard
  if (!guard) throw new Error('write-guard: 必须配置 guard（guard.py 的绝对路径）')
  const guardAbs = isAbsolute(guard) ? guard : join(process.cwd(), guard)
  const cwd = config?.cwd ?? process.cwd()
  const timeoutMs = Number(config?.timeoutMs ?? 20000)
  const trace = makeTrace(config?.log === false ? null : (config?.log ?? join(dirname(guardAbs), 'write-guard-boot.log')))
  trace(`applied; python=${python} guard=${guardAbs} cwd=${cwd}`)

  // 写后没通过的账：挂在这里，等下一次碰同一文件时当众翻出来（写坏了不能只写日志）
  const pending = new Map()

  const pre = async (target, actor, next) => {
    const decision = await next() // 先让既有决策者走完 → 不削弱原有的「读前写守」
    const toolName = actor?.name
    if (!toolName) return decision
    const path = target?.displayPath ?? actor?.arguments?.file_path ?? actor?.arguments?.path
    if (path && pending.has(path)) {
      const reason = pending.get(path)
      pending.delete(path)
      throw new FsError(`[write-guard] 上次写入未通过复核，先修再动它：\n${reason}`, 'WRITE_GUARD_PENDING')
    }
    const r = await runGuard(python, guardAbs, {
      hook_event_name: 'PreToolUse',
      session_id: 'fs-seam',
      tool_name: toolName,
      tool_input: actor?.arguments ?? {},
      cwd,
    }, timeoutMs)
    trace(`PRE ${toolName} ${path} → exit=${r.code}`)
    if (r.code === 2) {
      const reason = String(r.stderr || '').trim() || '[write-guard] 落盘被拦'
      throw new FsError(reason, 'WRITE_GUARD_BLOCK')
    }
    if (r.code !== 0) trace(`PRE 守卫异常 exit=${r.code}: ${String(r.stderr).slice(0, 300)}`)
    return decision
  }

  ctx.on('fs/write-intent', pre, { prepend: true })
  ctx.on('fs/edit-intent', pre, { prepend: true })

  ctx.on('fs/observed', (target, observation, actor) => {
    const toolName = actor?.name
    // 读文件也会触发 fs/observed，必须过滤
    if (toolName !== 'write' && toolName !== 'edit' && toolName !== 'str_replace_editor') return
    const path = target?.displayPath
    runGuard(python, guardAbs, {
      hook_event_name: 'PostToolUse',
      session_id: 'fs-seam',
      tool_name: toolName,
      tool_input: actor?.arguments ?? {},
      cwd,
    }, timeoutMs).then((r) => {
      trace(`POST ${toolName} ${path} → exit=${r.code}`)
      if (r.code === 2) {
        const reason = String(r.stderr || '').trim() || '[write-guard·POST] 刚写坏一处'
        pending.set(path, reason) // 不打断本次工具结果，但下次碰它必被拦下
        trace(`POST-FAIL 已记账，下次触碰该文件时当面拦下：${path}`)
      }
      if (r.code !== 0 && r.code !== 2) trace(`POST 守卫异常 exit=${r.code}: ${String(r.stderr).slice(0, 300)}`)
    })
  })
}

export { apply, name }
