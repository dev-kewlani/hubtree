import { atom, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import { DIR, PY } from './paths'

// Auto-continue after a usage limit.
// At each main turn end (and at session start), autopilot.py `check` reads this session's transcript.
// When the turn ended at a usage limit, the pane shows "auto-continue at 03:32" and a 30 s tick waits for that time.
// Then autopilot.py `fire` runs every gate (kill switch, hubtree.toml, /autopilot off, once per reset, no user prompt since)
// and the mod submits one prompt as the user's words. The same flag file keeps a session from continuing twice.

const SEP = DIR.includes('/') ? '/' : '\\'
const AP = DIR + SEP + 'autopilot.py'
const TICK_MS = 30000
const RECHECK_TICKS = 10 // with no plan, check again every 5 minutes (a turn end that the hook did not hear)
const AFTER_TURN_MS = 1500 // the limit record and the turn end reach the transcript a moment after turn.complete

type Plan = { due: number; key: string; label: string }
type Answer = { ok?: boolean; due?: number; key?: string; label?: string; why?: string; prompt?: string; msg?: string }

const apLine = atom({ plugin: 'hubtree', key: 'autopilot' } as const, '')

let me = ''
let plan: Plan | undefined
let firing = false
let idleTicks = 0

function hm(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function helper($: EngineInterface, args: string[]): Promise<{ code: number; j: Answer }> {
  const r = await $.process.run([PY, AP, ...args, '--sid', me], { timeoutMs: 30000 })
  let j: Answer = {}
  try {
    j = JSON.parse(r.stdout)
  } catch {
    j = { ok: false, why: `autopilot.py: ${r.stderr.trim().slice(-120)}` }
  }
  return { code: r.exitCode, j }
}

async function checkLimit($: EngineInterface): Promise<void> {
  if (!me) return
  try {
    const { j } = await helper($, ['check'])
    if (j.ok && j.due && j.key) {
      const isNew = plan?.key !== j.key
      plan = { due: j.due * 1000, key: j.key, label: j.label ?? '' }
      await update($, apLine, () => `auto-continue at ${plan?.label}`)
      if (isNew) void $.ui.toast(`Usage limit: auto-continue at ${j.label}`)
    } else if (plan) {
      plan = undefined
      await update($, apLine, () => '')
    }
  } catch {
    // a failed check keeps the last plan; fire checks everything again
  }
}

async function tick($: EngineInterface): Promise<void> {
  if (firing) return
  if (!plan) {
    idleTicks += 1
    if (idleTicks >= RECHECK_TICKS) {
      idleTicks = 0
      await checkLimit($)
    }
    return
  }
  const now = await $.clock.now()
  if (now < plan.due) return
  firing = true
  const p = plan
  plan = undefined
  try {
    const { code, j } = await helper($, ['fire', '--key', p.key, '--by', 'mod'])
    if (code === 0 && j.ok && j.prompt) {
      void $.prompt.submit({ text: j.prompt, asUser: true }) // waits for an idle session: never awaited here
      await update($, apLine, () => `auto-continued at ${hm(now)}`)
    } else {
      await update($, apLine, () => `auto-continue skipped: ${j.why ?? 'autopilot.py failed'}`)
    }
  } catch (err) {
    await update($, apLine, () => `auto-continue failed: ${String(err).slice(0, 120)}`)
  } finally {
    firing = false
  }
}

// The session start of the hubtree module calls this (interactive and background sessions alike).
export async function startAutopilot($: EngineInterface): Promise<void> {
  me = await $.session.id()
  await $.command.register({ name: 'autopilot', description: 'Auto-continue after a usage limit: on, off or status' })
  await checkLimit($) // a resumed session whose last turn ended at a limit
  $.clock.every(TICK_MS, () => void tick($))
}

// The turn end and prompt hooks of registerAutopilot call these two.
export async function autopilotTurnEnd($: EngineInterface, e: { agentId?: string; isAborted: boolean }): Promise<void> {
  if (e.agentId !== undefined || !me) return
  if (e.isAborted) {
    plan = undefined
    await update($, apLine, () => '')
    return
  }
  idleTicks = 0
  $.clock.after(AFTER_TURN_MS, () => void checkLimit($))
}

// A prompt from the user replaces any plan; the fire gate also refuses after a user prompt.
export async function autopilotPrompt($: EngineInterface, e: { text: string; command?: unknown; isMeta?: boolean }): Promise<void> {
  if (plan && !e.command && !e.isMeta && !e.text.startsWith('Continue: the usage limit has reset')) {
    plan = undefined
    await update($, apLine, () => '')
  }
}

export function registerAutopilot(on: On): void {
  on('turn.complete', async ($, e, next) => {
    const out = await next(e)
    await autopilotTurnEnd($, e)
    return out
  })
  on('prompt.submit', async ($, e, next) => {
    await autopilotPrompt($, e as unknown as { text: string; command?: unknown; isMeta?: boolean })
    return next(e)
  })
  on('command.run', { command: 'autopilot' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    const cmd = arg === 'off' || arg === 'on' ? arg : 'status'
    const { j } = await helper($, [cmd])
    if (cmd === 'off') {
      plan = undefined
      await update($, apLine, () => 'autopilot off for this session')
    } else if (cmd === 'on') {
      await update($, apLine, () => '')
      await checkLimit($)
    }
    return { text: j.msg ?? j.why ?? 'autopilot.py gave no answer.' }
  })
}
