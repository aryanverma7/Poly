import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Line } from 'react-chartjs-2'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend)

const api = (path) => path

const IST_TIME_FMT = new Intl.DateTimeFormat('en-IN', {
  timeZone: 'Asia/Kolkata',
  hour: 'numeric',
  minute: '2-digit',
  second: '2-digit',
  hour12: true,
})
const ET_WINDOW_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
})

function fmtPrice(v) {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return `${(Number(v) * 100).toFixed(1)}¢`
}

function toNum(v, fallback = 0) {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

function formatUsd(v, signed = false) {
  const n = toNum(v, 0)
  if (!signed) return `$${n.toFixed(2)}`
  const abs = Math.abs(n).toFixed(2)
  return `${n >= 0 ? '+' : '-'}$${abs}`
}

function toDate(iso) {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

function formatIstTime(iso) {
  const d = toDate(iso)
  if (!d) return '—'
  return IST_TIME_FMT.format(d)
}

function formatEtWindow(iso) {
  const d = toDate(iso)
  if (!d) return '—'
  const start = new Date(Math.floor(d.getTime() / 300000) * 300000)
  const end = new Date(start.getTime() + 300000)
  return `${ET_WINDOW_FMT.format(start)} - ${ET_WINDOW_FMT.format(end)} ET`
}

function formatLastPoll(iso) {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const sec = Math.round((Date.now() - t) / 1000)
  if (sec < 0) return 'just now'
  if (sec < 90) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  return new Date(iso).toLocaleTimeString()
}

function readPageFromHash() {
  const raw = window.location.hash || '#/home'
  if (raw.startsWith('#/strategy/')) {
    const strategyId = decodeURIComponent(raw.replace('#/strategy/', ''))
    return { type: 'strategy', strategyId }
  }
  return { type: 'home' }
}

function buildHash(page) {
  if (page.type === 'strategy' && page.strategyId) return `#/strategy/${encodeURIComponent(page.strategyId)}`
  return '#/home'
}

export default function App() {
  const [init, setInit] = useState(null)
  const [state, setState] = useState(null)
  const [useLive, setUseLive] = useState(false)
  const [err, setErr] = useState('')
  const [confirmLive, setConfirmLive] = useState(false)
  const [bookPrices, setBookPrices] = useState({ outcomes: {}, slug: null })
  const [page, setPage] = useState(() => readPageFromHash())
  const [strategyData, setStrategyData] = useState({})
  const tickRef = useRef(null)
  const [, setTick] = useState(0)

  const loadInit = useCallback(async () => {
    try {
      const r = await fetch(api('/api/init-check'))
      const d = await r.json()
      setInit(d)
      console.log('Init check:', d)
    } catch (e) {
      console.warn('Init check failed', e)
      setInit({ ok: false, message: String(e.message) })
    }
  }, [])

  const loadState = useCallback(async () => {
    try {
      const r = await fetch(api('/api/state'))
      const s = await r.json()
      setState(s)
    } catch (e) {
      console.error('state', e)
    }
  }, [])

  const loadStrategyChunk = useCallback(async (strategyId, key, offset, limit = 100) => {
    const endpoint = key === 'trades' ? 'trades' : 'roundtrips'
    const r = await fetch(api(`/api/strategy/${strategyId}/${endpoint}?offset=${offset}&limit=${limit}`))
    if (!r.ok) throw new Error(`Failed loading ${endpoint}`)
    return r.json()
  }, [])

  const loadMoreStrategy = useCallback(
    async (strategyId, key, reset = false) => {
      const existing = strategyData[strategyId] || {}
      const branch = existing[key] || {}
      const offset = reset ? 0 : toNum(branch.items?.length, 0)
      if (branch.loading) return
      setStrategyData((prev) => ({
        ...prev,
        [strategyId]: {
          ...(prev[strategyId] || {}),
          [key]: { ...(prev[strategyId]?.[key] || {}), loading: true },
        },
      }))
      try {
        const data = await loadStrategyChunk(strategyId, key, offset)
        setStrategyData((prev) => {
          const oldItems = reset ? [] : prev[strategyId]?.[key]?.items || []
          return {
            ...prev,
            [strategyId]: {
              ...(prev[strategyId] || {}),
              [key]: {
                loading: false,
                total: toNum(data.total, 0),
                items: [...oldItems, ...(data.items || [])],
              },
            },
          }
        })
      } catch (e) {
        setStrategyData((prev) => ({
          ...prev,
          [strategyId]: {
            ...(prev[strategyId] || {}),
            [key]: { ...(prev[strategyId]?.[key] || {}), loading: false, error: String(e.message || e) },
          },
        }))
      }
    },
    [loadStrategyChunk, strategyData]
  )

  const loadOutcomePrices = useCallback(async () => {
    try {
      const r = await fetch(api('/api/outcome-prices'))
      const d = await r.json()
      if (d.ok && d.outcomes) {
        setBookPrices({ outcomes: d.outcomes, slug: d.slug })
      }
    } catch (e) {
      console.warn('outcome-prices', e)
    }
  }, [])

  useEffect(() => {
    loadInit()
    loadState()
  }, [loadInit, loadState])

  useEffect(() => {
    const onHash = () => setPage(readPageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const want = buildHash(page)
    if ((window.location.hash || '#/home') !== want) window.location.hash = want
  }, [page])

  useEffect(() => {
    loadOutcomePrices()
    const id = setInterval(loadOutcomePrices, 700)
    return () => clearInterval(id)
  }, [loadOutcomePrices])

  useEffect(() => {
    if (state?.running) {
      tickRef.current = setInterval(() => {
        loadState()
      }, 1000)
    } else {
      if (tickRef.current) clearInterval(tickRef.current)
      tickRef.current = null
    }
    return () => {
      if (tickRef.current) clearInterval(tickRef.current)
    }
  }, [state?.running, loadState])

  useEffect(() => {
    const id = setInterval(loadState, 5000)
    return () => clearInterval(id)
  }, [loadState])

  useEffect(() => {
    if (page.type !== 'strategy' || !page.strategyId) return
    const d = strategyData[page.strategyId]
    const hasTrades = toNum(d?.trades?.items?.length, 0) > 0
    const hasRoundtrips = toNum(d?.roundtrips?.items?.length, 0) > 0
    if (!hasTrades) loadMoreStrategy(page.strategyId, 'trades', true)
    if (!hasRoundtrips) loadMoreStrategy(page.strategyId, 'roundtrips', true)
  }, [page, strategyData, loadMoreStrategy])

  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [])

  async function onStart() {
    setErr('')
    try {
      const resp = await fetch(api('/api/strategy/start'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: useLive ? 'live' : 'paper' }),
      })
      const data = await resp.json().catch(() => ({}))
      console.log('Start:', resp.status, data)
      if (!resp.ok) {
        setErr(data.detail || `Start failed (${resp.status})`)
        return
      }
      await loadState()
    } catch (e) {
      setErr(String(e.message))
    }
  }

  async function onStop() {
    setErr('')
    try {
      const resp = await fetch(api('/api/strategy/stop'), { method: 'POST' })
      const data = await resp.json().catch(() => ({}))
      console.log('End Strategy:', resp.status, data)
      await loadState()
      await loadInit()
    } catch (e) {
      setErr(String(e.message))
    }
  }

  const s = state || {}
  const strategies = s.strategies || []
  const selectedStrategy = useMemo(
    () => strategies.find((st) => st.id === page.strategyId) || null,
    [strategies, page.strategyId]
  )

  const leaderboard = useMemo(
    () =>
      [...strategies]
        .map((st) => ({
          id: st.id || '',
          label: st.label || st.strategy_name || '—',
          pnl: toNum(st.session_profit, 0),
          roiPct: toNum(st.roi_pct, 0),
          trades: toNum(st.session_trade_count, 0),
          invested: toNum(st.invested_amount, 0),
          balance: toNum(st.balance, 0),
          maxTradesPerWindow: toNum(st.max_trades_per_window, 1),
          consecutiveLosses: toNum(st.consecutive_losses, 0),
          cooldownWindows: toNum(st.cooldown_windows_remaining, 0),
          lastReject: String(st.last_rejection_reason || ''),
          stakeUsd: toNum(st.stake_usd, 0),
        }))
        .sort((a, b) => b.pnl - a.pnl),
    [strategies]
  )

  const comparisonChartData = useMemo(() => {
    if (!strategies.some((st) => (st.equity_curve || []).length > 0)) return null
    const allTimes = new Set()
    strategies.forEach((st) => {
      ;(st.equity_curve || []).forEach(([t]) => allTimes.add(t))
    })
    const labels = Array.from(allTimes).sort()
    const colors = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#14b8a6', '#e11d48']
    return {
      labels,
      datasets: strategies.map((st, i) => {
        const curve = st.equity_curve || []
        return {
          label: st.label || `Strategy ${i + 1}`,
          data: labels.map((lb) => {
            const idx = curve.findIndex(([t]) => t === lb)
            if (idx >= 0) return curve[idx][1]
            const before = curve.filter(([t]) => t <= lb)
            return before.length ? before[before.length - 1][1] : null
          }),
          borderColor: colors[i % colors.length],
          backgroundColor: `${colors[i % colors.length]}22`,
          tension: 0.25,
          borderWidth: 2,
          pointRadius: 0,
        }
      }),
    }
  }, [strategies])

  const strategyChartData = useMemo(() => {
    if (!selectedStrategy || !(selectedStrategy.equity_curve || []).length) return null
    return {
      labels: selectedStrategy.equity_curve.map(([t]) => t),
      datasets: [
        {
          label: 'Balance',
          data: selectedStrategy.equity_curve.map(([, b]) => b),
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59, 130, 246, 0.16)',
          fill: true,
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    }
  }, [selectedStrategy])

  const detail = selectedStrategy ? strategyData[selectedStrategy.id] || {} : {}
  const detailTrades = detail.trades?.items || []
  const detailRoundtrips = detail.roundtrips?.items || []
  const detailTradesTotal = toNum(detail.trades?.total, 0)
  const detailRoundtripsTotal = toNum(detail.roundtrips?.total, 0)
  const canLoadMoreTrades = detailTrades.length < detailTradesTotal
  const canLoadMoreRoundtrips = detailRoundtrips.length < detailRoundtripsTotal

  return (
    <div className="app">
      <h1>Polymarket Bitcoin 5m Strategy</h1>
      <p className="muted">Home has overview + leaderboard. Open any strategy for full details and history.</p>

      <div className="controls" style={{ marginTop: 12 }}>
        <button
          type="button"
          className={page.type === 'home' ? 'primary' : ''}
          onClick={() => setPage({ type: 'home' })}
        >
          Home
        </button>
        {page.type === 'strategy' && selectedStrategy && (
          <span className="muted">Viewing: {selectedStrategy.label || selectedStrategy.id}</span>
        )}
      </div>

      <div className="controls">
        <span className={`badge ${useLive ? 'live' : 'paper'}`}>
          {useLive ? 'Live (real money)' : 'Testing (paper)'}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="muted">Use real money</span>
          <div
            className={`switch ${useLive ? 'on' : ''}`}
            onClick={() => (useLive ? setUseLive(false) : setConfirmLive(true))}
            role="button"
            tabIndex={0}
          />
        </div>
        <button type="button" className="primary" disabled={s.running || !init?.ok} onClick={onStart}>
          Start Strategy
        </button>
        <button type="button" className="danger" disabled={!s.running} onClick={onStop}>
          End Strategy
        </button>
      </div>

      {confirmLive && (
        <div className="card" style={{ marginBottom: 16 }}>
          <strong>Confirm live trading?</strong>
          <p className="muted">Real funds will be used on Polymarket.</p>
          <button
            type="button"
            className="primary"
            onClick={() => {
              setUseLive(true)
              setConfirmLive(false)
            }}
          >
            Confirm
          </button>{' '}
          <button type="button" onClick={() => setConfirmLive(false)}>
            Cancel
          </button>
        </div>
      )}

      {page.type === 'home' ? (
        <>
          <div className="card status-bar">
            <label>{s.running ? 'Current market' : 'Market (before start)'}</label>
            <div className="value" style={{ fontSize: '0.95rem' }}>
              {s.running ? (
                s.event_slug ? (
                  <>
                    <span className="positive">Live - {s.event_slug}</span>
                    {s.event_title && (
                      <div className="muted" style={{ marginTop: 4 }}>{s.event_title}</div>
                    )}
                  </>
                ) : (
                  <span className="muted">Resolving...</span>
                )
              ) : init?.ok ? (
                <>
                  <span className="positive">Ready - {init.slug}</span>
                  <div className="muted" style={{ marginTop: 4 }}>{init.title}</div>
                </>
              ) : (
                <span className="negative">{init?.message || 'Checking...'}</span>
              )}
            </div>
            {!s.running && (
              <button type="button" style={{ marginTop: 8 }} onClick={loadInit}>
                Refresh market
              </button>
            )}
          </div>

          <div className="card status-bar">
            <label>Status</label>
            <div className="value">{s.status_message || 'Stopped'}</div>
            <div className="muted" style={{ marginTop: 4 }}>
              Last poll: {formatLastPoll(s.last_poll_at)}
              {s.event_slug ? ` · Event: ${s.event_slug}` : ''}
            </div>
          </div>

          <div className="card status-bar">
            <label>External data (Binance)</label>
            <div className="muted" style={{ marginTop: 4 }}>
              <strong>Mode:</strong> {s.external_data_enabled ? 'enabled' : 'disabled'}{' '}
              {!s.external_data_enabled ? (
                <span className="muted">(set <code>ENABLE_EXTERNAL_DATA=true</code> in <code>.env</code>, not <code>.env.example</code>)</span>
              ) : null}
            </div>
            <div className="muted" style={{ marginTop: 4 }}>
              <strong>WS freshness:</strong> {s.external_data_last_ws_at ? formatLastPoll(s.external_data_last_ws_at) : '—'}
            </div>
            <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
              <strong>Binance price:</strong> {s.external_snapshot?.binance_price != null ? Number(s.external_snapshot.binance_price).toFixed(2) : '—'}
              {' · '}
              <strong>Move 30s:</strong> {s.external_snapshot?.binance_move_30s != null ? Number(s.external_snapshot.binance_move_30s).toFixed(2) : 'warming up (30s)'}
              {' · '}
              <strong>Funding:</strong> {s.external_snapshot?.funding_rate != null ? String(s.external_snapshot.funding_rate) : '—'}
              {' · '}
              <strong>OI Δ5m:</strong> {s.external_snapshot?.open_interest_change_5m != null ? `${(Number(s.external_snapshot.open_interest_change_5m) * 100).toFixed(3)}%` : 'warming up (5m)'}
              {' · '}
              <strong>Depth imb:</strong> {s.external_snapshot?.binance_depth_imbalance != null ? Number(s.external_snapshot.binance_depth_imbalance).toFixed(2) : '—'}
              {' · '}
              <strong>Oracle gap:</strong> {s.external_snapshot?.oracle_gap_usd != null ? Number(s.external_snapshot.oracle_gap_usd).toFixed(2) : 'waiting for local BTC price'}
            </div>
          </div>

          <div className="card book-prices">
            <label>Up / Down - CLOB (buy = best ask, sell = best bid)</label>
            <div className="book-grid">
              {(Object.keys(bookPrices.outcomes || {}).length
                ? Object.keys(bookPrices.outcomes)
                : ['Up', 'Down']
              ).map((side) => {
                const o = bookPrices.outcomes?.[side] || {}
                return (
                  <div key={side} className="book-col">
                    <strong>{side}</strong>
                    <div>Ask (buy) <span className="mono">{fmtPrice(o.best_ask)}</span></div>
                    <div>Bid (sell) <span className="mono">{fmtPrice(o.best_bid)}</span></div>
                    <div>Last trade <span className="mono">{fmtPrice(o.last_trade)}</span></div>
                  </div>
                )
              })}
            </div>
          </div>

          <h2 className="section-title">Leaderboard</h2>

          <div className="card" style={{ marginBottom: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Strategy</th>
                  <th>P&amp;L</th>
                  <th>ROI</th>
                  <th>Trades</th>
                  <th>Cap/window</th>
                  <th>Loss streak</th>
                  <th>Cooldown</th>
                  <th>Last reject reason</th>
                  <th>Stake</th>
                  <th>Invested</th>
                  <th>Balance</th>
                  <th>Page</th>
                </tr>
              </thead>
              <tbody>
                {leaderboard.map((row, i) => (
                  <tr key={row.id || row.label}>
                    <td>{i + 1}</td>
                    <td>{row.label}</td>
                    <td className={row.pnl >= 0 ? 'positive' : 'negative'}>
                      {formatUsd(row.pnl, true)}
                    </td>
                    <td className={row.roiPct >= 0 ? 'positive' : 'negative'}>{row.roiPct.toFixed(2)}%</td>
                    <td>{row.trades}</td>
                    <td>{row.maxTradesPerWindow}</td>
                    <td>{row.consecutiveLosses}</td>
                    <td>{row.cooldownWindows}</td>
                    <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.lastReject}>{row.lastReject || '—'}</td>
                    <td>{formatUsd(row.stakeUsd)}</td>
                    <td>{formatUsd(row.invested)}</td>
                    <td>{formatUsd(row.balance)}</td>
                    <td>
                      <button type="button" onClick={() => setPage({ type: 'strategy', strategyId: row.id })}>
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {comparisonChartData && (
            <div className="card chart-wrap clean-chart" style={{ marginTop: 16 }}>
              <label>Strategy equity comparison</label>
              <div style={{ height: 280 }}>
                <Line
                  data={comparisonChartData}
                  options={{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: { legend: { display: true, labels: { boxWidth: 10 } } },
                    scales: {
                      x: {
                        ticks: {
                          maxTicksLimit: 10,
                          callback: (_, idx) => formatIstTime(comparisonChartData.labels[idx]),
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' },
                      },
                      y: {
                        grid: { color: 'rgba(255,255,255,0.08)' },
                        ticks: { callback: (v) => `$${Number(v).toFixed(0)}` },
                      },
                    },
                  }}
                />
              </div>
            </div>
          )}
        </>
      ) : selectedStrategy ? (
        <>
          <div className="controls">
            <button type="button" onClick={() => setPage({ type: 'home' })}>Back to home</button>
            <button
              type="button"
              onClick={() => {
                loadMoreStrategy(selectedStrategy.id, 'trades', true)
                loadMoreStrategy(selectedStrategy.id, 'roundtrips', true)
              }}
            >
              Refresh this page
            </button>
          </div>

          <div className="card">
            <label>Strategy</label>
            <div className="value">{selectedStrategy.label || selectedStrategy.id}</div>
            <div className="muted" style={{ marginTop: 4 }}>
              Last reject reason: {selectedStrategy.last_rejection_reason || '—'}
            </div>
          </div>

          <div className="grid">
            <div className="card"><label>Balance</label><div className="value">{formatUsd(selectedStrategy.balance)}</div></div>
            <div className="card"><label>P&amp;L session</label><div className={`value ${toNum(selectedStrategy.session_profit) >= 0 ? 'positive' : 'negative'}`}>{formatUsd(selectedStrategy.session_profit, true)}</div></div>
            <div className="card"><label>ROI</label><div className={`value ${toNum(selectedStrategy.roi_pct) >= 0 ? 'positive' : 'negative'}`}>{toNum(selectedStrategy.roi_pct).toFixed(2)}%</div></div>
            <div className="card"><label>Trades</label><div className="value">{toNum(selectedStrategy.session_trade_count)}</div></div>
            <div className="card"><label>Cap/window</label><div className="value">{toNum(selectedStrategy.max_trades_per_window)}</div></div>
            <div className="card"><label>Loss streak</label><div className="value">{toNum(selectedStrategy.consecutive_losses)}</div></div>
            <div className="card"><label>Cooldown</label><div className="value">{toNum(selectedStrategy.cooldown_windows_remaining)}</div></div>
            <div className="card"><label>Stake</label><div className="value">{formatUsd(selectedStrategy.stake_usd)}</div></div>
          </div>

          {strategyChartData && (
            <div className="card chart-wrap clean-chart">
              <label>Equity curve</label>
              <div style={{ height: 280 }}>
                <Line
                  data={strategyChartData}
                  options={{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: { legend: { display: false } },
                    scales: {
                      x: {
                        ticks: {
                          maxTicksLimit: 12,
                          callback: (_, idx) => formatIstTime(strategyChartData.labels[idx]),
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' },
                      },
                      y: {
                        ticks: { callback: (v) => `$${Number(v).toFixed(0)}` },
                        grid: { color: 'rgba(255,255,255,0.08)' },
                      },
                    },
                  }}
                />
              </div>
            </div>
          )}

          <div className="card">
            <label>Recent trades (infinite scroll)</label>
            <div
              className="scroll-table"
              onScroll={(e) => {
                const el = e.currentTarget
                if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40 && canLoadMoreTrades && !detail.trades?.loading) {
                  loadMoreStrategy(selectedStrategy.id, 'trades', false)
                }
              }}
            >
              <table>
                <thead>
                  <tr>
                    <th>Time (IST)</th>
                    <th>BTC window (ET)</th>
                    <th>Side</th>
                    <th>Outcome</th>
                    <th>Price</th>
                    <th>USD</th>
                  </tr>
                </thead>
                <tbody>
                  {detailTrades.map((t, i) => (
                    <tr key={`${t.ts || 't'}-${i}`}>
                      <td>{formatIstTime(t.ts)}</td>
                      <td>{formatEtWindow(t.ts)}</td>
                      <td>{t.side || '—'}</td>
                      <td>{t.outcome || '—'}</td>
                      <td>{toNum(t.price).toFixed(2)}</td>
                      <td>{formatUsd(t.amount_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="muted" style={{ marginTop: 8 }}>
              Showing {detailTrades.length} / {detailTradesTotal}
              {detail.trades?.loading ? ' · loading...' : ''}
            </div>
          </div>

          <div className="card">
            <label>Combined trade lifecycle (buy + sell)</label>
            <div
              className="scroll-table"
              onScroll={(e) => {
                const el = e.currentTarget
                if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40 && canLoadMoreRoundtrips && !detail.roundtrips?.loading) {
                  loadMoreStrategy(selectedStrategy.id, 'roundtrips', false)
                }
              }}
            >
              <table>
                <thead>
                  <tr>
                    <th>Start (IST)</th>
                    <th>BTC window (ET)</th>
                    <th>Outcome</th>
                    <th>Buy</th>
                    <th>Sell</th>
                    <th>Size</th>
                    <th>P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {detailRoundtrips.map((r) => (
                    <tr key={r.id}>
                      <td>{formatIstTime(r.buy_ts)}</td>
                      <td>{formatEtWindow(r.buy_ts)}</td>
                      <td>{r.outcome || '—'}</td>
                      <td>{toNum(r.buy_price).toFixed(2)}</td>
                      <td>{toNum(r.sell_price).toFixed(2)}</td>
                      <td>{toNum(r.size).toFixed(2)}</td>
                      <td className={toNum(r.pnl_usd) >= 0 ? 'positive' : 'negative'}>{formatUsd(r.pnl_usd, true)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="muted" style={{ marginTop: 8 }}>
              Showing {detailRoundtrips.length} / {detailRoundtripsTotal}
              {detail.roundtrips?.loading ? ' · loading...' : ''}
            </div>
          </div>
        </>
      ) : (
        <div className="card"><div className="muted">Strategy not found. Go back to home.</div></div>
      )}

      {err && <div className="error">{err}</div>}
      {s.last_error && <div className="error">Backend: {s.last_error}</div>}

      <p className="muted" style={{ marginTop: 24, fontSize: 12 }}>
        React UI (port 5173) — run backend on 8000: <code>python main.py</code>
      </p>
    </div>
  )
}
