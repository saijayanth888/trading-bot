import type { Config } from "tailwindcss";

// owner: builder-C
// Tailwind 4 — most tokens live in CSS (`@theme` in tokens.css). This config
// is kept primarily for the `content` glob + darkMode class strategy. Wong
// palette (#0072B2 / #E69F00 / #D55E00) is PRIMARY per spec §4.3 + frontend-debate G1.
const config: Config = {
  darkMode: ["class", "[data-theme='dark']"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Wong palette — PRIMARY hue per frontend-debate G1.
        wong: {
          blue: "#0072B2",
          orange: "#E69F00",
          vermillion: "#D55E00",
          green: "#009E73",
          yellow: "#F0E442",
          sky: "#56B4E9",
          purple: "#CC79A7",
        },
        // P&L SECONDARY signal — muted green/red on numerals only per spec.
        pnl: {
          up: "var(--pnl-up)",
          down: "var(--pnl-down)",
        },
        bg: {
          page: "var(--bg-page)",
          card: "var(--bg-card)",
          inset: "var(--bg-inset)",
        },
        stroke: {
          1: "var(--stroke-1)",
          2: "var(--stroke-2)",
        },
        text: {
          1: "var(--text-1)",
          2: "var(--text-2)",
          3: "var(--text-3)",
        },
      },
      fontFamily: {
        sans: ["Geist", "Inter", "system-ui", "sans-serif"],
        mono: ["Geist Mono", "JetBrains Mono", "ui-monospace", "monospace"],
      },
      borderRadius: {
        sm: "6px",
        DEFAULT: "10px",
        lg: "14px",
      },
      keyframes: {
        "flash-accent": {
          "0%": { backgroundColor: "var(--flash-accent)" },
          "100%": { backgroundColor: "transparent" },
        },
        "flash-up": {
          "0%": { backgroundColor: "var(--flash-up)" },
          "100%": { backgroundColor: "transparent" },
        },
        "flash-down": {
          "0%": { backgroundColor: "var(--flash-down)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
      animation: {
        "flash-accent": "flash-accent 300ms ease-out",
        "flash-up": "flash-up 300ms ease-out",
        "flash-down": "flash-down 300ms ease-out",
      },
    },
  },
};

export default config;
