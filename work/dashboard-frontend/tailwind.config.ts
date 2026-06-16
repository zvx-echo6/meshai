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
          DEFAULT: '#111111',
          card: '#0d0d0d',
          hover: '#161616',
          elevated: '#1a1a1a',
        },
        border: {
          DEFAULT: '#1e1e1e',
          light: '#222222',
          bright: '#2a2a2a',
        },
        accent: {
          DEFAULT: '#f59e0b',
          dim: '#d97706',
          muted: 'rgba(245,158,11,0.12)',
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
