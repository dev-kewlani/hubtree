import { describe, expect, mock, test } from 'claude-code/testing'

// A fake autopilot.py: the same JSON answers and gates (once per key, off switch), kept in memory.
function fakeHelper(clockNow: () => number) {
  const calls: string[][] = []
  const st = { limited: false, off: false, fired: new Set<string>(), dueMs: 0 }
  const run = (argv: readonly string[]) => {
    const a = [...argv]
    calls.push(a)
    const cmd = a[2]
    const plan = () => ({ ok: true, due: st.dueMs / 1000, key: 'K1', label: '03:32' })
    if (cmd === 'check') {
      if (!st.limited) return { code: 0, out: { ok: false, why: 'no limit record at the end' } }
      if (st.off) return { code: 0, out: { ok: false, why: 'off for this session' } }
      if (st.fired.has('K1')) return { code: 0, out: { ok: false, why: 'continued once already for this reset' } }
      return { code: 0, out: plan() }
    }
    if (cmd === 'fire') {
      if (st.off || !st.limited || clockNow() < st.dueMs || st.fired.has('K1'))
        return { code: 3, out: { ok: false, why: 'refused' } }
      st.fired.add('K1')
      return { code: 0, out: { ok: true, prompt: 'Continue: the usage limit has reset. Go on.', label: '03:32' } }
    }
    if (cmd === 'off' || cmd === 'on') {
      st.off = cmd === 'off'
      return { code: 0, out: { ok: true, msg: `Autopilot continue is ${cmd} for this session.` } }
    }
    return { code: 0, out: { ok: false, why: 'x', msg: 'No auto-continue planned.' } }
  }
  return { calls, st, run }
}

describe('autopilot', () => {
  test('one continue at the due time; none after an interrupt or with the off switch', async ($, on) => {
    const clock = mock.clock(on, { now: 1_000_000 })
    const fake = fakeHelper(() => clock.now())
    const submitted: { text: string; asUser?: boolean }[] = []
    const toasts: string[] = []
    on('process.run', async (_$, e) => {
      if (e.argv.some(a => a.endsWith('autopilot.py'))) {
        const r = fake.run(e.argv)
        return { value: { exitCode: r.code, stdout: JSON.stringify(r.out), stderr: '' } } as never
      }
      const out = { rows: [], counts: {} }
      return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '' } } as never
    })
    on('session.id', async () => ({ value: 'SID1' }) as never)
    on('session.messages', async () => ({ value: [] }) as never)
    on('ui.open', async () => ({ value: { isPlaced: true } }) as never)
    on('ui.toast', async (_$, e) => {
      toasts.push(String((e as { text?: string }).text ?? ''))
      return { value: undefined } as never
    })
    on('command.register', async () => ({ value: {} }) as never)
    on('session.start', async (_$, e) => ({ cwd: e.cwd }) as never)
    on('turn.complete', async (_$, e) => ({ text: e.answer }) as never)
    on('prompt.submit', async (_$, e) => {
      submitted.push({ text: e.text, asUser: (e.origin as { asUser?: boolean } | undefined)?.asUser })
      return { text: e.text } as never
    })
    const checks = () => fake.calls.filter(c => c[2] === 'check').length

    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    expect(fake.calls[0]).toEqual(expect.arrayContaining(['check', '--sid', 'SID1']))

    // 1. A turn ends at a usage limit: the pane shows the plan, and one prompt goes out at the due time.
    fake.st.limited = true
    fake.st.dueMs = clock.now() + 90_000
    await $.turn.complete({ answer: "You've hit your 5-hour limit · resets 3:30am", durationMs: 1, isAborted: false,
      turnId: 't1', reason: 'answer' } as never)
    await clock.advance(2000)
    const ui = await $.ui.mount({
      plugin: 'hubtree', surface: 'terminal', component: 'Pane', requestId: 'hubtree',
      props: { id: 'hubtree', title: 'hubtree' } as never,
    })
    expect((await ui.find({ type: 'Text', text: /auto/ }))?.text).toMatch(/auto-continue at 03:32/)
    await clock.advance(30_000)
    expect(submitted.length).toBe(0) // not due yet
    await clock.advance(90_000)
    expect(submitted).toEqual([{ text: 'Continue: the usage limit has reset. Go on.', asUser: true }])
    expect((await ui.find({ type: 'Text', text: /auto/ }))?.text).toMatch(/auto-continued at/)
    await clock.advance(10 * 60_000) // later ticks and the 5-minute re-check send nothing more
    expect(submitted.length).toBe(1)
    expect(fake.calls.filter(c => c[2] === 'fire').length).toBe(1)

    // 2. An interrupted turn: no check, no plan, no prompt.
    fake.st.fired.clear()
    const before = checks()
    await $.turn.complete({ answer: '', durationMs: 1, isAborted: true, turnId: 't2', reason: 'aborted' } as never)
    await clock.advance(2000)
    expect(checks()).toBe(before)
    fake.st.limited = false // the fake transcript no longer ends with a limit (the interrupt record follows it)
    await clock.advance(10 * 60_000)
    expect(submitted.length).toBe(1)

    // 3. The off switch: /autopilot off, then a limit turn gets no plan and no prompt.
    const r = await $.command.run({ command: 'autopilot', args: 'off' } as never)
    expect(fake.calls.some(c => c[2] === 'off')).toBe(true)
    expect(JSON.stringify(r)).toMatch(/off for this session/)
    fake.st.limited = true
    fake.st.dueMs = clock.now() + 60_000
    await $.turn.complete({ answer: 'limit', durationMs: 1, isAborted: false, turnId: 't3', reason: 'answer' } as never)
    await clock.advance(10 * 60_000)
    expect(submitted.length).toBe(1)
    expect((await ui.find({ type: 'Text', text: /auto/ }))?.text).toMatch(/autopilot off/)

    // 4. /autopilot on: the plan comes back, and the prompt goes out once.
    await $.command.run({ command: 'autopilot', args: 'on' } as never)
    await clock.advance(60_000)
    expect(submitted.length).toBe(2)
    expect(toasts.some(t => /auto-continue at 03:32/.test(t))).toBe(true)
    await ui.unmount()
  })
})
