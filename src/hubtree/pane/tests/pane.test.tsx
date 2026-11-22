import { describe, expect, mock, test } from 'claude-code/testing'

import type { TreeRow } from '../types'

const SAMPLE: TreeRow[] = [
  { depth: 0, kind: 'folder', label: 'hub/', name: 'hub' },
  { depth: 1, kind: 'session', label: 'hub-setup', name: 'hub/hub-setup', state: 'tab', sid: 'A', me: true },
  { depth: 2, kind: 'session', label: 'tree-test~auto', name: 'hub/hub-setup/tree-test~auto', state: 'you', sid: 'B',
    parent: 'A', auto: true, live_id: 'b1' },
  { depth: 2, kind: 'session', label: 'hung-child', name: 'hub/hub-setup/hung-child', state: 'stuck', sid: 'C',
    parent: 'A', why: 'Bash, 34 min with no write (limit 20)' },
]

describe('hubtree pane', () => {
  test('draws the tree, marks this session, and shows actions for a selected row', async ($, on) => {
    mock.clock(on)
    const ran: string[][] = []
    on('process.run', async (_$, e) => {
      ran.push([...e.argv])
      const out = { rows: SAMPLE, counts: {} }
      return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '' } } as never
    })
    on('session.id', async () => ({ value: 'A' }) as never)
    on('ui.open', async () => ({ value: { isPlaced: true } }) as never)
    on('command.register', async () => ({ value: {} }) as never)
    on('session.start', async (_$, e) => ({ cwd: e.cwd }) as never)

    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    expect(ran[0]?.[ran[0].length - 1]).toBe('A') // the helper got this session's id

    const ui = await $.ui.mount({
      plugin: 'hubtree', surface: 'terminal', component: 'Pane', requestId: 'hubtree',
      props: { id: 'hubtree', title: 'hubtree' } as never,
    })
    expect(await ui.find({ type: 'Text', text: /hub\// })).toBeDefined()
    expect((await ui.find({ key: 'row-A' }))?.text).toMatch(/this session/)
    expect((await ui.find({ key: 'row-B' }))?.text).toMatch(/YOU/)
    expect((await ui.find({ key: 'row-C' }))?.text).toMatch(/STCK hung-child.*Bash, 34 min with no write/)
    expect(await ui.find({ key: 'open' })).toBeUndefined()
    await ui.press({ key: 'row-B' })
    expect(await ui.find({ key: 'open' })).toBeDefined()
    expect(await ui.find({ key: 'msg' })).toBeDefined()
    await ui.unmount()
  })

  test('shows cost, the over-budget bar, the live step, agents and the budget raise', async ($, on) => {
    mock.clock(on)
    const ran: string[][] = []
    const rows: TreeRow[] = [
      { depth: 0, kind: 'folder', label: 'hub/', name: 'hub', usd: 3.5 },
      { depth: 1, kind: 'session', label: 'boss', name: 'hub/boss', state: 'tab', sid: 'A', me: true, usd: 1, usd_sub: 3.5,
        marks: 'OVER BUDGET 120%', budget: { usd: 1, pct: 120, level: 'over', text: 'budget 120%: $1.20 of $1.00' } },
      { depth: 2, kind: 'session', label: 'worker', name: 'hub/boss/worker', state: 'work', sid: 'B', parent: 'A',
        usd: 2.5, usd_sub: 2.5, marks: 'LOOP', flag_text: 'loop: Bash 6 times with the same input (echo hi)',
        activity: ['step: running Bash: echo hi, 3 s ago | last write 1 s ago | turns 2 | tool calls 9', 'tool Bash: echo hi'],
        agent_summary: '+3 agents, 1 failed', agent_lines: ['done: scout A', 'failed (limit): scout B', 'running: scout C'],
        budget: { usd: 5, pct: 50, level: null, text: 'budget 50%: $2.50 of $5.00' } },
    ]
    on('process.run', async (_$, e) => {
      ran.push([...e.argv])
      const out = { rows, counts: {} }
      return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '' } } as never
    })
    on('session.id', async () => ({ value: 'A' }) as never)
    on('ui.open', async () => ({ value: { isPlaced: true } }) as never)
    on('command.register', async () => ({ value: {} }) as never)
    on('session.start', async (_$, e) => ({ cwd: e.cwd }) as never)
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

    const ui = await $.ui.mount({
      plugin: 'hubtree', surface: 'terminal', component: 'Pane', requestId: 'hubtree',
      props: { id: 'hubtree', title: 'hubtree' } as never,
    })
    expect((await ui.find({ type: 'Text', text: /OVER BUDGET:/ }))?.text).toMatch(/OVER BUDGET: budget 120%.*press b/)
    expect((await ui.find({ key: 'row-A' }))?.text).toMatch(/\$1\.00 \(tree \$3\.50\)/)
    expect((await ui.find({ key: 'row-B' }))?.text).toMatch(/\$2\.50.*LOOP/)
    await ui.press({ key: 'row-B' })
    expect(await ui.find({ type: 'Text', text: /loop: Bash 6 times/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /step: running Bash: echo hi/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /agents: 3 agents, 1 failed/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /scout B/ })).toBeUndefined() // collapsed by default
    await ui.press({ key: 'agentsbtn' })
    expect(await ui.find({ type: 'Text', text: /failed \(limit\): scout B/ })).toBeDefined()
    await ui.press({ key: 'raise' })
    expect(ran.some(a => a.some(x => x.endsWith('swarm_obs.py')) && a.includes('--mult') && a.includes('B'))).toBe(true)
    await ui.unmount()
  })
})
