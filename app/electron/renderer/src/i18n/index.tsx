/**
 * Lightweight renderer-side i18n (English + Korean).
 *
 * `LanguageProvider` holds the active language (persisted to localStorage, with a
 * first-run guess from `navigator.language`) and exposes `t(key, vars?)` plus
 * `lang` / `setLang` via `useI18n()`. Strings live in `./strings`; this module is
 * the only place that decides which dictionary to read and how to interpolate.
 *
 * It is intentionally renderer-only — language is a presentation preference, so
 * it never touches the worker contract or the main-process settings store.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { en, ko, type Lang, type TKey } from './strings';

export type { Lang, TKey } from './strings';
export type TVars = Record<string, string | number>;
export type TFunc = (key: TKey, vars?: TVars) => string;

const STORAGE_KEY = 'uvapp.lang';

function interpolate(template: string, vars?: TVars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, k: string) => (k in vars ? String(vars[k]) : `{${k}}`));
}

/** Resolve a key for an explicit language (falls back to English, then the key). */
export function translate(lang: Lang, key: TKey, vars?: TVars): string {
  const dict = lang === 'ko' ? ko : en;
  const raw = dict[key] ?? en[key] ?? String(key);
  return interpolate(raw, vars);
}

function detectInitialLang(): Lang {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'en' || saved === 'ko') return saved;
  } catch {
    /* localStorage unavailable — fall through to navigator guess */
  }
  const nav = typeof navigator !== 'undefined' ? navigator.language : '';
  return nav && nav.toLowerCase().startsWith('ko') ? 'ko' : 'en';
}

interface I18nValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: TFunc;
}

const I18nContext = createContext<I18nValue | null>(null);

export function LanguageProvider(props: { children: React.ReactNode }): JSX.Element {
  const [lang, setLangState] = useState<Lang>(detectInitialLang);

  // Keep <html lang> in sync for correct font/line-break handling.
  useEffect(() => {
    if (typeof document !== 'undefined') document.documentElement.lang = lang;
  }, [lang]);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* ignore persistence failures (e.g. private mode) */
    }
  }, []);

  const value = useMemo<I18nValue>(
    () => ({ lang, setLang, t: (key, vars) => translate(lang, key, vars) }),
    [lang, setLang],
  );

  return <I18nContext.Provider value={value}>{props.children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error('useI18n must be used within a LanguageProvider');
  return ctx;
}

/** Convenience hook for components that only need the translator. */
export function useT(): TFunc {
  return useI18n().t;
}

// --- shared label helpers --------------------------------------------------

const STATUS_KEYS: Record<string, TKey> = {
  idle: 'status.idle',
  queued: 'status.queued',
  running: 'status.running',
  accepted: 'status.accepted',
  rejected: 'status.rejected',
  failed: 'status.failed',
  cancelled: 'status.cancelled',
  partial: 'status.partial',
  needs_user_review: 'status.needs_user_review',
  needs_input: 'status.needs_input',
  no_uv: 'status.no_uv',
};

/** Localized label for a run-status pill (falls back to a humanized raw value). */
export function statusLabel(t: TFunc, status: string | null | undefined): string {
  const s = status ?? 'idle';
  const key = STATUS_KEYS[s];
  return key ? t(key) : s.replace(/_/g, ' ');
}
