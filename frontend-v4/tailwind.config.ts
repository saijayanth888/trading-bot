import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// V4 design tokens preserve the names from user_data/dashboard/static/css/quanta.css
// so the new SPA reads like the existing one for any operator who has memorized
// the color semantics. Geist replaces Inter as the primary sans.
const config: Config = {
  darkMode: ["class", "[data-theme='dark']"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    container: {
      center: true,
      padding: "1.5rem",
      screens: {
        "2xl": "1440px",
      },
    },
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "Geist", "Inter", "system-ui", "sans-serif"],
        mono: [
          "var(--font-mono)",
          "Geist Mono",
          "JetBrains Mono",
          "ui-monospace",
          "monospace",
        ],
      },
      fontSize: {
        // 9·10·11·12·13·15·22·38 scale from PROMPT.md
        "2xs": ["9px", { lineHeight: "12px" }],
        xxs: ["10px", { lineHeight: "14px" }],
      },
      colors: {
        // semantic tokens — map to CSS vars defined in globals.css
        bg: {
          page: "var(--bg-page)",
          card: "var(--bg-card)",
          "card-2": "var(--bg-card-2)",
          inset: "var(--bg-inset)",
          overlay: "var(--bg-overlay)",
          rail: "var(--bg-rail)",
        },
        stroke: {
          1: "var(--stroke-1)",
          2: "var(--stroke-2)",
          3: "var(--stroke-3)",
        },
        text: {
          1: "var(--text-1)",
          2: "var(--text-2)",
          3: "var(--text-3)",
          4: "var(--text-4)",
        },
        success: {
          DEFAULT: "var(--success)",
          bg: "var(--success-bg)",
          line: "var(--success-line)",
        },
        danger: {
          DEFAULT: "var(--danger)",
          bg: "var(--danger-bg)",
          line: "var(--danger-line)",
        },
        warn: {
          DEFAULT: "var(--warn)",
          bg: "var(--warn-bg)",
          line: "var(--warn-line)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          bg: "var(--accent-bg)",
          line: "var(--accent-line)",
        },
        info: {
          DEFAULT: "var(--info)",
          bg: "var(--info-bg)",
          line: "var(--info-line)",
        },
      },
      borderRadius: {
        sm: "6px",
        DEFAULT: "10px",
        lg: "14px",
      },
      keyframes: {
        "debate-pulse": {
          "0%, 100%": { transform: "scale(1)", opacity: "1" },
          "50%": { transform: "scale(1.4)", opacity: "0.5" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "token-pop": {
          "0%": { opacity: "0", transform: "translateY(2px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        marquee: {
          from: { transform: "translateX(0)" },
          to: { transform: "translateX(-50%)" },
        },
      },
      animation: {
        "debate-pulse": "debate-pulse 1.6s ease infinite",
        "fade-in": "fade-in 200ms ease-out",
        "token-pop": "token-pop 120ms ease-out",
        marquee: "marquee 40s linear infinite",
      },
    },
  },
  plugins: [animate],
};

export default config;
