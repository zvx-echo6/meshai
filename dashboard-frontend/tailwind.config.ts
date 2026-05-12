import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: '#0a0e17',
          card: '#111827',
          hover: '#1a2332',
        },
        border: {
          DEFAULT: '#1e2a3a',
          light: '#2d3a4d',
        },
        accent: '#3b82f6',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
