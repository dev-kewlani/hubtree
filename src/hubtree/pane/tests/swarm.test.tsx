import { describe, expect, mock, test } from 'claude-code/testing'

import type { TreeRow } from '../types'

// Swarm control in the hubtree pane (swarm.py).
describe('hubtree pane: swarm control', () => {
  test('swarm control: results, accept, change, conflict, queued row, paused bar', async ($, on) => {
    mock.clock(on)
    const ran: string[][] = []
    const rows: TreeRow[] = [
      { depth: 0, kind: 'folder', label: 'hub/', name: 'hub' },
      { depth: 1, kind: 'session', label: 'boss', name: 'hub/boss', state: 'paused', sid: 'A', me: true, results: 2,
        paused: true },
      { depth: 2, kind: 'session', label: 'kid~auto', name: 'hub/boss/kid~auto', state: 'idle', sid: 'B', parent: 'A',
        result: 'open', result_id: 7, conflict: 'x.py (with other~auto)' },
      { depth: 2, kind: 'session', label: 'next~auto', name: 'hub/boss/next~auto', state: 'queued', sid: 'q-1',
        parent: 'A', why: 'waits for kid~auto (released)', wait: 'waits for hub/boss/kid~auto (released)' },
    ]
    on('process.run', async (_$, e) => {
      ran.push([...e.argv])
      const swarm = e.argv.some(a => a.endsWith('swarm.py'))
      const out = swarm ? { ok: true } : { rows, counts: {} }
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
    expect(await ui.find({ type: 'Text', text: /PAUSED/ })).toBeDefined()
    expect((await ui.find({ key: 'row-A' }))?.text).toMatch(/PAUS boss.*2 results/)
    expect((await ui.find({ key: 'row-B' }))?.text).toMatch(/result ready.*CONFLICT x\.py/)
    expect((await ui.find({ key: 'row-q-1' }))?.text).toMatch(/wait next~auto.*waits for kid~auto/)
    await ui.press({ key: 'row-B' })
    expect(await ui.find({ type: 'Text', text: /CONFLICT: x\.py/ })).toBeDefined()
    await ui.press({ key: 'accept' })
    expect(ran.some(a => a.some(x => x.endsWith('swarm.py')) && a.includes('accept') && a.includes('7'))).toBe(true)
    expect(await ui.find({ type: 'Text', text: /Accepted: hub\/boss\/kid~auto is closed/ })).toBeDefined()
    await ui.input({ key: 'change', text: 'Add a test.' })
    expect(ran.some(a => a.includes('change') && a.includes('Add a test.'))).toBe(true)
    await ui.press({ key: 'row-q-1' })
    expect(await ui.find({ key: 'accept' })).toBeUndefined()
    await ui.press({ key: 'startnow' })
    expect(ran.some(a => a.includes('start-now') && a.includes('q-1'))).toBe(true)
    await ui.unmount()
  })
})
