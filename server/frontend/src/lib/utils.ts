import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatRelativeDate(date: string) {
  const d = new Date(date)
  const now = new Date()
  const diff = now.getTime() - d.getTime()
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))
  if (days === 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return `${days}d ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function toNum(v: string | null | undefined): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = parseFloat(v)
  return isNaN(n) ? null : n
}

export function toInt(v: string | null | undefined): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = parseInt(v, 10)
  return isNaN(n) ? null : n
}

/** Convert NaN numeric values and empty strings to null for database storage */
export function cleanNaN<T extends Record<string, unknown>>(obj: T): T {
  return Object.fromEntries(
    Object.entries(obj).map(([k, v]) => [
      k,
      typeof v === 'number' && isNaN(v) ? null : v === '' ? null : v,
    ])
  ) as T
}
