import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    borderRadius: {
      none: '0',
      DEFAULT: '0',
      sm: '0',
      md: '0',
      lg: '0',
      xl: '0',
      '2xl': '0',
      full: '9999px',
    },
    extend: {
      colors: {
        bg: {
          DEFAULT: '#0a0e17',
          card: '#0f1520',
          hover: '#162030',
          elevated: '#1a2535',
        },
        border: {
          DEFAULT: '#1c2a3a',
          light: '#243345',
          bright: '#2d4060',
        },
        amber: {
          DEFAULT: '#f59e0b',
          dim: '#d97706',
          glow: '#fbbf24',
          muted: 'rgba(245,158,11,0.15)',
        },
        blue: {
          DEFAULT: '#38bdf8',
          dim: '#0ea5e9',
          muted: 'rgba(56,189,248,0.12)',
        },
        danger: {
          DEFAULT: '#f97316',
          dim: '#ea580c',
          muted: 'rgba(249,115,22,0.15)',
        },
        slate: {
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
