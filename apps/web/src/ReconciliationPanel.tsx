import { useEffect, useState } from 'react'
import { isReconciliationReport, type ReconciliationReport } from './reconciliation-contract'

export default function ReconciliationPanel({ refreshToken }: { refreshToken: unknown }) {
  const [report, setReport] = useState<ReconciliationReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    async function load() {
      try {
        const response = await fetch('/api/v1/system/reconciliation', { signal: controller.signal })
        if (!response.ok) throw new Error('启动对账状态暂不可用，请检查本机服务。')
        const value: unknown = await response.json()
        if (!isReconciliationReport(value)) throw new Error('启动对账状态格式无效，请重连服务后重试。')
        if (disposed) return
        setReport(value)
        setError(null)
      } catch (failure) {
        if (disposed || controller.signal.aborted) return
        setReport(null)
        setError(failure instanceof Error && failure.message.startsWith('启动对账')
          ? failure.message : '启动对账状态读取失败，请检查本机服务。')
      }
    }
    void load()
    return () => { disposed = true; controller.abort() }
  }, [refreshToken])

  return <div aria-label="启动索引对账">
    <p className="service-hint">{error || (report?.state === 'complete' ? '启动对账完成'
      : report?.state === 'unavailable' ? '启动对账未完成' : '等待启动对账')}</p>
    {report && <>
      <p className="service-hint">检查 {report.checked} · 正常 {report.healthy} · 修复已排队 {report.repair_queued}</p>
      {report.skipped > 0 && <p className="service-hint">状态变化，跳过 {report.skipped} 项</p>}
      {report.detail && <p className="service-detail">{report.detail}，可使用上方“重连服务”重试。</p>}
    </>}
  </div>
}
