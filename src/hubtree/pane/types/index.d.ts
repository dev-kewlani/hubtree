export type TreeRow = {
  depth: number
  kind: 'folder' | 'session'
  label: string
  name: string
  state?: 'tab' | 'work' | 'you' | 'idle' | 'stuck' | 'died' | 'closed' | 'queued' | 'paused'
  why?: string
  below?: string
  sid?: string
  me?: boolean
  fork?: boolean
  auto?: boolean
  root?: boolean
  parent?: string | null
  cwd?: string | null
  live_id?: string | null
  // swarm observe (swarm_obs.py): cost, budget, live step, agents, flags
  usd?: number
  usd_sub?: number
  tok?: number
  tok_sub?: number
  marks?: string
  budget?: { usd?: number; tokens?: number; pct: number; level: 'warn' | 'over' | null; text: string } | null
  activity?: string[]
  agent_summary?: string
  agent_lines?: string[]
  flags?: string[]
  flag_text?: string
  paused?: string | boolean | null
  // swarm control (swarm.py): child results, file conflicts, the queue
  results?: number
  result?: 'open' | 'accepted' | 'change' | 'rejected'
  result_id?: number
  conflict?: string
  wait?: string
}

declare module 'claude-code' {
  interface PluginState {
    hubtree: {
      rows: TreeRow[]
      selected: string
      note: string
      autopilot: string
      agentsOpen: boolean
    }
  }
}
