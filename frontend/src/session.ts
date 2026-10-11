import { useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
const prefix = 'tenderhack:session:v1:'
export function readSession<T>(key: string, fallback: T): T {
  try { const value = sessionStorage.getItem(prefix + key); return value === null ? fallback : JSON.parse(value) as T } catch { return fallback }
}
export function writeSession(key: string, value: unknown) {
  try { sessionStorage.setItem(prefix + key, JSON.stringify(value)) } catch { /* Storage can be unavailable or full; keep the current form usable. */ }
}
export function useSessionState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => readSession(key, typeof initial === 'function' ? (initial as () => T)() : initial))
  const save: Dispatch<SetStateAction<T>> = next => setValue(current => {
    const updated = typeof next === 'function' ? (next as (previous: T) => T)(current) : next
    writeSession(key, updated)
    return updated
  })
  return [value, save]
}
