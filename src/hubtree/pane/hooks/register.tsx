import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { TreeRow } from '../types'
import { registerAutopilot, startAutopilot } from './autopilot'
import { DIR, PY } from './paths'

// The session tree in a side pane.
// Data: `python tree_view.py --me <id>` prints the rows (registry + live sessions); refreshed every 5 s.
// /tree opens or closes the pane. Select a session row, then open it in a tab or send it a message.

const PANE = 'hubtree'
const SEP = DIR.includes('/') ? '/' : '\\'
const VIEW = DIR + SEP + 'tree_view.py'
const OPEN = DIR + SEP + 'open_session.py'
const OBS = DIR + SEP + 'swarm_obs.py' // budget raise
const SWARM = DIR + SEP + 'swarm.py' // queue, results
const MARK: Record<string, [string, string]> = {
  tab: ['tab ', 'green'],
  work: ['work', 'cyan'],
  you: ['YOU ', 'red'],
  idle: ['idle', 'yellow'],
  stuck: ['STCK', 'magenta'],
  died: ['died', 'red'],
  closed: ['done', 'gray'],
  queued: ['wait', 'blue'],
  paused: ['PAUS', 'yellow'],
}

// '  2 results  result ready  CONFLICT x.py (with b)': the swarm control tags of a row (swarm.py).
const ctl = (row: TreeRow): string =>
  (row.results ? `  ${row.results} result${row.results === 1 ? '' : 's'}` : '') +
  (row.result === 'open' ? '  result ready' : '') +
  (row.conflict ? `  CONFLICT ${row.conflict}` : '')

const mark = (row: TreeRow): [string, string] => MARK[row.state ?? 'closed'] ?? ['?   ', 'gray']

const usd = (x: number): string => (x < 1000 ? `$${x.toFixed(2)}` : `$${Math.round(x).toLocaleString('en-US')}`)

// ' $1.23' or ' $1.23 (tree $4.56)': the cost of a row, and of its sub-tree when that is larger.
const cost = (row: TreeRow): string => {
  if (row.usd === undefined) return ''
  const sub = row.usd_sub !== undefined && row.usd_sub - row.usd > 0.005 ? ` (tree ${usd(row.usd_sub)})` : ''
  return `  ${usd(row.usd)}${sub}`
}

const rows = atom({ plugin: 'hubtree', key: 'rows' } as const, [])
const selected = atom({ plugin: 'hubtree', key: 'selected' } as const, '')
const note = atom({ plugin: 'hubtree', key: 'note' } as const, '')
const apLine = atom({ plugin: 'hubtree', key: 'autopilot' } as const, '') // written by hooks/autopilot.tsx
const agentsOpen = atom({ plugin: 'hubtree', key: 'agentsOpen' } as const, false)

export const register: Register = on => {
  let me = ''
  let isOpen = false
  const seen: Record<string, string> = {}

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!e.isInteractive) return started
    me = await $.session.id()
    await $.command.register({ name: 'tree', description: 'Open or close the session tree pane' })

    const refresh = async () => {
      try {
        const r = await $.process.run([PY, VIEW, '--me', me], { timeoutMs: 30000 })
        if (r.exitCode !== 0) return
        const list: TreeRow[] = JSON.parse(r.stdout).rows
        for (const row of list) {
          // Alert only for this session's own children, so one alert does not show in every session.
          if (row.kind !== 'session' || !row.sid || row.parent !== me) continue
          const before = seen[row.sid]
          if (before && before !== row.state) {
            if (row.state === 'you') void $.ui.notify(`${row.name} needs you`, { title: 'hubtree' })
            else if (row.state === 'stuck' || row.state === 'died')
              void $.ui.notify(`${row.name} is ${row.state}: ${row.why ?? ''}`, { title: 'hubtree' })
            else if (before === 'work' && row.state === 'idle') void $.ui.toast(`${row.name} finished`)
          }
          seen[row.sid] = row.state ?? ''
        }
        await update($, rows, () => list)
      } catch {
        // a failed refresh keeps the last tree
      }
    }
    await startAutopilot($)
    await refresh()
    $.clock.every(5000, () => void refresh())
    const opened = await $.ui.open({ id: PANE, title: 'hubtree' })
    isOpen = opened.isPlaced
    return started
  })
  registerAutopilot(on)

  on('command.run', { command: 'tree' }, async $ => {
    if (isOpen) {
      await $.ui.close({ id: PANE })
      isOpen = false
      return { text: 'The tree pane closed. Run /tree to open it again.' }
    }
    const opened = await $.ui.open({ id: PANE, title: 'hubtree' })
    isOpen = true
    return { text: opened.isPlaced ? 'The tree pane opened.' : 'The tree pane waits for a wider terminal.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Input = 'Input' in els ? els.Input : undefined
    const list = await read($, rows)
    const sel = await read($, selected)
    const msg = await read($, note)
    const ap = await read($, apLine)
    const showAgents = await read($, agentsOpen)
    const mine = list.find(r => r.me)
    const over = mine?.budget?.level === 'over'
    const room = Math.max(4, (e.viewport?.rows ?? 30) - 9)
    const pick = list.find(r => r.sid === sel)

    const openTab = async (row: TreeRow) => {
      // A live background row has a live_id (attach). A live tab row (tab, or stuck or you in a tab) has none.
      if (row.state === 'tab' || (!row.live_id && (row.state === 'stuck' || row.state === 'you'))) {
        await update($, note, () => `${row.name} is open in another tab already.`)
        return
      }
      const argv = [PY, OPEN, '--sid', row.sid ?? '', '--cwd', row.cwd || '', '--title', row.name]
      if (row.live_id) argv.push('--attach', row.live_id)
      const r = await $.process.run(argv, { timeoutMs: 60000 })
      await update($, note, () => (r.exitCode === 0 ? `Opened ${row.name} in ${r.stdout.trim()}.` : `Open failed: ${r.stderr.slice(-160)}`))
    }

    const raise = async (row: TreeRow) => {
      const r = await $.process.run([PY, OBS, 'budget', '--sid', row.sid ?? '', '--mult', '2', '--by', 'pane'], { timeoutMs: 30000 })
      await update($, note, () => (r.exitCode === 0 ? `Budget of ${row.name} raised x2.` : `Raise failed: ${r.stderr.slice(-160)}`))
    }

    // swarm.py: accept, change, reject a child result; start or cancel a queued child. Prints one JSON line.
    const swarm = async (args: string[], okText: string) => {
      const r = await $.process.run([PY, SWARM, ...args], { timeoutMs: 120000 })
      let res: { ok?: boolean; error?: string } = {}
      try {
        res = JSON.parse(r.stdout.trim().split('\n').pop() ?? '{}')
      } catch {
        res = { ok: false, error: r.stderr.slice(-160) }
      }
      await update($, note, () => (res.ok ? okText : `Not done: ${(res.error ?? '').slice(0, 200)}`))
    }

    const send = async (text: string) => {
      if (!pick?.sid || !text.trim()) return
      const sent = await $.session.send({ to: { sessionId: pick.sid }, text })
      await update($, note, () => (sent ? `Sent to ${pick.name}.` : `Not sent to ${pick.name}.`))
    }

    return (
      <Box flexDirection="column">
        <Text dimColor>tab=open  work=running  YOU=needs you  STCK=no write for its limit  died  idle  done</Text>
        {over && (
          <Text key="overbudget" color="red" bold inverse>
            {` OVER BUDGET: ${mine?.budget?.text ?? ''}. No auto-continue. Select this row and press b to raise it. `}
          </Text>
        )}
        {mine?.paused && mine?.state !== 'queued' ? (
          <Text key="paused" color="yellow" bold inverse>
            {' PAUSED (group action): finish the current step, then wait for a resume. '}
          </Text>
        ) : null}
        {ap !== '' && <Text key="autopilot" color="yellow">{ap}</Text>}
        {list.slice(0, room).map(row =>
          row.kind === 'folder' ? (
            <Text bold>{'  '.repeat(row.depth)}{row.label}{cost(row)}{row.below ? `  (${row.below})` : ''}</Text>
          ) : (
            <Button key={`row-${row.sid}`} plain onPress={() => void update($, selected, () => row.sid ?? '')}>
              {'  '.repeat(row.depth)}
              <Text color={mark(row)[1]}>{mark(row)[0]}</Text>
              {' '}{row.label}{row.root ? ' (root)' : ''}{row.fork ? ' (fork)' : ''}{row.me ? '  <- this session' : ''}
              {cost(row)}{row.below ? `  (${row.below})` : ''}{row.why ? `  ${row.why}` : ''}
              {row.marks ? <Text color={row.budget?.level === 'over' ? 'red' : 'magenta'}>{`  ${row.marks}`}</Text> : ''}
              {ctl(row) ? <Text color={row.conflict ? 'red' : 'green'}>{ctl(row)}</Text> : ''}
              {row.sid === sel ? '  *' : ''}
            </Button>
          ),
        )}
        {list.length > room && <Text dimColor>... {list.length - room} more rows: see hubtree</Text>}
        {pick && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>{pick.name}</Text>
            {pick.budget && (
              <Text key="budget" color={pick.budget.level === 'over' ? 'red' : pick.budget.level ? 'magenta' : undefined}>
                {pick.budget.text}
              </Text>
            )}
            {pick.flag_text ? <Text key="flags" color="magenta">{pick.flag_text}</Text> : null}
            {(pick.activity ?? []).map((line, i) => (
              <Text key={`act-${i}`} dimColor={i > 0}>{line}</Text>
            ))}
            {pick.agent_summary ? <Text key="agents">{`agents: ${pick.agent_summary.replace(/^\+/, '')}`}</Text> : null}
            {showAgents && (pick.agent_lines ?? []).map((line, i) => (
              <Text key={`ag-${i}`} dimColor>{line}</Text>
            ))}
            <Box flexDirection="row">
              <Button key="open" hotkey="o" onPress={() => void openTab(pick)}>Open in tab</Button>
              <Button key="clear" hotkey="c" onPress={() => void update($, selected, () => '')}>Clear</Button>
              {pick.agent_summary ? (
                <Button key="agentsbtn" hotkey="e" onPress={() => void update($, agentsOpen, v => !v)}>
                  {showAgents ? 'Hide agents' : 'Show agents'}
                </Button>
              ) : null}
              {pick.budget ? <Button key="raise" hotkey="b" onPress={() => void raise(pick)}>Raise budget x2</Button> : null}
              {pick.result === 'open' && pick.result_id ? (
                <Button key="accept" hotkey="a"
                  onPress={() => void swarm(['accept', String(pick.result_id)], `Accepted: ${pick.name} is closed.`)}>
                  Accept result
                </Button>
              ) : null}
              {pick.result === 'open' && pick.result_id ? (
                <Button key="reject" hotkey="x"
                  onPress={() => void swarm(['reject', String(pick.result_id)], `Rejected the result of ${pick.name}.`)}>
                  Reject
                </Button>
              ) : null}
              {pick.state === 'queued' ? (
                <Button key="startnow" hotkey="g" onPress={() => void swarm(['start-now', pick.sid ?? ''], `Started ${pick.name}.`)}>
                  Start now
                </Button>
              ) : null}
              {pick.state === 'queued' ? (
                <Button key="cancelq" hotkey="x" onPress={() => void swarm(['cancel', pick.sid ?? ''], `Cancelled ${pick.name}.`)}>
                  Cancel
                </Button>
              ) : null}
            </Box>
            {pick.wait ? <Text key="wait" color="blue">{pick.wait}</Text> : null}
            {pick.conflict ? <Text key="conflict" color="red">{`CONFLICT: ${pick.conflict}`}</Text> : null}
            {Input && pick.result === 'open' && pick.result_id ? (
              <Input key="change" placeholder={`ask ${pick.label} for a change (your words)`} submitLabel="Ask"
                onSubmit={(v: string) =>
                  void (v.trim() && swarm(['change', String(pick.result_id), '--words', v], `Asked ${pick.name} for a change.`))} />
            ) : null}
            {Input && (
              <Input key="msg" placeholder={`message to ${pick.label}`} submitLabel="Send"
                onSubmit={(v: string) => void send(v)} />
            )}
          </Box>
        )}
        {msg !== '' && <Text dimColor>{msg}</Text>}
      </Box>
    )
  })
}
