/**
 * useTypeahead.js
 * ---------------
 * WHAT: A custom hook that owns all the async state for the search box:
 *       - debounced suggestion fetching with stale-request cancellation
 *       - search submission and "saved" feedback
 *       - loading / error / empty states
 *
 * WHY A CUSTOM HOOK INSTEAD OF INLINE LOGIC IN App.jsx:
 *   The component should describe WHAT the UI looks like; this hook describes
 *   HOW the data flows. Separating them makes both easier to read, and makes
 *   the data logic testable without rendering any JSX.
 *
 * DEBOUNCE STRATEGY:
 *   We use setTimeout (250ms) rather than a library like lodash.debounce.
 *   WHY: The timeout is created in an effect and cleaned up in the same
 *   effect's cleanup function — that's the standard React idiom. Lodash
 *   debounce is fine too, but importing a library to wrap two lines is
 *   unnecessary here.
 *   WHAT "DEBOUNCE" BUYS US: Without it, every character fires a network
 *   request. At 150,000 rows and sub-ms Trie lookup, the backend can handle
 *   it, but it's bad practice — we'd be burning bandwidth and re-rendering on
 *   every keypress for zero user-visible benefit. 250ms is short enough that
 *   users don't notice the delay.
 *
 * STALE REQUEST CANCELLATION (AbortController):
 *   Without cancellation, a slow network can cause responses to arrive
 *   out of order: the user types "ip" (request A), then "iph" (request B).
 *   If B's response arrives before A's, A's response OVERWRITES B's results —
 *   the dropdown shows suggestions for "ip" while the input shows "iph".
 *   Fix: each new fetch aborts the previous one. The fetch() call throws an
 *   AbortError when cancelled; we catch that specific error and ignore it
 *   (it's not a real error, just a cancellation signal).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSuggestions, submitSearch } from "./api";

const DEBOUNCE_MS = 250;

export function useTypeahead() {
  const [inputValue, setInputValue]       = useState("");
  const [suggestions, setSuggestions]     = useState([]);
  const [activeIndex, setActiveIndex]     = useState(-1);   // keyboard highlight
  const [isOpen, setIsOpen]               = useState(false);
  const [loadingSuggest, setLoadingSuggest] = useState(false);
  const [suggestError, setSuggestError]   = useState(null);
  const [latencyMs, setLatencyMs]         = useState(null);
  // Phase 3: surfaces the cache-aside outcome of the LAST /suggest call so
  // the UI can show "served from node-1 (cache hit)" live, the same way
  // latencyMs already surfaces measured timing. Mirrors latencyMs's
  // pattern exactly (set alongside it, reset alongside it) rather than
  // introducing a separate state-management approach for what is really
  // the same kind of "debug info about the last fetch" data.
  const [cacheHit, setCacheHit]           = useState(null);
  const [cacheNode, setCacheNode]         = useState(null);

  const [lastSearch, setLastSearch]       = useState(null);  // {query, count}
  const [submitting, setSubmitting]       = useState(false);
  const [submitError, setSubmitError]     = useState(null);

  // Ref to the current AbortController so we can cancel in-flight requests.
  const abortRef = useRef(null);

  // ── Suggestion fetching (debounced + cancellable) ──────────────────────────
  useEffect(() => {
    const trimmed = inputValue.trim();

    if (!trimmed) {
      setSuggestions([]);
      setIsOpen(false);
      setLatencyMs(null);
      setCacheHit(null);
      setCacheNode(null);
      setSuggestError(null);
      return;
    }

    const timer = setTimeout(async () => {
      // Cancel any in-flight request for a previous prefix.
      if (abortRef.current) abortRef.current.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      setLoadingSuggest(true);
      setSuggestError(null);

      try {
        const data = await fetchSuggestions(trimmed, controller.signal);
        setSuggestions(data.suggestions ?? []);
        setLatencyMs(data.latency_ms ?? null);
        setCacheHit(data.cache_hit ?? null);
        setCacheNode(data.node ?? null);
        setActiveIndex(-1);
        setIsOpen(true);
      } catch (err) {
        if (err.name === "AbortError") return; // stale request — ignore cleanly
        setSuggestError("Could not reach the backend. Is it running?");
        setSuggestions([]);
        setCacheHit(null);
        setCacheNode(null);
        setIsOpen(false);
      } finally {
        setLoadingSuggest(false);
      }
    }, DEBOUNCE_MS);

    // Cleanup: if the user types another character before DEBOUNCE_MS elapses,
    // clear the pending timer (so we don't fire a request for the old prefix).
    return () => clearTimeout(timer);
  }, [inputValue]);

  // ── Search submission ───────────────────────────────────────────────────────
  const doSearch = useCallback(async (query) => {
    const q = query.trim();
    if (!q) return;

    // Abort any in-flight suggestion request — we're committing to a search.
    if (abortRef.current) abortRef.current.abort();

    setIsOpen(false);
    setSubmitting(true);
    setSubmitError(null);
    setLastSearch(null);

    try {
      const data = await submitSearch(q);
      setLastSearch({ query: data.query, count: data.count });
      // Re-fetch suggestions for the current prefix so the incremented count
      // is visible immediately in the dropdown.
      setInputValue(q);          // triggers the suggestion useEffect
    } catch (err) {
      setSubmitError(err.message ?? "Search submission failed.");
    } finally {
      setSubmitting(false);
    }
  }, []);

  // ── Keyboard handler (arrow up/down, Enter, Escape) ────────────────────────
  const handleKeyDown = useCallback(
    (e) => {
      if (!isOpen && e.key !== "Enter") return;

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setActiveIndex((i) => Math.min(i + 1, suggestions.length - 1));
          break;

        case "ArrowUp":
          e.preventDefault();
          setActiveIndex((i) => Math.max(i - 1, -1));
          break;

        case "Enter":
          e.preventDefault();
          if (isOpen && activeIndex >= 0 && suggestions[activeIndex]) {
            // User highlighted a suggestion — submit that one.
            doSearch(suggestions[activeIndex].query);
          } else {
            // No highlight — submit whatever's in the input.
            doSearch(inputValue);
          }
          break;

        case "Escape":
          setIsOpen(false);
          setActiveIndex(-1);
          break;

        default:
          break;
      }
    },
    [isOpen, activeIndex, suggestions, inputValue, doSearch]
  );

  const handleInputChange = useCallback((e) => {
    setInputValue(e.target.value);
    setLastSearch(null);
    setSubmitError(null);
  }, []);

  const handleSuggestionClick = useCallback(
    (suggestion) => {
      setInputValue(suggestion.query);
      doSearch(suggestion.query);
    },
    [doSearch]
  );

  const handleSearchButtonClick = useCallback(() => {
    doSearch(inputValue);
  }, [doSearch, inputValue]);

  return {
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
    closeDropdown: () => setIsOpen(false),
    setInputValue,  // Phase 5: exposed so TrendingPanel clicks can populate the search box
  };
}
