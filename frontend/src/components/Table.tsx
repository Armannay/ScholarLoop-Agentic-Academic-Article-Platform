import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: string
  cell?: (item: T) => ReactNode
  className?: string
}

export interface TableProps<T> {
  columns: Column<T>[]
  data: T[]
  empty?: ReactNode
  rowClassName?: (item: T) => string
}

export function Table<T>({
  columns,
  data,
  empty,
  rowClassName,
}: TableProps<T>): React.JSX.Element {
  if (data.length === 0) {
    return (
      <section className="panel">
        <p className="dim empty-state">{empty ?? 'No items.'}</p>
      </section>
    )
  }

  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} scope="col" className={column.className}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((item, index) => (
            <tr key={String(index)} className={rowClassName?.(item)}>
              {columns.map((column) => (
                <td key={column.key} className={column.className}>
                  {column.cell ? column.cell(item) : String(item[column.key as keyof T])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
