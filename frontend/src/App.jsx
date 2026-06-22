/**
 * App.jsx — QueryFlow Analytics
 * --------------------------------
 * UI redesign: modern SaaS dashboard branding.
 * All business logic, API calls, and data flows unchanged.
 *
 * Phase 5 features (unchanged):
 *   - TrendingPanel: shows top 10 globally trending queries, polls every 10s.
 *   - MetricsBar: live performance numbers (cache hit rate, p95, DB writes saved),
 *     polls every 5s.
 *   - "Buffered" badge on search result: reflects the batch-write change.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { useTypeahead } from "./useTypeahead";
import { fetchTrending, fetchMetrics } from "./api";
import styles from "./App.module.css";
import {
  Search,
  TrendingUp,
  BarChart3,
  Zap,
  Activity,
  CheckCircle2,
  ChevronUp,
  AlertCircle,
  Info,
} from "lucide-react";

export default function App() {
  const {
    inputValue,
    suggestions,
    activeIndex,
    isOpen,
    loadingSuggest,
    suggestError,
    latencyMs,
    cacheHit,
    cacheNode,
    lastSearch,
    submitting,
    submitError,
    handleInputChange,
    handleKeyDown,
    handleSuggestionClick,
    handleSearchButtonClick,
    closeDropdown,
    setInputValue,
  } = useTypeahead();

  const inputRef = useRef(null);

  useEffect(() => {
    const handler = (e) => {
      if (!e.target.closest(`.${styles.combobox}`)) closeDropdown();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [closeDropdown]);

  const activeId = activeIndex >= 0 ? `suggestion-${activeIndex}` : undefined;

  // Populate search box from a trending click
  const handleTrendingClick = useCallback((query) => {
    setInputValue(query);
    inputRef.current?.focus();
  }, [setInputValue]);

  return (
    <div className={styles.page}>
      {/* ── Hero Header ─────────────────────────────────────────── */}
      <header className={styles.header}>
        <div className={styles.heroBadge}>
          <span className={styles.heroBadgeDot} />
          Realtime Search Platform
        </div>
        <h1 className={styles.title}>QueryFlow Analytics</h1>
        <p className={styles.subtitle}>
          Real-time Search Intelligence&nbsp;•&nbsp;Cache Optimization&nbsp;•&nbsp;Live System Insights
        </p>
      </header>

      {/* ── Search box ──────────────────────────────────────────── */}
      <div className={styles.combobox}>
        <div className={styles.inputRow}>
          <div className={styles.inputWrapper}>
            <input
              ref={inputRef}
              className={styles.input}
              type="text"
              value={inputValue}
              placeholder="Start typing a search…"
              autoComplete="off"
              spellCheck={false}
              aria-label="Search"
              aria-autocomplete="list"
              aria-expanded={isOpen}
              aria-activedescendant={activeId}
              onChange={handleInputChange}
              onKeyDown={handleKeyDown}
            />
            <span className={styles.inputIcon}>
              <Search size={16} />
            </span>
          </div>
          <button
            className={styles.searchBtn}
            onClick={handleSearchButtonClick}
            disabled={submitting || !inputValue.trim()}
            aria-label="Submit search"
          >
            {submitting ? "…" : "Search"}
          </button>
        </div>

        {isOpen && (
          <ul className={styles.dropdown} role="listbox" aria-label="Suggestions">
            {loadingSuggest && (
              <li className={styles.statusRow}><span className={styles.muted}>Loading…</span></li>
            )}
            {!loadingSuggest && suggestError && (
              <li className={styles.statusRow}><span className={styles.error}>{suggestError}</span></li>
            )}
            {!loadingSuggest && !suggestError && suggestions.length === 0 && (
              <li className={styles.statusRow}><span className={styles.muted}>No matches</span></li>
            )}
            {!loadingSuggest && suggestions.map((s, i) => (
              <li
                key={s.query}
                id={`suggestion-${i}`}
                role="option"
                aria-selected={i === activeIndex}
                className={[styles.option, i === activeIndex ? styles.optionActive : ""].join(" ")}
                onMouseDown={(e) => { e.preventDefault(); handleSuggestionClick(s); }}
              >
                <span className={styles.optionQuery}>{s.query}</span>
                <span className={styles.optionCount}>{s.count.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ── Meta strip ──────────────────────────────────────────── */}
      <div className={styles.metaStrip}>
        {latencyMs !== null && !isOpen && (
          <MetaPill label="last suggest" value={`${latencyMs} ms`} color="accent" />
        )}
        {latencyMs !== null && isOpen && suggestions.length > 0 && (
          <>
            <MetaPill label="suggest latency" value={`${latencyMs} ms`} color="accent" />
            <MetaPill label="results" value={suggestions.length} color="muted" />
          </>
        )}
        {cacheHit !== null && (
          <MetaPill label="cache" value={cacheHit ? "HIT" : "MISS"} color={cacheHit ? "good" : "muted"} />
        )}
        {cacheNode && <MetaPill label="served by" value={cacheNode} color="muted" />}
      </div>

      {/* ── Buffered success card ────────────────────────────────── */}
      {lastSearch && (
        <div className={styles.resultCard}>
          <div className={styles.resultCardHeader}>
            <span className={styles.resultCardIcon}>
              <CheckCircle2 size={16} />
            </span>
            <span className={styles.resultCardTitle}>
              <Zap size={11} style={{ display: "inline", marginRight: 4, verticalAlign: "middle" }} />
              Buffered
            </span>
          </div>
          <div className={styles.resultQuery}>&ldquo;{lastSearch.query}&rdquo;</div>
          <p className={styles.resultNote}>
            Search event written to in-memory buffer. DB write happens on next batch flush (~5s).
            <br />
            <span className={styles.muted}>
              Active cache invalidation will evict affected prefixes after the flush.
            </span>
          </p>
        </div>
      )}

      {submitError && (
        <div className={styles.errorCard}>
          <span className={styles.error}>
            <AlertCircle size={14} style={{ display: "inline", marginRight: 6, verticalAlign: "middle" }} />
            {submitError}
          </span>
        </div>
      )}

      {/* ── Trending panel + Metrics bar ────────────────────────── */}
      <div className={styles.panelRow}>
        <TrendingPanel onQueryClick={handleTrendingClick} />
        <MetricsBar />
      </div>

      {/* ── Phase notes ─────────────────────────────────────────── */}
      <aside className={styles.phaseNotes}>
        <h2 className={styles.notesTitle}>
          <span className={styles.notesTitleIcon}><Activity size={13} /></span>
          Phase 5 — what changed
        </h2>
        <ul className={styles.notesList}>
          <li><span className={styles.badge}>done</span> Batch writer: POST /search writes to buffer, background thread flushes every 5s</li>
          <li><span className={styles.badge}>done</span> Active cache invalidation: on flush, all affected prefix cache entries are evicted immediately</li>
          <li><span className={styles.badge}>done</span> GET /metrics: live latency percentiles, cache hit rate, DB writes saved</li>
          <li><span className={styles.badge}>done</span> GET /trending: top 10 globally trending queries, cached, polled every 10s</li>
          <li><span className={[styles.badge, styles.badgeKnown].join(" ")}>known</span> Buffer is in-memory: crash before flush loses up to flush_interval × QPS events (acceptable for typeahead)</li>
          <li><span className={[styles.badge, styles.badgeKnown].join(" ")}>known</span> Trie + cache still single-process: multi-worker deployment needs real Redis</li>
        </ul>
      </aside>
    </div>
  );
}

// ── Trending Panel ─────────────────────────────────────────────────────────────

function TrendingPanel({ onQueryClick }) {
  const [trending, setTrending] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const data = await fetchTrending();
      setTrending(data.trending ?? []);
    } catch {
      // silently ignore — trending is non-critical
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  function rankClass(i) {
    if (i === 0) return styles.trendingRankGold;
    if (i === 1) return styles.trendingRankSilver;
    if (i === 2) return styles.trendingRankBronze;
    return styles.trendingRankPlain;
  }

  return (
    <div className={styles.panel}>
      <div className={styles.panelHeader}>
        <div className={styles.panelTitleGroup}>
          <span className={styles.panelIconWrap}>
            <TrendingUp size={14} />
          </span>
          <span className={styles.panelTitle}>Trending</span>
        </div>
        <span className={styles.panelHint}>updates every 10s</span>
      </div>
      {loading ? (
        <span className={styles.muted}>Loading…</span>
      ) : trending.length === 0 ? (
        <span className={styles.muted}>No trending data yet</span>
      ) : (
        <ol className={styles.trendingList}>
          {trending.map((item, i) => (
            <li key={item.query} className={styles.trendingItem}>
              <span className={[styles.trendingRank, rankClass(i)].join(" ")}>
                {i + 1}
              </span>
              <button
                className={styles.trendingQuery}
                onClick={() => onQueryClick(item.query)}
                title="Click to search"
              >
                {item.query}
              </button>
              <span className={styles.trendingTailWrap}>
                <span className={styles.trendingArrow}><ChevronUp size={11} /></span>
                <span className={styles.trendingScore}>{(item.score ?? item.count ?? 0).toLocaleString()}</span>
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ── Metrics Bar ────────────────────────────────────────────────────────────────

function MetricsBar() {
  const [m, setM] = useState(null);

  const load = useCallback(async () => {
    try {
      setM(await fetchMetrics());
    } catch {
      // silently ignore
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className={styles.panel}>
      <div className={styles.panelHeader}>
        <div className={styles.panelTitleGroup}>
          <span className={[styles.panelIconWrap, styles.panelIconWrapAmber].join(" ")}>
            <BarChart3 size={14} />
          </span>
          <span className={styles.panelTitle}>Live Metrics</span>
        </div>
        <span className={styles.panelHint}>updates every 5s</span>
      </div>
      {!m ? (
        <span className={styles.muted}>Loading…</span>
      ) : (
        <div className={styles.metricsGrid}>
          <MetricBlock
            label="Cache Hit Rate"
            value={m.cache_hit_rate_pct != null ? `${m.cache_hit_rate_pct}%` : "—"}
            highlight={m.cache_hit_rate_pct > 60}
            dotClass={m.cache_hit_rate_pct > 60 ? styles.metricDotGreen : styles.metricDot}
          />
          <MetricBlock
            label="Suggest p95"
            value={m.suggest_p95_ms != null ? `${m.suggest_p95_ms} ms` : "—"}
            mono
            dotClass={styles.metricDotBlue}
          />
          <MetricBlock
            label="Suggest p99"
            value={m.suggest_p99_ms != null ? `${m.suggest_p99_ms} ms` : "—"}
            mono
            dotClass={styles.metricDotBlue}
          />
          <MetricBlock
            label="DB Writes Saved"
            value={m.db_writes_saved.toLocaleString()}
            highlight={m.db_writes_saved > 0}
            dotClass={m.db_writes_saved > 0 ? styles.metricDotGreen : styles.metricDot}
          />
          <MetricBlock
            label="Batch Flushes"
            value={m.batch_flush_count}
            dotClass={styles.metricDotAmber}
          />
          <MetricBlock
            label="Events Flushed"
            value={m.batch_events_flushed.toLocaleString()}
            dotClass={styles.metricDotAmber}
          />
          <MetricBlock
            label="Buffer Size"
            value={m.buffer_current_size}
            dotClass={styles.metricDot}
          />
          <MetricBlock
            label="/suggest Calls"
            value={m.suggest_request_count.toLocaleString()}
            dotClass={styles.metricDotBlue}
          />
          <MetricBlock
            label="/search Calls"
            value={m.search_request_count.toLocaleString()}
            dotClass={styles.metricDotBlue}
          />
        </div>
      )}
    </div>
  );
}

function MetricBlock({ label, value, highlight, mono, dotClass }) {
  return (
    <div className={styles.metricBlock}>
      <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <span className={dotClass ?? styles.metricDot} style={{ width: 6, height: 6, borderRadius: "50%", flexShrink: 0 }} />
        <span className={styles.metricBlockLabel}>{label}</span>
      </div>
      <span className={[
        styles.metricBlockValue,
        mono ? styles.monoVal : "",
        highlight ? styles.metricHighlight : "",
      ].join(" ").trim()}>
        {value}
      </span>
    </div>
  );
}

function MetaPill({ label, value, color }) {
  return (
    <span className={`${styles.pill} ${styles[`pill_${color}`]}`}>
      <span className={styles.pillLabel}>{label}</span>
      <span className={styles.pillValue}>{value}</span>
    </span>
  );
}
