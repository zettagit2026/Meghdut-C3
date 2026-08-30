import { createContext, useContext, useEffect, useState, useCallback } from "react";

// Theme system for the MEGHDUT C3 console. Mirrors the AuthContext pattern
// (createContext + provider + a use* hook) so the app has one consistent
// context idiom.
//
// Resolution order on first paint:
//   1. explicit user choice persisted in localStorage ("cema_theme")
//   2. OS-level prefers-color-scheme
//   3. dark (the console's native/default theme)
// The resolved value is written to <html data-theme="..."> so every CSS
// token block in index.css ([data-theme="light"] / dark) resolves correctly,
// and non-CSS-var consumers (maplibre paint props, ECharts, cytoscape) can
// read it back via document.documentElement.dataset.theme.

const ThemeContext = createContext(null);
const STORAGE_KEY = "cema_theme";
const VALID = new Set(["dark", "light"]);

function readInitialTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && VALID.has(saved)) return saved;
  } catch { /* localStorage unavailable -- fall through */ }
  // OS-level prefers-color-scheme auto-detection disabled for the demo: the
  // console is locked to dark (ThemeToggle hidden in Layout.jsx). Restore the
  // block below alongside the toggle to re-enable light-mode auto-detection.
  // try {
  //   if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
  //     return "light";
  //   }
  // } catch { /* matchMedia unavailable */ }
  return "dark";
}

// Apply synchronously at module load so the very first render already has the
// right data-theme (avoids a dark->light flash on refresh for light users).
function applyTheme(theme) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-theme", theme);
  }
}
applyTheme(readInitialTheme());

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(readInitialTheme);

  useEffect(() => {
    applyTheme(theme);
    // Notify non-React color consumers (maplibre/ECharts/cytoscape) that
    // snapshot colors imperatively, so they can re-read tokens on flip.
    window.dispatchEvent(new CustomEvent("cema-theme-change", { detail: { theme } }));
  }, [theme]);

  const setTheme = useCallback((next) => {
    if (!VALID.has(next)) return;
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* noop */ }
    setThemeState(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setThemeState((prev) => {
      const next = prev === "dark" ? "light" : "dark";
      try { localStorage.setItem(STORAGE_KEY, next); } catch { /* noop */ }
      return next;
    });
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => {
  const ctx = useContext(ThemeContext);
  // Safe fallback so components used outside the provider (or in tests) don't
  // crash -- they just read the DOM's current theme and no-op on change.
  if (!ctx) {
    const current =
      (typeof document !== "undefined" && document.documentElement.getAttribute("data-theme")) ||
      "dark";
    return { theme: current, setTheme: () => {}, toggleTheme: () => {} };
  }
  return ctx;
};
