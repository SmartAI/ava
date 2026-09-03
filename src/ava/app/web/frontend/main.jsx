import React from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'

try {
  const storedTheme = localStorage.getItem('ava-theme')
  if (storedTheme) document.documentElement.dataset.theme = storedTheme
  const storedFontSize = localStorage.getItem('ava-font-size')
  if (['small', 'default', 'large'].includes(storedFontSize)) {
    document.documentElement.dataset.fontSize = storedFontSize
  }
} catch { /* Browser storage may be disabled. */ }

createRoot(document.getElementById('root')).render(<App />)
