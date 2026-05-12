import { create } from "zustand";

type Theme = "dark" | "light";
type Density = "comfy" | "compact";

interface UiState {
  theme: Theme;
  density: Density;
  pair: string;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  setDensity: (d: Density) => void;
  setPair: (p: string) => void;
}

const THEME_KEY = "quanta_v4_theme";
const DENSITY_KEY = "quanta_v4_density";
const PAIR_KEY = "quanta_v4_pair";

function readLs<T extends string>(key: string, fallback: T, allowed: readonly T[]): T {
  if (typeof window === "undefined") return fallback;
  const raw = window.localStorage.getItem(key);
  if (!raw) return fallback;
  return (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
}

function writeLs(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* private mode etc — non-fatal */
  }
}

export const useUi = create<UiState>((set, get) => ({
  theme: readLs<Theme>(THEME_KEY, "dark", ["dark", "light"] as const),
  density: readLs<Density>(DENSITY_KEY, "comfy", ["comfy", "compact"] as const),
  pair: readLs<string>(PAIR_KEY, "BTC/USD", ["BTC/USD"] as const) || "BTC/USD",
  setTheme: (t) => {
    writeLs(THEME_KEY, t);
    if (typeof document !== "undefined") {
      document.documentElement.dataset.theme = t;
    }
    set({ theme: t });
  },
  toggleTheme: () => {
    const next = get().theme === "dark" ? "light" : "dark";
    get().setTheme(next);
  },
  setDensity: (d) => {
    writeLs(DENSITY_KEY, d);
    set({ density: d });
  },
  setPair: (p) => {
    writeLs(PAIR_KEY, p);
    set({ pair: p });
  },
}));
