import { AGENT_ROLE_ORDER, type Plan, type RunEvent, type Task } from './types'
import { completedTaskIds } from './fixtures'
import { formatInt } from './format'

export interface PlanBoardProps {
  plan: Plan | null
  events: readonly RunEvent[]
  /** Current iteration, used to say which wave is running. */
  iter?: number | undefined
}

interface RoleGroup {
  role: Task['role']
  tasks: Task[]
}

/** Group tasks by owning role, in the fixed agent order. */
export function groupByRole(tasks: readonly Task[]): RoleGroup[] {
  return AGENT_ROLE_ORDER.map((role) => ({
    role,
    tasks: tasks.filter((task) => task.role === role),
  })).filter((group) => group.tasks.length > 0)
}

function taskState(task: Task, done: Set<string>): 'done' | 'ready' | 'blocked' {
  if (done.has(task.id)) return 'done'
  const deps = task.depends_on ?? []
  return deps.every((id) => done.has(id)) ? 'ready' : 'blocked'
}

/**
 * The plan board: every task the Planner wrote, grouped by the role that owns it.
 *
 * A task is done when the ledger/SSE log carries a `task_completed` for it, so the
 * board is derived from the event stream rather than from a second status endpoint.
 */
export function PlanBoard({ plan, events, iter }: PlanBoardProps): React.JSX.Element {
  if (!plan) {
    return (
      <div className="empty-state" role="status">
        No plan yet — the Planner writes one at the start of the first iteration.
      </div>
    )
  }
  const done = completedTaskIds([...events])
  const groups = groupByRole(plan.tasks)
  const total = plan.tasks.length
  const completed = plan.tasks.filter((task) => done.has(task.id)).length

  return (
    <div className="stack-sm">
      <p className="subtitle">
        <strong>{plan.goal}</strong>
        <span className="dim">
          {' '}
          · plan v{plan.version} · {completed}/{total} tasks complete
          {iter !== undefined ? ` · iteration ${iter}` : ''}
        </span>
      </p>

      <div className="plan-progress" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={completed} aria-label="Plan progress">
        <span style={{ width: `${total === 0 ? 0 : Math.round((completed / total) * 100)}%` }} />
      </div>

      <div className="role-board">
        {groups.map((group) => (
          <section key={group.role} className="role-column" aria-label={`${group.role} tasks`}>
            <header className="role-column-head">
              <span className="badge mono">{group.role}</span>
              <span className="dim small">
                {group.tasks.filter((task) => done.has(task.id)).length}/{group.tasks.length}
              </span>
            </header>
            <ul className="task-list">
              {group.tasks.map((task) => {
                const state = taskState(task, done)
                return (
                  <li key={task.id} className={`task-card task-${state}`}>
                    <div className="task-title">
                      <span className={`task-dot task-dot-${state}`} aria-hidden="true" />
                      {task.title}
                    </div>
                    <div className="task-meta mono small dim">
                      <span>{state}</span>
                      <span>· {formatInt(task.token_budget)} tok budget</span>
                      {(task.section_ids ?? []).length > 0 && (
                        <span>· {(task.section_ids ?? []).join(', ')}</span>
                      )}
                    </div>
                    <ul className="task-acceptance">
                      {(task.acceptance ?? []).map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </li>
                )
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
