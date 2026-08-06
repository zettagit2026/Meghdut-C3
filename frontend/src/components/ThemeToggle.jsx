import { Sun, Moon } from "lucide-react";
import { useTheme } from "@/context/ThemeContext";

// Accessible theme toggle placed in the top status bar (Layout.jsx), where an
// operator expects display/settings controls. Real <button>, keyboard
// focusable, descriptive aria-label + aria-pressed, and a title tooltip.
export default function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";
  const next = isDark ? "light" : "dark";
  return (
    <button
      type="button"
      data-testid="theme-toggle"
      onClick={toggleTheme}
      aria-label={`Switch to ${next} theme`}
      aria-pressed={!isDark}
      title={`Switch to ${next} theme`}
      className="flex items-center gap-1.5 px-2 py-1 tactical-border font-mono text-[10px] uppercase tracking-widest hover-surface transition-colors scanline-btn"
      style={{ color: "var(--accent-info)", borderColor: "var(--border-col)" }}
    >
      {isDark ? <Moon size={12} strokeWidth={1.5} /> : <Sun size={12} strokeWidth={1.5} />}
      <span>{isDark ? "DARK" : "LIGHT"}</span>
    </button>
  );
}
