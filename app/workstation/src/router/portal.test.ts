import { describe, expect, it } from 'vitest'
import { canEnterAdminPortal, isAdminPath, withPortal } from './portal'

describe('portal path helpers', () => {
  it('recognises admin paths', () => {
    expect(isAdminPath('/admin')).toBe(true)
    expect(isAdminPath('/admin/tasks/1')).toBe(true)
    expect(isAdminPath('/administrator')).toBe(false)
    expect(isAdminPath('/tasks')).toBe(false)
  })

  it('keeps department portal paths untouched outside /admin', () => {
    expect(withPortal('/tasks', '/reports/1')).toBe('/reports/1')
    expect(withPortal('/', '/quick')).toBe('/quick')
  })

  it('prefixes /admin when navigating inside the admin portal', () => {
    expect(withPortal('/admin/tasks', '/reports/1')).toBe('/admin/reports/1')
    expect(withPortal('/admin', '/')).toBe('/admin')
    expect(withPortal('/admin/reports/1', '/admin/tenants')).toBe('/admin/tenants')
    expect(withPortal('/admin/reports/1', 'tasks/new')).toBe('/admin/tasks/new')
  })

  it('only lets management roles into the admin portal', () => {
    expect(canEnterAdminPortal(['dashboard', 'qcRules', 'taskObjects:read:owner'])).toBe(false)
    expect(canEnterAdminPortal(['dashboard', 'users', 'departments', 'taskObjects:read:tenant'])).toBe(true)
    expect(canEnterAdminPortal(['dashboard', 'auditLogs', 'taskObjects:read:tenant'])).toBe(true)
    expect(canEnterAdminPortal(undefined)).toBe(false)
  })
})
