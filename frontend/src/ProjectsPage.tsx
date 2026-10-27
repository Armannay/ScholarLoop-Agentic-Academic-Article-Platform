import { useCallback } from 'react'
import { listProjects } from './api'
import { formatInt, formatTimestamp, runStateLabel, runStateTone } from './format'
import { StatusPill } from './StatusPill'
import { useAsync } from './useApi'
import type { ProjectList } from './types'

export interface ProjectsPageProps {
  onOpenProject: (projectId: string) => void
  onNewProject: () => void
}

/** The project list: derived run facts per project, newest activity first. */
export function ProjectsPage({ onOpenProject, onNewProject }: ProjectsPageProps): React.JSX.Element {
  const load = useCallback((signal?: AbortSignal) => listProjects({ signal }), [])
  const projects = useAsync<ProjectList>(load)

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-head-wrap">
          <div>
            <h2>Projects</h2>
            <p className="subtitle">
              A project owns one brief and every run written against it. Run counts and the
              latest state are derived from the runs themselves.
            </p>
          </div>
          <div className="btn-row">
            <button type="button" className="btn" onClick={projects.reload} disabled={projects.loading}>
              {projects.loading ? 'Refreshing…' : 'Refresh'}
            </button>
            <button type="button" className="btn btn-primary" onClick={onNewProject}>
              + New project
            </button>
          </div>
        </div>

        {projects.error && (
          <div className="notice notice-bad" role="alert">
            <span className="mono small">{projects.error}</span>
            <button type="button" className="btn btn-sm" onClick={projects.reload}>
              Retry
            </button>
          </div>
        )}

        {projects.loading && !projects.data ? (
          <div className="empty-state" role="status" aria-busy="true">
            Loading projects…
          </div>
        ) : !projects.data || projects.data.projects.length === 0 ? (
          <div className="empty-state" role="status">
            <p>No projects yet. A project starts with a Brief — topic, audience, language, length.</p>
            <button type="button" className="btn btn-primary" onClick={onNewProject}>
              Create the first project
            </button>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Project</th>
                  <th scope="col">Latest run</th>
                  <th scope="col">Q</th>
                  <th scope="col">Runs</th>
                  <th scope="col">Updated</th>
                  <th scope="col">
                    <span className="dim">Open</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {projects.data.projects.map((project) => (
                  <tr key={project.id}>
                    <td>
                      <button
                        type="button"
                        className="btn-link strong"
                        onClick={() => onOpenProject(project.id)}
                      >
                        {project.name}
                      </button>
                      <div className="small dim">{project.brief?.topic ?? project.description ?? '—'}</div>
                    </td>
                    <td>
                      {project.latest_state ? (
                        <StatusPill
                          tone={runStateTone(project.latest_state)}
                          label={runStateLabel(project.latest_state)}
                        />
                      ) : (
                        <span className="dim small">no runs yet</span>
                      )}
                    </td>
                    <td className="mono">
                      {project.latest_q ?? '—'}
                      {project.best_q !== null && project.best_q !== undefined ? (
                        <span className="dim small"> (best {formatInt(project.best_q)})</span>
                      ) : null}
                    </td>
                    <td className="mono">{project.run_count}</td>
                    <td className="mono small dim">{formatTimestamp(project.updated_at)}</td>
                    <td>
                      <button type="button" className="btn btn-sm" onClick={() => onOpenProject(project.id)}>
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {projects.data && (
          <p className="small dim">
            {projects.data.projects.length} of {projects.data.total} project(s)
            {projects.data.has_more ? ' · more available (cursor pagination)' : ''}
          </p>
        )}
      </section>
    </div>
  )
}
